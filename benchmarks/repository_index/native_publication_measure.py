"""Installed production Rust ranking versus canonical NetworkX in real tasks.

Both arms use current cursor extraction and unchanged registry/handler/API/event
publication. Child environments control only optional package availability;
worker receipts prove the selected backend after measured work. Reuses the
domain worker's five fixtures, warmup, raw clocks and independent memory cohort.
No live inference, HTTP, durable chat or long-run retention claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import FunctionType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
import domain_measure as domain
from lib.repo_map._native_rank import available_kernel
from benchmarks.system_performance import compare


def worker(variant, case, samples, memory):
    function = FunctionType(domain.worker.__code__,
        dict(domain.worker.__globals__, ReferenceExtractionPort=domain.CursorExtractionPort),
        argdefs=domain.worker.__defaults__)
    result = function(variant, case, samples, memory)
    selected = available_kernel() is not None
    assert selected == (variant == 'candidate'), 'native availability does not match arm'
    result['native_backend_selected'] = selected
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-prefix', type=Path)
    parser.add_argument('--native-package', type=Path)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--worker', choices=('reference', 'candidate'))
    parser.add_argument('--case', choices=domain.CASES)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--cases', nargs='+', choices=domain.CASES, default=domain.CASES)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.case, args.samples, args.memory))); return
    assert args.native_package and args.output_prefix and args.samples > 0 and args.pairs > 0
    native = args.native_package.resolve()
    base_paths = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep)
                  if p and Path(p).resolve() != native]
    environments = {v: dict(os.environ, PYTHONPATH=os.pathsep.join(
        ([str(native)] if v == 'candidate' else []) + base_paths))
        for v in ('reference', 'candidate')}
    package = native / 'tofu_repo_kernel'
    receipt = json.loads((package / 'build.json').read_text())
    assert receipt['source_sha256'] == hashlib.sha256((ROOT / 'packages/tofu-repo-kernel/rust/kernel.rs').read_bytes()).hexdigest()
    for filename in ('__init__.py', '_kernel.py'):
        assert (package / filename).read_bytes() == (ROOT / 'packages/tofu-repo-kernel/tofu_repo_kernel' / filename).read_bytes()
    assert hashlib.sha256((package / receipt['library']).read_bytes()).hexdigest() == receipt['library_sha256']
    python = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    checks = {}
    for name in ('check_publication_port.py', 'check_published_extraction.py'):
        done = subprocess.run([python, str(Path(__file__).parent / name)], env=environments['candidate'],
                              cwd=ROOT, check=True, capture_output=True, text=True, timeout=120)
        checks[name] = done.stdout.strip()
    names = [str(p.relative_to(ROOT)) for p in (ROOT / 'lib/repo_map').glob('*.py')]
    names += ['benchmarks/repository_index/' + name for name in
              ('native_publication_measure.py', 'domain_measure.py', 'domain_fixtures.py', 'publication_port.py',
               'published_extraction.py', 'task_measure.py', 'index.py', 'extraction_epoch.py')]
    names += ['packages/tofu-repo-kernel/' + name for name in
              ('rust/kernel.rs', 'tofu_repo_kernel/__init__.py', 'tofu_repo_kernel/_kernel.py', 'setup.py', 'pyproject.toml')]
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    hardware = domain.resource_budget_manifest(snapshot=domain.probe_system_resources())
    runtime = {'python': sys.version, 'platform': domain.platform.platform(),
               'extraction_packages': domain.loaded_identity()['packages']}
    reports = {v: {'format': 'tofu.system-performance/v1', 'scope': __doc__, 'sources': sources,
                   'native_package_receipt': receipt, 'check_receipts': checks, 'workloads': []}
               for v in environments}
    seen = set()
    for case in args.cases:
        rows = {}
        fixture_hash = hashlib.sha256(json.dumps(domain.fixture(case), sort_keys=True).encode()).hexdigest()
        for variant in reports:
            identity = hashlib.sha256(json.dumps({'sources': sources, 'variant': variant,
                'native_library_sha256': receipt['library_sha256']}, sort_keys=True).encode()).hexdigest()
            row = {'domain': 'repository', 'case': case, 'scope': 'domain_integration',
                   'correctness_passed': True, 'failure_and_cleanup_passed': True,
                   'identity': {'source_sha256': identity, 'runtime': runtime,
                                'hardware': hardware, 'fixture_sha256': fixture_hash}, 'measurements': []}
            rows[variant] = row; reports[variant]['workloads'].append(row)
        for pair in range(args.pairs):
            values = {}
            for variant in (('reference', 'candidate') if pair % 2 == 0 else ('candidate', 'reference')):
                assert sources == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
                command = [python, __file__, '--worker', variant, '--case', case, '--samples', str(args.samples)]
                def invoke(extra):
                    return json.loads(subprocess.run(command + extra, env=environments[variant], cwd=ROOT,
                        check=True, capture_output=True, text=True, timeout=300).stdout)
                value = invoke([]); memory = invoke(['--memory'])
                for result in (value, memory):
                    assert result['process_identity'] not in seen; seen.add(result['process_identity'])
                assert value['output_trace_sha256'] == memory['output_trace_sha256']
                value.update(peak_rss_bytes=memory['peak_rss_bytes'], retained_bytes=memory['retained_bytes'],
                    retention_baseline_rss_bytes=memory['retention_baseline_rss_bytes'],
                    post_cleanup_rss_bytes=memory['post_cleanup_rss_bytes'],
                    memory_process_identity=memory['process_identity'], memory_separate_process=True)
                values[variant] = value
            assert values['reference']['output_trace_sha256'] == values['candidate']['output_trace_sha256']
            for variant in reports:
                rows[variant]['measurements'].append(values[variant])
                Path(str(args.output_prefix) + '-' + variant + '.json').write_text(json.dumps(reports[variant], indent=2) + '\n')
            print(case, pair, round(values['candidate']['p50_ms'] / values['reference']['p50_ms'], 3), flush=True)
    verdict = compare(reports['reference'], reports['candidate'])
    Path(str(args.output_prefix) + '-verdict.json').write_text(json.dumps(verdict, indent=2) + '\n')
    print('repository cases complete', verdict['coverage'], verdict['verdict'], flush=True)


if __name__ == '__main__': main()
