"""Actual native request projection with immutable raw-source revision costs.

No network/model calls. Token counts are supplied: token admission, durable
history and interpreter startup remain outside this experiment's scope.
"""
import argparse
import copy
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

from compiler import History, Record
from check_provider import OWNER, project

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {'openai': ('responses', 'openai'),
            'compatible': ('responses', 'compatible'),
            'anthropic': ('anthropic', 'native')}


def fixture(profile):
    rows = []
    for index in range(128):
        rows.extend([{'role': 'user', 'content': f'question {index}'},
                     {'role': 'assistant', 'content': f'answer {index}'}])
    native = {'role': 'assistant', 'content': 'native answer'}
    if profile == 'anthropic':
        native['_anthropic_content_blocks'] = [
            {'type': 'thinking', 'thinking': 'fixture reasoning', 'signature': 'opaque-signature'},
            {'type': 'text', 'text': 'native answer'}]
    else:
        native['_responses_items'] = [{'type': 'reasoning', 'id': 'fixture-reasoning',
                                      'summary': [], 'encrypted_content': 'opaque-reasoning'}]
    return rows + [{'role': 'user', 'content': 'native question'}, native,
                   {'role': 'user', 'content': 'continue'}]


def capture(rows):
    return History(OWNER, tuple(Record.capture(row) for row in rows))


def worker(variant, case, profile, samples, memory=False):
    rows = fixture(profile); original = copy.deepcopy(rows)
    protocol, feature = PROFILES[profile]
    expected = project(rows, protocol, feature)
    assert project(capture(rows).materialize_provider_input(OWNER), protocol, feature) == expected
    assert rows == original
    mandatory_native = any(item.get('encrypted_content') for row in rows
                           for item in row.get('_responses_items', []) if isinstance(item, dict))
    if profile == 'compatible' and mandatory_native:
        assert expected['payload'] is None
        assert expected['validation_issues'] == ('mandatory_continuity_unrepresentable',)
    else:
        assert expected['payload'] is not None and not expected['validation_issues']
    history = capture(rows) if variant == 'source_ir' and case != 'first' else None

    def operation(index):
        nonlocal history
        if case == 'append':
            # Grow both producer histories by the same settled pair, including
            # capture/tuple revision cost in the source-IR arm.
            additions = [{'role': 'assistant', 'content': f'added answer {index}'},
                         {'role': 'user', 'content': f'added question {index}'}]
            rows.extend(additions)
            if history is not None:
                for row in additions:
                    history = history.append(row)
        elif case == 'native_edit':
            changed = copy.deepcopy(original[-2])
            if profile == 'anthropic':
                changed['_anthropic_content_blocks'][0]['signature'] = f'edited-signature-{index}'
            else:
                changed['_responses_items'][0]['encrypted_content'] = f'edited-reasoning-{index}'
            rows[-2] = changed
            if history is not None:
                history = history.replace(len(rows)-2, changed)
        if variant == 'raw':
            return project(rows, protocol, feature)
        current = capture(rows) if case == 'first' else history
        return project(current.materialize_provider_input(OWNER), protocol, feature)

    for index in range(20):
        operation(index)
    raw = []; trace = hashlib.sha256()
    try:
        # Append histories grow with operation count. Memory workers must run
        # the full latency cohort, rather than measuring a shorter history.
        for index in range(20, 20+samples):
            started = time.perf_counter_ns(); cpu = time.process_time_ns()
            result = operation(index)
            raw.append({'wall_ms': (time.perf_counter_ns()-started)/1e6,
                        'cpu_ms': (time.process_time_ns()-cpu)/1e6})
            trace.update(json.dumps(result, sort_keys=True).encode()); trace.update(b'\0')
        wall = sorted(row['wall_ms'] for row in raw)
        return {'pid': os.getpid(), 'p50_ms': statistics.median(wall),
                'p95_ms': wall[int((len(wall)-1)*.95)], 'p99_ms': wall[int((len(wall)-1)*.99)],
                'cpu_ms_per_operation': statistics.mean(row['cpu_ms'] for row in raw),
                'throughput_per_second': len(raw)*1000/sum(row['wall_ms'] for row in raw),
                'peak_process_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                'trace_sha256': trace.hexdigest(), 'raw': [] if memory else raw}
    finally:
        # Immutable revisions have no global registry/cache; dropping this
        # worker's final revision releases its owner-scoped source references.
        history = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', choices=('raw', 'source_ir'))
    parser.add_argument('--profile', choices=tuple(PROFILES))
    parser.add_argument('--case', choices=('first', 'append', 'native_edit'))
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.case, args.profile, args.samples, args.memory)))
        return
    assert args.output and args.samples > 0 and args.pairs > 0
    names = ('benchmarks/context_compiler/provider_measure.py', 'benchmarks/context_compiler/compiler.py',
             'benchmarks/context_compiler/check_provider.py', 'lib/llm/body/_build.py',
             'lib/llm/conversion.py', 'lib/llm/responses_outbound/_to_responses.py',
             'lib/llm/anthropic_outbound/_to_anthropic.py')
    digests = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    report = {'format': 'tofu.native-context-source-ir/v1', 'scope': __doc__,
              'whole_system_verdict': 'incomplete', 'python': sys.version,
              'platform': platform.platform(), 'affinity': sorted(os.sched_getaffinity(0)),
              'sources': digests, 'samples': args.samples, 'pairs': args.pairs,
              'fixture_digests': {p: hashlib.sha256(json.dumps(fixture(p), sort_keys=True).encode()).hexdigest()
                                  for p in PROFILES},
              'memory_scope': 'separate worker process high-water including imports, fixture/oracle and warmups',
              'retained_bytes_scope': 'not measured as a live application owner; no retained-memory admission claim',
              'cases': []}
    for profile in ((args.profile,) if args.profile else PROFILES):
        for case in ((args.case,) if args.case else ('first', 'append', 'native_edit')):
            pairs = []; report['cases'].append({'profile': profile, 'case': case, 'pairs': pairs})
            for pair in range(args.pairs):
                measurements = {}
                for variant in (('raw', 'source_ir') if pair%2 == 0 else ('source_ir', 'raw')):
                    for name, digest in digests.items():
                        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
                    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__,
                               '--worker', variant, '--profile', profile, '--case', case,
                               '--samples', str(args.samples)]
                    done = subprocess.run(command, check=True, capture_output=True, text=True, timeout=1800)
                    result = json.loads(done.stdout)
                    done = subprocess.run(command+['--memory'], check=True, capture_output=True, text=True, timeout=1800)
                    memory = json.loads(done.stdout)
                    result['memory_pid'] = memory['pid']
                    result['peak_process_rss_bytes'] = memory['peak_process_rss_bytes']
                    measurements[variant] = result
                assert measurements['raw']['trace_sha256'] == measurements['source_ir']['trace_sha256']
                pairs.append(measurements)
                args.output.write_text(json.dumps(report, indent=2)+'\n')
                print(profile, case, pair, round(measurements['source_ir']['p50_ms']/measurements['raw']['p50_ms'], 3), flush=True)


if __name__ == '__main__':
    main()
