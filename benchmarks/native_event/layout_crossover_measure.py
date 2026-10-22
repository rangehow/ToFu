"""Actual runtime write-only layout crossover; cold initialization kept separate.

Measure capacities around the observed small-window regression before selecting
any layout threshold. No reader/source/durable/production admission is inferred.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunked_measure import worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--variant', choices=['reference', 'chunked'])
    parser.add_argument('--count', type=int)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    args.case = 'append'
    if args.samples < 1 or args.pairs < 1:
        parser.error('samples/pairs must be positive')
    if args.worker:
        print(json.dumps(worker(args)))
        return
    if args.output is None:
        parser.error('--output required')
    names = ('layout_crossover_measure.py', 'chunked_window.py', 'chunked_runtime.py',
             'chunked_measure.py', 'runtime.py', 'runtime_measure.py', 'native_reader.py')
    sources = {str((Path(__file__).parent / n).relative_to(ROOT)):
               hashlib.sha256((Path(__file__).parent / n).read_bytes()).hexdigest() for n in names}
    sources.update({n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
                    for n in ('lib/agent_core/task_runtime.py', 'lib/task_replay/task_replay.py')})
    report = dict(format='tofu.event-layout-crossover/v1', whole_system_verdict='incomplete',
                  production_promoted=False, scope=__doc__, samples=args.samples, pairs=args.pairs,
                  sources=sources, cases=[])
    for count in (64, 128, 256, 512, 1024, 2048, 4096):
        row = dict(count=count, pairs=[])
        report['cases'].append(row)
        for pair in range(args.pairs):
            result = {}
            for variant in (('reference', 'chunked') if pair % 2 == 0 else ('chunked', 'reference')):
                for n, digest in sources.items():
                    assert hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == digest
                command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__,
                           '--worker', '--variant', variant, '--count', str(count), '--samples', str(args.samples)]
                def invoke(memory=False):
                    completed = subprocess.run(command + (['--memory'] if memory else []),
                        check=True, capture_output=True, text=True, timeout=300)
                    return json.loads(completed.stdout)
                value, memory = invoke(), invoke(True)
                assert value['pid'] != memory['pid'] and value['output_trace_sha256'] == memory['output_trace_sha256']
                value.update(memory_pid=memory['pid'], peak_rss_bytes=memory['peak_rss_bytes'])
                result[variant] = value
            for key in ('output_trace_sha256', 'retained_events', 'retained_event_bytes'):
                assert result['reference'][key] == result['chunked'][key]
            row['pairs'].append(result)
            args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(count, pair, {key: round(result['chunked'][key] / result['reference'][key], 3)
                for key in ('p50_ms', 'p99_ms', 'cpu_ms_per_operation', 'peak_rss_bytes')}, flush=True)


if __name__ == '__main__':
    main()
