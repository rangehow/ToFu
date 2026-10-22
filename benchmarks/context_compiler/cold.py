"""First-build comparison, including immutable revision capture and cache fill.

Every operation creates a fresh compiler/history. Imports and fixture creation
are outside timing; this measures empty-cache first use, not interpreter boot.
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

from compiler import Compiler, History, Record, apply_wire_sanitize, encode
from measure import batch

OWNER = 900001
VARIANTS = ('full', 'incremental', 'bulk_capture')


def capture_history(rows, bulk=False):
    if bulk:
        return History(OWNER, tuple(Record.capture(row) for row in rows))
    history = History(OWNER)
    for row in rows:
        history = history.append(row)
    return history


def fixture(case):
    rows = [{'role': 'user', 'content': 'task'}]
    for index in range(1024 if case == 'long' else 128):
        rows.extend(batch(index, case == 'multimodal'))
    return rows


def operation(rows, variant):
    if variant == 'full':
        return encode(apply_wire_sanitize(rows, user_id=OWNER))
    compiler = Compiler(OWNER, 16 * 1024 * 1024)
    try:
        return compiler.compile(capture_history(rows, variant == 'bulk_capture'))
    finally:
        compiler.close()
        assert compiler.retained_bytes == 0 and not compiler.groups


def check():
    scenarios = [fixture('basic')[:9],
                 [{'role': 'user', 'content': 'first'}, {'role': 'user', 'content': 'second'}],
                 [{'role': 'tool', 'tool_call_id': 'orphan', 'content': 'result'}],
                 [{'role': 'assistant', 'tool_calls': [{'id': 'bad', 'function': {'name': '!'}}]}]]
    for rows in scenarios:
        assert capture_history(rows) == capture_history(rows, True)
        expected = operation(rows, 'full')
        assert all(operation(rows, variant) == expected for variant in VARIANTS)
    rows = fixture('basic')[:3]
    history = capture_history(rows, True)
    rows[0]['content'] = 'mutated after capture'
    assert json.loads(history.records[0].payload)['content'] == 'task'
    foreign = Compiler(OWNER + 1, 1024)
    try:
        foreign.compile(history)
    except PermissionError:
        pass
    else:
        raise AssertionError('foreign owner accepted')
    finally:
        foreign.close()
    print('first-build factory parity, malformed fallback, snapshot isolation and ownership passed')


def worker(variant, case, samples, memory=False):
    rows = fixture(case)
    expected = operation(rows, 'full')
    assert operation(rows, variant) == expected
    for _ in range(20):
        operation(rows, variant)
    raw = []
    trace = hashlib.sha256()
    for _ in range(20 if memory else samples):
        started = time.perf_counter_ns()
        cpu = time.process_time_ns()
        payload = operation(rows, variant)
        raw.append({'wall_ms': (time.perf_counter_ns() - started) / 1e6,
                    'cpu_ms': (time.process_time_ns() - cpu) / 1e6})
        assert payload == expected
        trace.update(payload)
        trace.update(b'\0')
    wall = sorted(row['wall_ms'] for row in raw)
    return {'pid': os.getpid(), 'messages': len(rows),
            'fixture_sha256': hashlib.sha256(encode(rows)).hexdigest(),
            'p50_ms': statistics.median(wall), 'p95_ms': wall[int((len(wall) - 1) * .95)],
            'p99_ms': wall[int((len(wall) - 1) * .99)],
            'cpu_ms_per_operation': statistics.mean(row['cpu_ms'] for row in raw),
            'process_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            'trace_sha256': trace.hexdigest(), 'raw': [] if memory else raw}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--worker', choices=VARIANTS)
    parser.add_argument('--case', choices=('basic', 'long', 'multimodal'))
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.check:
        check()
        return
    if args.worker:
        print(json.dumps(worker(args.worker, args.case, args.samples, args.memory)))
        return
    assert args.output and args.samples > 0 and args.pairs > 0
    sources = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
               for name in ('cold.py', 'compiler.py', 'measure.py')}
    report = {'format': 'tofu.context-first-build/v1', 'whole_system_verdict': 'incomplete',
              'scope': 'fresh revision capture, canonical sanitizer, serialization and initial cache fill; imports/fixtures excluded',
              'python': sys.version, 'platform': platform.platform(), 'affinity': sorted(os.sched_getaffinity(0)),
              'sources': sources, 'samples': args.samples, 'pairs': args.pairs,
              'memory_scope': 'separate process high-water including fixture and oracle; not live-app retained RSS', 'cases': []}
    for case in ((args.case,) if args.case else ('basic', 'long', 'multimodal')):
        pairs = []
        report['cases'].append({'case': case, 'pairs': pairs})
        for pair in range(args.pairs):
            result = {}
            for variant in (VARIANTS if pair % 2 == 0 else tuple(reversed(VARIANTS))):
                for name, digest in sources.items():
                    assert hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() == digest
                command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__,
                           '--worker', variant, '--case', case, '--samples', str(args.samples)]
                done = subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
                result[variant] = json.loads(done.stdout)
                done = subprocess.run(command + ['--memory'], check=True, capture_output=True, text=True, timeout=300)
                memory = json.loads(done.stdout)
                result[variant]['process_peak_rss_bytes'] = memory['process_peak_rss_bytes']
                result[variant]['memory_pid'] = memory['pid']
            assert len({row['fixture_sha256'] for row in result.values()}) == 1
            assert len({row['trace_sha256'] for row in result.values()}) == 1
            pairs.append(result)
            args.output.write_text(json.dumps(report, indent=2) + '\n')
            print(case, pair, {variant: round(result[variant]['p50_ms'] / result['full']['p50_ms'], 3)
                               for variant in VARIANTS}, flush=True)


if __name__ == '__main__':
    main()
