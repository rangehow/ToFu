"""Actual AgentExecution event-page projection over list/native retained windows.

Seeded identical event dictionaries isolate the public read boundary; producers,
HTTP transport, durable recovery and native packaging are outside this scope.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SOURCES = ('benchmarks/native_event/public_measure.py',
           'benchmarks/native_event/check.py', 'tofu_agent/runtime.py',
           'lib/task_replay/task_replay.py', 'lib/agent_core/run_contract.py')


def worker(variant, count, case, samples, memory):
    from check import module
    from tofu_agent.models import AgentRequest
    from tofu_agent.runtime import AgentExecution
    events = [{'seq': index+17, 'type': 'delta',
               'content': f'event {index} 中文', 'evidence': {'fixture': [index]}}
              for index in range(count)]
    window = events
    if variant == 'native':
        window = module.ReplayWindow(count, count*1024)
        for event in events:
            window.append(event, 128)
    task = {'id': 'public-measure', 'events': window,
            'events_lock': threading.RLock(), 'status': 'running',
            '_eventBaseSeq': 17, '_eventNextSeq': 17+count}
    request = AgentRequest(messages=[{'role': 'user', 'content': 'offline fixture'}])
    execution = AgentExecution(None, task, request,
                               model='fixture', public_provider_id='fixture')
    cursor = {'stale': 0, 'tail': count+13, 'future': count+27}[case]
    reference_task = dict(task, events=events)
    reference = AgentExecution(None, reference_task, request,
                               model='fixture', public_provider_id='fixture')
    expected = reference.event_page(cursor=cursor)
    assert execution.event_page(cursor=cursor) == expected
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True,
                                      ensure_ascii=True).encode()).hexdigest()
    for _ in range(20):
        execution.event_page(cursor=cursor)
    raw = []
    for _ in range(samples):
        started = time.perf_counter_ns(); cpu = time.process_time_ns()
        page = execution.event_page(cursor=cursor)
        elapsed_cpu = time.process_time_ns()-cpu
        elapsed_wall = time.perf_counter_ns()-started
        if not memory:
            raw.append({'wall_ms': elapsed_wall/1e6, 'cpu_ms': elapsed_cpu/1e6})
        # Equality is outside measured intervals and runs for every public page.
        assert page == expected
        del page
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    assert not execution._async_waiters
    if variant == 'native':
        window.clear(); assert window.retained_bytes == 0
    task['events'] = []
    values = sorted(row['wall_ms'] for row in raw)
    return {'pid': os.getpid(), 'raw': raw, 'output_sha256': digest,
            'output_event_count': len(expected['events']),
            'peak_process_rss_bytes': peak,
            'p50_ms': statistics.median(values) if values else None,
            'p95_ms': values[int((samples-1)*.95)] if values else None,
            'p99_ms': values[int((samples-1)*.99)] if values else None,
            'cpu_ms_per_operation': statistics.mean(r['cpu_ms'] for r in raw) if raw else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--count', type=int)
    parser.add_argument('--case', choices=('stale', 'tail', 'future'))
    parser.add_argument('--worker', choices=('reference', 'native'))
    parser.add_argument('--memory', action='store_true')
    args = parser.parse_args()
    assert args.samples > 0 and args.pairs > 0
    if args.worker:
        print(json.dumps(worker(args.worker, args.count, args.case, args.samples, args.memory)))
        return
    assert args.output and args.library
    library = args.library.resolve()
    sources = {n: hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCES}
    library_digest = hashlib.sha256(library.read_bytes()).hexdigest()
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete',
              'python': sys.version, 'affinity': sorted(os.sched_getaffinity(0)),
              'sources': sources, 'library_sha256': library_digest,
              'samples': args.samples, 'cases': [],
              'memory_scope': 'Independent worker high-water includes imports, both fixture/oracle and read allocations.',
              'limitations': ['No producer/append timing, HTTP auth/SSE/backpressure or durable recovery.',
                              'Stale public event_page is unbounded; no HTTP page-size claim.',
                              'Exact page equality outside timing still affects process residency and inter-sample GC.']}
    identities = set()
    for count in ((args.count,) if args.count else (64, 4096, 65536)):
        for case in ((args.case,) if args.case else ('stale', 'tail', 'future')):
            pairs = []; report['cases'].append({'count': count, 'case': case, 'pairs': pairs})
            for pair in range(args.pairs):
                values = {}
                for variant in (('reference', 'native') if pair%2 == 0 else ('native', 'reference')):
                    for n, digest in sources.items():
                        assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest() == digest, n
                    assert hashlib.sha256(library.read_bytes()).hexdigest() == library_digest
                    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable),
                               __file__, '--worker', variant, '--count', str(count),
                               '--case', case, '--samples', str(args.samples)]
                    environment = dict(os.environ, TOFU_NATIVE_EVENT_LIBRARY=str(library))
                    result = json.loads(subprocess.run(command, env=environment, check=True,
                                        capture_output=True, text=True, timeout=1800).stdout)
                    memory = json.loads(subprocess.run(command+['--memory'], env=environment, check=True,
                                        capture_output=True, text=True, timeout=1800).stdout)
                    for pid in (result['pid'], memory['pid']):
                        assert pid not in identities; identities.add(pid)
                    assert result['output_sha256'] == memory['output_sha256']
                    assert all(math.isfinite(r[k]) and r[k] > 0
                               for r in result['raw'] for k in ('wall_ms', 'cpu_ms'))
                    result['memory_pid'] = memory['pid']
                    result['peak_process_rss_bytes'] = memory['peak_process_rss_bytes']
                    values[variant] = result
                assert values['reference']['output_sha256'] == values['native']['output_sha256']
                pairs.append(values)
                args.output.write_text(json.dumps(report, indent=2)+'\n')
                print(count, case, pair, round(values['native']['p50_ms']/values['reference']['p50_ms'], 3), flush=True)
    for n, digest in sources.items():
        assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest() == digest, n
    assert hashlib.sha256(library.read_bytes()).hexdigest() == library_digest


if __name__ == '__main__':
    main()
