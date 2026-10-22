"""Controlled public scan/rank/render comparison for direct CSR ranking.

Entry: CLI --output outside checkout; worker subprocess mode. Independent
latency/memory processes, exact output traces, complete source witnesses. No
HTTP, durable task publication, live model or whole-system performance claim.
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from types import FunctionType

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from lib.repo_map import api
from sparse_rank import scores

CASES = ('small', 'dense', 'multilingual')
REFERENCE_REVISION = 'a90b3facc'


def reference_scores():
    """Immutable graph implementation, even after the candidate is promoted."""
    source = subprocess.check_output(['/usr/bin/git', 'show',
        REFERENCE_REVISION + ':lib/repo_map/rank.py'], cwd=ROOT)
    namespace = {'__name__': 'lib.repo_map._ranking_reference', '__package__': 'lib.repo_map'}
    exec(compile(source, '<frozen-repository-ranking>', 'exec'), namespace)
    return namespace['sumbd'], hashlib.sha256(source).hexdigest()


def fixture(root, case):
    count = 192 if case == 'dense' else 96 if case == 'multilingual' else 32
    for i in range(count):
        language = ('py', 'js', 'rs')[i % 3] if case == 'multilingual' else 'py'
        functions = []
        for j in range(12):
            name = f'symbol_{i}_{j}'
            target = f'symbol_{(i * 7 + j + 3) % count}_{(j + i) % 12}'
            functions.append({'py': f'def {name}():\n    return {target}()\n',
                              'js': f'function {name}() {{ return {target}(); }}\n',
                              'rs': f'fn {name}() -> i32 {{ {target}() }}\n'}[language])
        (root / f'module_{i}.{language}').write_text('\n'.join(functions))


def rss():
    return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')


def worker(variant, case, samples, memory, backend='sparse', library=None, library_sha256=None):
    native_kernel = None
    def native_scores(repo, chat_rel_paths=None):
        nonlocal native_kernel
        from native_csr import Kernel
        from native_repo_rank import scores as native_rank_scores
        if native_kernel is None:
            native_kernel = Kernel(library, library_sha256)
        return native_rank_scores(repo, chat_rel_paths, native_kernel)
    candidate = native_scores if backend == 'native' else scores
    generate = FunctionType(api.generate_repo_map.__code__,
        dict(api.generate_repo_map.__globals__, sumbd=reference_scores()[0] if variant == 'reference' else candidate),
        argdefs=api.generate_repo_map.__defaults__)
    generate.__kwdefaults__ = api.generate_repo_map.__kwdefaults__
    with tempfile.TemporaryDirectory(prefix='tofu-sparse-ranking-') as directory:
        root = Path(directory); fixture(root, case)
        def operation():
            return generate(str(root), chat_rel_paths={'module_0.py'})
        start = time.perf_counter_ns(); operation()
        cold_ms = (time.perf_counter_ns() - start) / 1e6
        for _ in range(20): operation()
        if memory: gc.collect()
        baseline = rss()
        wall, cpu = [], []
        trace = hashlib.sha256()
        for _ in range(samples):
            start = time.perf_counter_ns(); clock = time.process_time_ns()
            result = operation()
            elapsed_cpu = (time.process_time_ns() - clock) / 1e6
            elapsed_wall = (time.perf_counter_ns() - start) / 1e6
            if not memory:
                wall.append(elapsed_wall); cpu.append(elapsed_cpu)
            trace.update(json.dumps(result, sort_keys=True).encode()); trace.update(b'\0')
        del result
        if memory: gc.collect()
        ordered = sorted(wall)
        return {'pid': os.getpid(), 'trace_sha256': trace.hexdigest(),
                'cold_diagnostic_ms': cold_ms, 'warmup_operations': 20,
                'wall_samples_ms': wall, 'cpu_samples_ms': cpu,
                'p50_ms': statistics.median(wall) if wall else None,
                'p95_ms': ordered[int((samples - 1) * .95)] if wall else None,
                'p99_ms': ordered[int((samples - 1) * .99)] if wall else None,
                'cpu_ms_per_operation': statistics.mean(cpu) if cpu else None,
                'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                'baseline_rss_bytes': baseline, 'post_cleanup_rss_bytes': rss(),
                'retained_bytes': max(0, rss() - baseline)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--worker', choices=('reference', 'candidate'))
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--backend', choices=('sparse', 'native'), default='sparse')
    parser.add_argument('--library', type=Path)
    parser.add_argument('--library-sha256')
    args = parser.parse_args()
    if args.samples < 1 or args.pairs < 1: parser.error('positive samples and pairs required')
    if args.backend == 'native' and args.library is None: parser.error('native backend requires --library')
    if args.backend == 'native' and args.library_sha256 is None:
        args.library_sha256 = hashlib.sha256(args.library.read_bytes()).hexdigest()
    if args.worker:
        if not args.case: parser.error('--worker requires --case')
        print(json.dumps(worker(args.worker, args.case, args.samples, args.memory,
                                args.backend, args.library, args.library_sha256))); return
    if args.output is None: parser.error('--output required')
    names = ('benchmarks/repository_index/sparse_rank.py',
             'benchmarks/repository_index/sparse_rank_measure.py',
             'lib/repo_map/rank.py', 'lib/repo_map/api.py',
             'lib/repo_map/outline.py', 'lib/repo_map/render.py')
    if args.backend == 'native':
        names += ('benchmarks/repository_index/native_csr.py',
                  'benchmarks/repository_index/native_csr_kernel.rs',
                  'benchmarks/repository_index/native_repo_rank.py')
    report = {'format': 'tofu.repository-sparse-ranking/v1', 'scope': __doc__,
              'production_promoted': False, 'whole_system_verdict': 'incomplete',
              'sources': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names},
              'runtime': {'python': sys.version, 'platform': platform.platform(),
                          'affinity': sorted(os.sched_getaffinity(0))},
              'samples': args.samples, 'required_pairs': args.pairs, 'cases': []}
    report['backend'] = args.backend
    if args.backend == 'native':
        report['library'] = str(args.library.resolve())
        report['library_sha256'] = args.library_sha256
    report['reference_revision'] = REFERENCE_REVISION
    report['reference_source_sha256'] = reference_scores()[1]
    python = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    for case in CASES:
        row = {'case': case, 'pairs': []}; report['cases'].append(row)
        for pair in range(args.pairs):
            values = {}
            for variant in (('reference', 'candidate') if pair % 2 == 0 else ('candidate', 'reference')):
                command = [python, __file__, '--worker', variant, '--case', case, '--samples', str(args.samples)]
                command += ['--backend', args.backend]
                if args.backend == 'native':
                    command += ['--library', str(args.library.resolve()), '--library-sha256', args.library_sha256]
                latency = json.loads(subprocess.run(command, capture_output=True, text=True, check=True, timeout=300).stdout)
                memory = json.loads(subprocess.run(command + ['--memory'], capture_output=True, text=True, check=True, timeout=300).stdout)
                assert latency['pid'] != memory['pid']
                assert latency['trace_sha256'] == memory['trace_sha256']
                latency['memory'] = memory; values[variant] = latency
            assert values['reference']['trace_sha256'] == values['candidate']['trace_sha256'], case
            row['pairs'].append(values)
            args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        print(case, [round(p['candidate']['p50_ms'] / p['reference']['p50_ms'], 3) for p in row['pairs']], flush=True)


if __name__ == '__main__': main()
