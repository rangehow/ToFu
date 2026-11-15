#!/usr/bin/env python3
"""Tool dispatch overhead — in-process cost around each tool call.

Responsibility: drive the REAL ``execute_tool_pipeline`` (admission, policy,
approval, handler, result settlement, event emission) for one call at a time
per tool type on a production-shaped task (managed event-delivery lane,
execution clock) against a disposable SQLite storage sidecar, and report how
much wall time Tofu spends before and after each handler. It also probes
whether one model turn's parallel calls really overlap.

This is a component observation, not an end-to-end latency claim: there is
no model, the storage sidecar is local and idle, and production adds the
event durability barriers measured by ``debug/tool_overhead_audit.py``.

Per call (milliseconds, p50/p90 over ``--iterations``; each turn empties the
per-task result-reuse cache so every call really executes):
``wall``     pipeline entry to return.
``pre``      pipeline entry to registry-handler entry (admission, policy,
             approval, pool hand-off).
``handler``  registry handler entry to exit (includes the tool's own work).
``post``     handler exit to pipeline return (settlement, budgets, events).
Handler entry/exit come from a benchmark-local registry shim.

Usage::

    python3 benchmarks/tool_dispatch_overhead.py [--iterations 20]
        [--workdir DIR] [--tools run_command,read_files] [--json]

``--workdir`` places the disposable project tree (default: a fresh temporary
directory); point it at a FUSE path to include that filesystem's costs. The
project tree and sidecar are deleted afterwards. No network or model calls.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SLEEP_TOOL = 'bench_parallel_sleep'
SLEEP_SECONDS = 0.2
PARALLEL_CALLS = 4


def _tool_arguments(project):
    """Deterministic, side-effect-contained arguments per measured tool."""
    return {
        'run_command': lambda index: {'command': f'echo bench-{index}'},
        'read_files': lambda index: {'path': 'src/module_0.py', 'start_line': 1,
                                     'end_line': 3 + index % 5},
        'search_files': lambda index: {'pattern': f'needle_{index % 7}', 'path': 'src'},
        'find_files': lambda index: {'pattern': f'module_{index % 9}*.py', 'path': 'src'},
        'write_file': lambda index: {'path': 'out/written.txt', 'content': f'line {index}\n'},
        'edit_file': lambda index: {'path': 'out/edited.txt', 'edits': [{
            'operation': 'replace', 'anchor': f'value={index}',
            'content': f'value={index + 1}'}]},
    }


def build_project(root):
    """A small git-free tree with searchable content and edit targets."""
    src = root / 'src'
    src.mkdir(parents=True)
    for index in range(60):
        (src / f'module_{index}.py').write_text(
            ''.join(f'def function_{line}():\n    return "needle_{line % 7}"\n'
                    for line in range(40)), encoding='utf-8')
    (root / 'out').mkdir()
    (root / 'out' / 'edited.txt').write_text('value=0\n', encoding='utf-8')
    (root / 'AGENTS.md').write_text('# Benchmark project\n', encoding='utf-8')


def start_sidecar(data_root):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(StorageSupervisor(
        project_root=data_root, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    return runtime


def stop_sidecar(runtime):
    from lib.storage.service import install_runtime_for_test
    install_runtime_for_test(None)
    runtime.stop()


def make_task(project):
    """A managed root carrier: execution clock plus the async delivery lane."""
    from lib.agent_core.execution_clock import ExecutionClock
    return {
        'id': f'bench-{uuid.uuid4().hex}', 'convId': f'bench-conv-{uuid.uuid4().hex[:12]}',
        '_userId': 1, 'model': 'bench-model', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False, '_tool_result_cache': {},
        'config': {'projectPath': str(project), 'autoApply': True},
        '_executionClock': ExecutionClock.start(), '_workerStartedAt': time.time(),
    }


class HandlerClock:
    """Benchmark-local registry shim stamping handler entry/exit per call."""

    def __init__(self, tools):
        from lib.tasks_pkg.executor import tool_registry
        self.registry = tool_registry
        self.stamps = {}
        self.installed = {}
        for name in tools:
            original = tool_registry.lookup(name)
            if original is None:
                continue
            self.installed[name] = tool_registry._exact.get(name)
            tool_registry._exact[name] = self._wrap(original)

    def _wrap(self, handler):
        def timed(task, tc, fn_name, tc_id, *args, **kwargs):
            entered = time.perf_counter()
            try:
                return handler(task, tc, fn_name, tc_id, *args, **kwargs)
            finally:
                self.stamps[tc_id] = (entered, time.perf_counter())
        return timed

    def close(self):
        for name, previous in self.installed.items():
            if previous is None:
                self.registry._exact.pop(name, None)
            else:
                self.registry._exact[name] = previous


class StorageOps:
    """Counts storage operations other than the async event-lane writes.

    Every such operation is a synchronous sidecar round trip on the calling
    tool's path; the count is load-independent where the clocks are not.
    """

    def __init__(self):
        from lib.storage import client as storage_client
        self.owner = storage_client.StorageClient
        self.originals = {name: getattr(self.owner, name) for name in ('command', 'query')}
        self.count = 0
        for name, original in self.originals.items():
            setattr(self.owner, name, self._wrap(original))

    def _wrap(self, original):
        def counted(client, operation, *args, **kwargs):
            if not str(operation).startswith('event.'):
                self.count += 1
            return original(client, operation, *args, **kwargs)
        return counted

    def close(self):
        for name, original in self.originals.items():
            setattr(self.owner, name, original)


class Dispatcher:
    """Runs one model turn's calls through the production pipeline.

    The per-task result-reuse cache is emptied before each turn so every
    measured call executes instead of replaying an earlier identical call.
    """

    def __init__(self, task, project, clock, storage_ops):
        self.task = task
        self.project = str(project)
        self.clock = clock
        self.storage_ops = storage_ops
        self.round = 0

    def run(self, calls):
        from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline, parse_tool_calls
        self.task['_tool_result_cache'].clear()
        self.round += 1
        tool_calls = [{'id': f'call-{self.round}-{index}', 'type': 'function',
                       'function': {'name': name, 'arguments': json.dumps(arguments)}}
                      for index, (name, arguments) in enumerate(calls)]
        parsed, _ = parse_tool_calls({'content': '', 'tool_calls': tool_calls}, self.task,
                                     round_num=self.round, tool_round_num=self.round,
                                     project_enabled=True)
        messages = []
        self.storage_ops.count = 0
        started = time.perf_counter()
        execute_tool_pipeline(
            self.task, parsed, cfg={'autoApply': True, 'projectPath': self.project},
            project_path=self.project, project_enabled=True, tool_list=None,
            messages=messages, all_search_results_text=[], round_num=self.round,
            model='bench-model')
        returned = time.perf_counter()
        rows = [item[5] for item in parsed]
        stamps = [self.clock.stamps.pop(item[2], None) for item in parsed]
        return started, returned, rows, stamps, messages


def _split(started, returned, stamp):
    wall = (returned - started) * 1000
    if stamp is None:
        return {'wall': wall}
    entered, exited = stamp
    return {'wall': wall, 'pre': (entered - started) * 1000,
            'handler': (exited - entered) * 1000, 'post': (returned - exited) * 1000}


def _stats(values):
    ordered = sorted(values)
    return {'n': len(ordered), 'p50': statistics.median(ordered),
            'p90': ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))],
            'mean': statistics.fmean(ordered)}


def measure_serial_calls(dispatcher, tools, iterations, warmup):
    arguments = _tool_arguments(dispatcher.project)
    samples = {tool: {} for tool in tools}
    failures = {}
    for index in range(warmup + iterations):
        for tool in tools:
            if tool == 'edit_file':
                # Edit admission requires a read of the target in an earlier turn.
                dispatcher.run([('read_files', {'path': 'out/edited.txt'})])
            started, returned, rows, stamps, messages = dispatcher.run(
                [(tool, arguments[tool](index))])
            content = str((messages[-1] if messages else {}).get('content') or '')
            if rows[0].get('status') == 'error' or content.startswith('Error'):
                failures.setdefault(tool, content[:300])
            if index < warmup:
                continue
            for key, value in _split(started, returned, stamps[0]).items():
                samples[tool].setdefault(key, []).append(value)
            samples[tool].setdefault('storage_ops', []).append(
                dispatcher.storage_ops.count)
    return {tool: {key: _stats(values) for key, values in series.items()}
            for tool, series in samples.items()}, failures


_SLEEP_STARTS = []


def _sleep_handler(task, tc, name, tc_id, fn_args, rn, round_entry, cfg,
                   project_path, project_enabled, all_tools=None):
    _SLEEP_STARTS.append(time.perf_counter())
    time.sleep(SLEEP_SECONDS)
    return tc_id, 'slept', False


def measure_parallelism(dispatcher):
    """One turn of independent sleeping calls.

    ``serialization`` = wall / (calls x sleep): about 1/calls when the calls
    overlap, about 1.0 when one lane runs them one after another.
    """
    from lib.tasks_pkg.executor import tool_registry
    tool_registry.register(SLEEP_TOOL, _sleep_handler)
    try:
        cases = {
            'parallel_read_tool': [(SLEEP_TOOL, {'n': index}) for index in range(PARALLEL_CALLS)],
            'parallel_run_command': [
                ('run_command', {'command': f'sleep {SLEEP_SECONDS}; echo {index}'})
                for index in range(PARALLEL_CALLS)],
        }
        dispatcher.run(cases['parallel_read_tool'][:1])
        report = {}
        for name, calls in cases.items():
            _SLEEP_STARTS.clear()
            started, returned, rows, stamps, _messages = dispatcher.run(calls)
            wall = (returned - started) * 1000
            starts = sorted(_SLEEP_STARTS) if _SLEEP_STARTS else sorted(
                stamp[0] for stamp in stamps if stamp is not None)
            report[name] = {
                'calls': len(calls), 'wall_ms': wall,
                'serialization': wall / (len(calls) * SLEEP_SECONDS * 1000),
                'start_spread_ms': ((starts[-1] - starts[0]) * 1000) if starts else None,
            }
        return report
    finally:
        tool_registry._exact.pop(SLEEP_TOOL, None)
        tool_registry._metadata.pop(SLEEP_TOOL, None)


def render(report):
    lines = ['tool dispatch overhead (ms; p50/p90 over iterations; storage ops = mean '
             'synchronous non-event sidecar round trips per call)',
             f"{'tool':<14}{'wall':>16}{'pre':>16}{'handler':>16}{'post':>16}{'storage ops':>13}"]
    for tool, stats in report['serial'].items():
        cells = []
        for key in ('wall', 'pre', 'handler', 'post'):
            value = stats.get(key)
            cells.append('-'.rjust(16) if value is None
                         else f"{value['p50']:.1f}/{value['p90']:.1f}".rjust(16))
        cells.append(f"{stats['storage_ops']['mean']:.1f}".rjust(13))
        lines.append(f'{tool:<14}' + ''.join(cells))
    for name, probe in report['parallel'].items():
        spread = probe['start_spread_ms']
        lines.append(f"{name}: {probe['calls']} calls x {SLEEP_SECONDS * 1000:.0f} ms -> "
                     f"wall {probe['wall_ms']:.0f} ms, serialization "
                     f"{probe['serialization']:.2f}, start spread "
                     f"{'-' if spread is None else f'{spread:.0f}'} ms")
    for tool, detail in report['failures'].items():
        lines.append(f'FAILED {tool}: {detail}')
    return lines


def run(args):
    workdir = Path(tempfile.mkdtemp(prefix='tool-dispatch-bench-', dir=args.workdir))
    storage_dir = Path(tempfile.mkdtemp(prefix='tool-dispatch-bench-storage-'))
    runtime = clock = storage_ops = None
    try:
        project = workdir / 'project'
        build_project(project)
        runtime = start_sidecar(storage_dir)
        import lib.tasks_pkg.tool_dispatch.api  # noqa: F401 - registers the tool handlers
        clock = HandlerClock(set(args.tools) | {'read_files', 'run_command'})
        storage_ops = StorageOps()
        dispatcher = Dispatcher(make_task(project), project, clock, storage_ops)
        serial, failures = measure_serial_calls(dispatcher, args.tools, args.iterations,
                                                args.warmup)
        parallel = measure_parallelism(dispatcher)
        from lib.tasks_pkg.event_delivery import flush_task_events
        flush_task_events(dispatcher.task, timeout=60)
        return {'workdir': str(workdir), 'iterations': args.iterations,
                'serial': serial, 'parallel': parallel, 'failures': failures}
    finally:
        if storage_ops is not None:
            storage_ops.close()
        if clock is not None:
            clock.close()
        if runtime is not None:
            stop_sidecar(runtime)
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(storage_dir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--workdir', default=None,
                        help='parent directory for the disposable project tree')
    parser.add_argument('--tools', default=','.join(_tool_arguments('')),
                        help='comma-separated subset of measured tools')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    args.tools = [tool for tool in args.tools.split(',') if tool]
    unknown = sorted(set(args.tools) - set(_tool_arguments('')))
    if unknown or not 1 <= args.iterations <= 10_000 or not 0 <= args.warmup <= 100:
        parser.error(f'invalid tools {unknown} or iteration counts')
    report = run(args)
    print(json.dumps(report, indent=2) if args.json else '\n'.join(render(report)))
    return 1 if report['failures'] else 0


if __name__ == '__main__':
    sys.exit(main())
