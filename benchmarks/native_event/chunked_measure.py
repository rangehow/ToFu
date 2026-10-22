"""Actual TaskRuntime append/tail/full replay with bounded chunked retention.

Same canonical producer, clock, byte accounting and poll projection. The
experimental retention/reader binding differs; transport and durability are not
measured. Memory workers independently repeat the same operation count.
"""
import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunked_runtime import create, normalized
from chunked_window import ChunkedWindow
from native_reader import native_reader
from lib.task_replay.task_replay import task_memory_replay_page


def tail_witness(task):
    cursor = max(task['_eventBaseSeq'], task['_eventNextSeq'] - 4)
    reader = native_reader if type(task['events']) is ChunkedWindow else task_memory_replay_page
    return normalized(reader(task, cursor).payload())


def worker(args):
    os.environ['TOFU_BENCHMARK_SAMPLES'] = str(args.samples)
    path = Path(__file__).with_name('runtime_measure.py')
    function = next(node for node in ast.parse(path.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'worker')
    # Preserve actual operation/timing bodies; attach an untimed final tail
    # witness without constructing a full-window index for append-only runs.
    source = ast.unparse(function)
    warmup = 'for _ in range(20):'
    assert source.count(warmup) == 1
    source = source.replace(warmup, 'cold_started = time.perf_counter_ns()\n    operation()\n    cold_ms = (time.perf_counter_ns()-cold_started)/1e6\n    ' + warmup)
    boundary = 'return {'
    assert source.count(boundary) == 2
    source = source.replace(boundary,
        "return dict(pid=os.getpid(), cold_first_operation_ms=cold_ms, warmup_operations=20, output_trace_sha256=hashlib.sha256(json.dumps(tail_witness(task), sort_keys=True).encode()).hexdigest(), **{")
    tree = ast.parse(source.replace("'retained_event_bytes': task['_eventRetainedBytes']}",
                                    "'retained_event_bytes': task['_eventRetainedBytes']})")
                    .replace("resource.RUSAGE_SELF).ru_maxrss * 1024}",
                             "resource.RUSAGE_SELF).ru_maxrss * 1024})"))
    namespace = dict(globals())
    exec(compile(tree, '<canonical-chunked-retention-worker>', 'exec'), namespace)
    return namespace['worker']('native' if args.variant == 'chunked' else 'reference',
                               args.count, args.case, args.memory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--variant', choices=['reference', 'chunked'])
    parser.add_argument('--count', type=int)
    parser.add_argument('--case', choices=['append', 'tail', 'full'])
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.samples < 1 or args.pairs < 1:
        parser.error('samples/pairs must be positive')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    if args.output is None:
        parser.error('--output required')
    names = ('chunked_window.py', 'chunked_runtime.py', 'check_chunked.py', 'chunked_measure.py',
             'runtime.py', 'runtime_measure.py', 'runtime_faults.py', 'native_reader.py')
    sources = {str((Path(__file__).parent / n).relative_to(ROOT)):
               hashlib.sha256((Path(__file__).parent / n).read_bytes()).hexdigest() for n in names}
    sources.update({n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
                    for n in ('lib/agent_core/task_runtime.py', 'lib/task_replay/task_replay.py')})
    report = dict(format='tofu.chunked-event-retention/v1', scope=__doc__, whole_system_verdict='incomplete',
                  production_promoted=False, sources=sources, samples=args.samples, required_pairs=args.pairs,
                  runtime=dict(python=sys.version, platform=platform.platform(), affinity=sorted(os.sched_getaffinity(0))), cases=[])
    for count in (64, 4096, 65536):
        for case in ('append', 'tail', 'full'):
            row = dict(count=count, case=case, pairs=[])
            report['cases'].append(row)
            for pair in range(args.pairs):
                values = {}
                for variant in (('reference', 'chunked') if pair % 2 == 0 else ('chunked', 'reference')):
                    for n, h in sources.items():
                        assert hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == h
                    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__, '--worker',
                               '--variant', variant, '--count', str(count), '--case', case, '--samples', str(args.samples)]
                    def invoke(memory=False):
                        result = subprocess.run(command + (['--memory'] if memory else []), check=True,
                                                capture_output=True, text=True, timeout=300)
                        return json.loads(result.stdout)
                    value, memory = invoke(), invoke(True)
                    assert value['pid'] != memory['pid'] and value['output_trace_sha256'] == memory['output_trace_sha256']
                    value['memory_pid'] = memory['pid']; value['peak_rss_bytes'] = memory['peak_rss_bytes']
                    values[variant] = value
                assert values['reference']['output_trace_sha256'] == values['chunked']['output_trace_sha256']
                for key in ('retained_events', 'retained_event_bytes'):
                    assert values['reference'][key] == values['chunked'][key]
                row['pairs'].append(values)
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                print(count, case, pair, {key: round(values['chunked'][key] / values['reference'][key], 3)
                    for key in ('p50_ms', 'p99_ms', 'cpu_ms_per_operation', 'peak_rss_bytes')}, flush=True)


if __name__ == '__main__':
    main()
