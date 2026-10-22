"""Native-heavy actual body/protocol comparison, changing one copy site only.

Borrow the verified worker accounting without mutating its module globals.
Synthetic opaque states are structural fixtures, not live cryptographic evidence.
"""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import types

import provider_measure
import native_copy
from lib.llm.body import _build


def fixture(profile, density):
    rows = native_copy.fixture(profile)
    if density != 'heavy':
        for index, row in enumerate(rows):
            if density == 'sparse' and index == len(rows)-2:
                continue
            for key in ('_responses_items', '_anthropic_content_blocks'):
                row.pop(key, None)
    return rows


def worker(variant, profile, samples, memory, density='heavy'):
    builder = _build.build_body if variant == 'reference' else native_copy.candidate_builder()
    namespace = dict(provider_measure.worker.__globals__)
    namespace['fixture'] = lambda selected: fixture(selected, density)
    namespace['project'] = lambda rows, protocol, feature: native_copy.project(
        rows, 'anthropic' if protocol == 'anthropic' else feature, builder)
    function = types.FunctionType(provider_measure.worker.__code__, namespace,
                                  argdefs=provider_measure.worker.__defaults__)
    # Raw producer input in both variants: no immutable-history capture or
    # materialization is added to either measured request path.
    return function('raw', 'first', profile, samples, memory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', choices=('reference', 'specialized'))
    parser.add_argument('--profile', choices=('openai', 'compatible', 'anthropic'))
    parser.add_argument('--density', choices=('heavy', 'sparse', 'none'))
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.profile, args.samples, args.memory, args.density or 'heavy')))
        return
    assert args.output and args.samples > 0 and args.pairs > 0
    root = Path(__file__).resolve().parents[2]
    names = ('benchmarks/context_compiler/native_copy_measure.py', 'benchmarks/context_compiler/native_copy.py',
             'benchmarks/context_compiler/provider_measure.py', 'benchmarks/context_compiler/compiler.py',
             'lib/llm/body/_build.py', 'lib/llm_sanitize/_fields.py',
             'lib/llm/responses_outbound/_to_responses.py', 'lib/llm/anthropic_outbound/_to_anthropic.py')
    digests = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete', 'python': sys.version,
              'platform': platform.platform(),
              'affinity': sorted(os.sched_getaffinity(0)), 'sources': digests, 'samples': args.samples,
              'checks': native_copy.check(), 'cases': [],
              'memory_scope': 'separate worker high-water including imports, fixtures and preflight',
              'fixture_sha256': {p+'/'+density: hashlib.sha256(json.dumps(fixture(p,density), sort_keys=True).encode()).hexdigest()
                                 for p in ('openai', 'compatible', 'anthropic') for density in ('heavy','sparse','none')}}
    for profile in ((args.profile,) if args.profile else ('openai', 'compatible', 'anthropic')):
        for density in ((args.density,) if args.density else ('heavy','sparse','none')):
            pairs = []; report['cases'].append({'profile': profile, 'density': density, 'pairs': pairs})
            for pair in range(args.pairs):
                values = {}
                for variant in (('reference', 'specialized') if pair%2 == 0 else ('specialized', 'reference')):
                    for name, digest in digests.items():
                        assert hashlib.sha256((root/name).read_bytes()).hexdigest() == digest, name
                    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__,
                               '--worker', variant, '--profile', profile, '--density', density, '--samples', str(args.samples)]
                    done = subprocess.run(command, check=True, capture_output=True, text=True, timeout=1800)
                    result = json.loads(done.stdout)
                    done = subprocess.run(command+['--memory'], check=True, capture_output=True, text=True, timeout=1800)
                    memory = json.loads(done.stdout)
                    assert result['pid'] != memory['pid']
                    assert result['trace_sha256'] == memory['trace_sha256']
                    result['memory_pid'] = memory['pid'];result['peak_process_rss_bytes'] = memory['peak_process_rss_bytes']
                    values[variant] = result
                    for name, digest in digests.items():
                        assert hashlib.sha256((root/name).read_bytes()).hexdigest() == digest, name
                assert values['reference']['trace_sha256'] == values['specialized']['trace_sha256']
                pairs.append(values);args.output.write_text(json.dumps(report, indent=2)+'\n')
                print(profile, density, pair, round(values['specialized']['p50_ms']/values['reference']['p50_ms'], 3), flush=True)


if __name__ == '__main__':
    main()
