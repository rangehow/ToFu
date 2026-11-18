"""Wall-clock breakdown of a steady Tofu round (``noop`` and ``run_command true``).

``python3 benchmarks/harness_perf/round_profile.py --root DIR --out DIR [--no-spy]``

Boots the same private deployment as ``run.py`` (engine, execution host and
shards, API host, zero-latency stub), warms it, then runs ``noop`` Turns of
``--rounds`` read rounds and ``true`` Turns of one ``run_command true`` round.
Unless ``--no-spy``, py-spy samples the execution host and every subprocess
(its shards) with idle threads included, so waits count as wall time.
``attribute`` charges each sample of the thread running the Turn to the first
matching component of ``COMPONENTS``; a component's share of those samples
times the Turn's wall time per request is its wall clock per round. Without
py-spy the run is an unperturbed timing sample (``result.json``: round
overheads with raw samples, the TurnTrace split and the stub records), the unit
of an ABAB comparison. ``attribute FILE`` re-reads a folded profile.
Method and results: docs/HARNESS_PERFORMANCE.md, "Round hops".
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
TURN_FRAME = 'run_task (lib'

# First match wins, most specific first. A key is a frame substring, or a
# tuple of substrings that must all occur in the stack.
COMPONENTS = (
    ('turn start-up (before round 0)', ('setup_project_context', 'prepare_turn')),
    ('turn finalize/settle (after the last response)', ('finalize_after_loop', '_finalize_and_emit_done')),
    ('thread creation per request/round', ('Thread.start (',)),
    ('provider exchange: send + response headers (post thread)', ('post_headers_abortable',)),
    ('provider exchange: response body read', ('_iter_response_bytes',)),
    ('MCP relay RPC to the execution host', ('RelayClient.call',)),
    ('event barrier: tools_in_flight', (('execute_tools (', 'checkpoint_invocation'),)),
    ('event barrier: pre-request (deferred ready)', ('checkpoint_model_dispatch',)),
    ('event barrier: other', ('_await_task_events', 'flush_task_events')),
    ('storage read: turn requirements', ('_RequirementsRead',)),
    ('storage read: working facts', (('refresh_working_facts', 'Future.result'),)),
    ('storage read: steers (turn head)', ('take_steers',)),
    ('storage read: other synchronous RPC', ('StorageV2Client._call',)),
    ('tool execution (awaited worker)', (('inject_into_cache', 'Future.result'), '_completed_tools_until_cancel')),
    ('request prep: prepare_request (seal, translate, recount, fingerprints)', ('prepare_request (',)),
    ('request prep: build_round_request + request snapshot', ('build_round_request',)),
    ('request prep: slot adaptation, admission count, dispatcher pick',
     ('_adapt_stream_body_for_slot', 'pick_and_reserve')),
    ('context prep: prompt admission count', ('admit_round_context',)),
    ('context prep: composer, hygiene, inbox', ('prepare_round_context',)),
    ('post-tool state snapshot', ('_emit_post_tool_snapshot',)),
    ('tool loop breaker', ('handle_tool_loop_circuit_breaker',)),
    ('stream tool callback (prefetch submit, tool_start)', ('_notify_tool_call_ready',)),
    ('cache-break accounting', ('stamp_round_cache_accounting',)),
    ('tool pipeline CPU (parse, settle, budget)', ('run_tool_dispatch', 'settle_stream_accumulator')),
    ('round close', ('close_round', 'run_round_checkpoint')),
    ('SSE parse and stream state', ('_stream_chat_once_impl', 'SSEAccumulator')),
    ('event append (projection, detach)', ('append_event (',)),
)


def _matches(stack: str, key) -> bool:
    return all(part in stack for part in key) if isinstance(key, tuple) else key in stack


def attribute(folded_lines, wall_ms_per_request: float = 1.0) -> dict:
    """Shares of the Turn thread's samples per component, and ms per request."""
    counts = collections.Counter()
    total = 0
    for line in folded_lines:
        stack, _, count = line.rstrip().rpartition(' ')
        if TURN_FRAME not in stack or not count.isdigit():
            continue
        total += int(count)
        name = next((name for name, keys in COMPONENTS if any(_matches(stack, key) for key in keys)),
                    'other loop work')
        counts[name] += int(count)
    rows = [{'component': name, 'share': round(count / total, 4),
             'ms_per_request': round(wall_ms_per_request * count / total, 2)}
            for name, count in counts.most_common()] if total else []
    return {'samples': total, 'rows': rows}


