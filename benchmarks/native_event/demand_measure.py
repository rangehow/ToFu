"""Actual runtime demand-layout append/growth/mixed/tail/full comparisons.

Every operation includes its real producer/reader work. Cold operation and
layout migrations remain visible; pure growth deliberately starts empty.
Transport, durable recovery and full-event domain admission remain separate.
"""
import argparse
import hashlib
import json
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
from demand_runtime import create
from chunked_measure import tail_witness
from chunked_window import ChunkedWindow


def worker(args):
    runtime, task = create('native' if args.variant == 'demand' else 'reference', args.count)
    if args.case != 'grow':
        for _ in range(args.count):
            runtime.append_event(task['id'], {'type': 'delta', 'delta': 'fixture'}, deliver_push=False)
    def operation():
        if args.case in ('append', 'grow', 'mixed'):
            runtime.append_event(task['id'], {'type': 'delta', 'delta': 'fixture'}, deliver_push=False)
        if args.case in ('tail', 'mixed'):
            runtime.poll(task['id'], max(0, task['_eventNextSeq'] - 4))
        elif args.case == 'full':
            runtime.poll(task['id'], task['_eventBaseSeq'])
    started = time.perf_counter_ns(); operation(); cold_ms = (time.perf_counter_ns() - started) / 1e6
    for _ in range(20):
        operation()
    raw = []
    for _ in range(args.samples):
        started = time.perf_counter_ns(); cpu = time.process_time_ns()
        operation()
        raw.append({'wall_ms': (time.perf_counter_ns() - started) / 1e6,
                    'cpu_ms': (time.process_time_ns() - cpu) / 1e6})
    trace = hashlib.sha256(json.dumps(tail_witness(task), sort_keys=True).encode()).hexdigest()
    ordered = sorted(x['wall_ms'] for x in raw)
    result = dict(pid=os.getpid(), raw=raw, p50_ms=statistics.median(ordered),
                  p95_ms=ordered[int((len(ordered) - 1) * .95)], p99_ms=ordered[int((len(ordered) - 1) * .99)],
                  cpu_ms_per_operation=statistics.mean(x['cpu_ms'] for x in raw),
                  peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                  cold_first_operation_ms=cold_ms, warmup_operations=20, output_trace_sha256=trace,
                  retained_events=len(task['events']), retained_event_bytes=task['_eventRetainedBytes'],
                  final_layout='chunked' if type(task['events']) is ChunkedWindow else 'list')
    runtime.finish(task['id'], result='measured')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--variant', choices=['reference', 'demand'])
    parser.add_argument('--count', type=int)
    parser.add_argument('--case', choices=['append', 'grow', 'mixed', 'tail', 'full'])
    parser.add_argument('--counts', type=int, nargs='+', default=[64, 1024, 4096, 65536])
    parser.add_argument('--cases', nargs='+', choices=['append', 'grow', 'mixed', 'tail', 'full'], default=['append', 'grow', 'mixed', 'tail', 'full'])
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.samples < 1 or args.pairs < 1 or any(count < 1 for count in args.counts):
        parser.error('positive samples/pairs required')
    if args.worker:
        print(json.dumps(worker(args))); return
    if args.output is None:
        parser.error('--output required')
    names = ('demand_runtime.py', 'demand_measure.py', 'check_demand.py', 'chunked_runtime.py',
             'chunked_window.py', 'chunked_measure.py', 'runtime.py', 'native_reader.py')
    sources = {str((Path(__file__).parent / n).relative_to(ROOT)):
               hashlib.sha256((Path(__file__).parent / n).read_bytes()).hexdigest() for n in names}
    sources.update({n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
                    for n in ('lib/agent_core/task_runtime.py', 'lib/task_replay/task_replay.py')})
    report = dict(format='tofu.event-demand-layout/v1', whole_system_verdict='incomplete', production_promoted=False,
                  scope=__doc__, sources=sources, samples=args.samples, required_pairs=args.pairs,
                  runtime=dict(python=sys.version, platform=platform.platform(), affinity=sorted(os.sched_getaffinity(0))), cases=[])
    for count in args.counts:
        for case in args.cases:
            row = dict(count=count, case=case, pairs=[]); report['cases'].append(row)
            for pair in range(args.pairs):
                values = {}
                for variant in (('reference', 'demand') if pair % 2 == 0 else ('demand', 'reference')):
                    for n, h in sources.items():
                        assert hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == h
                    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__, '--worker',
                               '--variant', variant, '--count', str(count), '--case', case, '--samples', str(args.samples)]
                    def invoke():
                        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
                        return json.loads(completed.stdout)
                    value, memory = invoke(), invoke()
                    assert value['pid'] != memory['pid']
                    for key in ('output_trace_sha256', 'retained_events', 'retained_event_bytes', 'final_layout'):
                        assert value[key] == memory[key]
                    value.update(memory_pid=memory['pid'], peak_rss_bytes=memory['peak_rss_bytes']); values[variant] = value
                for key in ('output_trace_sha256', 'retained_events', 'retained_event_bytes'):
                    assert values['reference'][key] == values['demand'][key]
                row['pairs'].append(values); args.output.write_text(json.dumps(report, indent=2) + '\n')
                print(count, case, pair, {key: round(values['demand'][key] / values['reference'][key], 3)
                    for key in ('p50_ms', 'p99_ms', 'cpu_ms_per_operation', 'peak_rss_bytes')}, flush=True)


if __name__ == '__main__':
    main()
