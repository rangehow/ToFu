"""Native public-reader snapshot kernel: old sequence fallback vs explicit iterator.

Independent processes/libraries, exact event identity/order, wall/CPU samples.
This does not admit live transport, durability or the whole event domain.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time


def worker(count, samples, memory):
    from check import module
    window = module.ReplayWindow(count, count*64)
    events = [{'seq': index, 'content': 'snapshot fixture'} for index in range(count)]
    for event in events:
        window.append(event, 32)
    snapshot = list(window)
    assert snapshot == events and all(a is b for a, b in zip(snapshot, events))
    del snapshot
    for _ in range(20):
        list(window)
    raw = []
    for _ in range(samples):
        started = time.perf_counter_ns(); cpu = time.process_time_ns()
        snapshot = list(window)
        raw.append({'wall_ms': (time.perf_counter_ns()-started)/1e6,
                    'cpu_ms': (time.process_time_ns()-cpu)/1e6})
        assert len(snapshot) == count and snapshot[0] is events[0] and snapshot[-1] is events[-1]
        del snapshot
    wall = sorted(row['wall_ms'] for row in raw)
    window.clear();assert window.retained_bytes == 0
    return {'pid': os.getpid(), 'p50_ms': statistics.median(wall),
            'explicit_snapshot_iterator': hasattr(module.ReplayWindow, '__iter__'),
            'p95_ms': wall[int((len(wall)-1)*.95)], 'p99_ms': wall[int((len(wall)-1)*.99)],
            'cpu_ms_per_operation': statistics.mean(row['cpu_ms'] for row in raw),
            'process_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'raw': [] if memory else raw}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path);parser.add_argument('--candidate', type=Path)
    parser.add_argument('--worker', action='store_true');parser.add_argument('--memory', action='store_true')
    parser.add_argument('--count', type=int);parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3);parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.count, args.samples, args.memory)));return
    assert args.output and args.reference and args.candidate and args.samples > 0 and args.pairs > 0
    paths = {'reference': args.reference.resolve(), 'candidate': args.candidate.resolve()}
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete', 'python': sys.version,
              'affinity': sorted(os.sched_getaffinity(0)), 'libraries': digests,
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'samples': args.samples, 'cases': [],
              'memory_scope': 'separate process high-water; includes imports, fixture, warmup and kernel allocations'}
    for count in ((args.count,) if args.count else (4096, 65536)):
        pairs = [];report['cases'].append({'count': count, 'pairs': pairs})
        for pair in range(args.pairs):
            values = {}
            for variant in (('reference','candidate') if pair%2 == 0 else ('candidate','reference')):
                assert hashlib.sha256(paths[variant].read_bytes()).hexdigest() == digests[variant]
                command = [os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable), __file__,
                           '--worker','--count',str(count),'--samples',str(args.samples)]
                environment = dict(os.environ,TOFU_NATIVE_EVENT_LIBRARY=str(paths[variant]))
                done = subprocess.run(command,env=environment,check=True,capture_output=True,text=True,timeout=1800)
                result = json.loads(done.stdout)
                done = subprocess.run(command+['--memory'],env=environment,check=True,capture_output=True,text=True,timeout=1800)
                memory = json.loads(done.stdout);assert result['pid'] != memory['pid']
                result['memory_pid'] = memory['pid'];result['process_peak_rss_bytes'] = memory['process_peak_rss_bytes']
                values[variant] = result
                assert result['explicit_snapshot_iterator'] == (variant == 'candidate')
            pairs.append(values);args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(count,pair,round(values['candidate']['p50_ms']/values['reference']['p50_ms'],3),flush=True)


if __name__ == '__main__':main()