def _child(args) -> int:
    sys.path[:0] = [str(PROJECT_ROOT), str(HERE), str(PROJECT_ROOT / 'scripts')]
    import run as harness
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stub = harness.Stub(out)
    deployment = harness.TofuDeployment(Path(args.root), stub, argparse.Namespace(
        startup_timeout=600.0, turn_timeout=300.0, engine=''))
    result = {}
    try:
        deployment.start()
        # Warm-up: first-use imports and caches are cold start, not rounds.
        harness.sequential_phase(deployment, stub, 'chat', 1, 0, 0.01)
        harness.sequential_phase(deployment, stub, 'noop', 1, 3, 0.01)
        harness.sequential_phase(deployment, stub, 'true', 2, 1, 0.01)
        for scenario, turns, rounds in (('noop', args.noop_turns, args.rounds), ('true', args.true_turns, 1)):
            spies = []
            if not args.no_spy:
                for name, pid in (('exec', deployment.execution_pid), ('api', deployment.api_pid)):
                    spies.append(subprocess.Popen(
                        [args.py_spy, 'record', '--pid', str(pid), '--idle', '--threads', '--subprocesses',
                         '--nonblocking', '--rate', str(args.rate), '--format', 'raw',
                         '--output', str(out / f'{scenario}-{name}.folded')],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                time.sleep(1.5)
            phase = harness.sequential_phase(deployment, stub, scenario, turns, rounds, 0.01)
            result[scenario] = {key: phase[key] for key in (
                'round_overhead_ms', 'steady_round_overhead_ms', 'ttfr_ms', 'settle_ms', 'requests_per_turn')}
            for spy in spies:
                spy.send_signal(signal.SIGINT)
            for spy in spies:
                spy.wait(timeout=120)
            print(scenario, json.dumps({key: value.get('median') for key, value in result[scenario].items()}),
                  flush=True)
        result['turn_trace'] = harness.turn_trace_breakdown(Path(args.root) / 'logs' / 'app.log')
        result['records'] = stub.records()
        if not args.no_spy:
            for scenario in ('noop', 'true'):
                trace = result['turn_trace'].get(scenario) or {}
                requests = (result[scenario]['requests_per_turn'] or {}).get('median') or 1
                per_request = (trace.get('totalMs') or 0) / requests
                with open(out / f'{scenario}-exec.folded', errors='replace') as folded:
                    result[f'{scenario}_attribution'] = attribute(folded, per_request)
    finally:
        deployment.close()
        stub.close()
        (out / 'result.json').write_text(json.dumps(result, indent=1, default=str))
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ['attribute']:
        with open(argv[1], errors='replace') as folded:
            print(json.dumps(attribute(folded, float(argv[2]) if len(argv) > 2 else 1.0), indent=1))
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, help='private working root')
    parser.add_argument('--out', required=True)
    parser.add_argument('--noop-turns', type=int, default=4)
    parser.add_argument('--true-turns', type=int, default=20)
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--rate', type=int, default=400, help='py-spy samples per second (measurement design)')
    parser.add_argument('--no-spy', action='store_true')
    parser.add_argument('--py-spy', default=shutil.which('py-spy') or 'py-spy')
    parser.add_argument('--engine', default=os.environ.get('TOFUDB_BIN', ''))
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args)
    sys.path[:0] = [str(PROJECT_ROOT), str(HERE), str(PROJECT_ROOT / 'scripts')]
    import run as harness
    from serverctl_pkg.local_placement import temporary_root
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    harness.storage_harness._refuse_live_root(root)
    (root / '.tofudb-load-test').touch()
    if not args.engine or not Path(args.engine).is_file():
        raise SystemExit('--engine (or $TOFUDB_BIN) must name a tofu-db binary')
    engine, digest = harness.storage_harness._pin_engine(Path(args.engine), root)
    scratch = Path(tempfile.mkdtemp(prefix='harness-perf-tmp-', dir=temporary_root()))
    try:
        environment = harness.storage_harness._child_environment(root.resolve(), scratch, engine, digest,
                                                                 'auto', 'tofudb')
        environment['TOFU_PROJECT_PATH'] = str(PROJECT_ROOT)
        environment['TOFU_SUPERVISOR_PORT'] = str(harness._free_port())
        browsers = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or str(Path.home() / '.cache' / 'ms-playwright')
        if Path(browsers).is_dir():
            environment['PLAYWRIGHT_BROWSERS_PATH'] = browsers
        command = [sys.executable, str(Path(__file__).resolve()), '--child', *argv]
        return subprocess.run(command, env=environment, cwd=str(root.resolve())).returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
