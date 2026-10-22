"""Paired scan/rank/render measurements on a frozen, real Tofu source corpus.

Snapshot preparation and correctness checks are outside measured operations.
Each worker owns a private copy; edits cannot touch the application checkout.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from index import Index, outline
from lib.repo_map import rank
from lib.repo_map.render import render_map
from reference_tags import extract_reference, REFERENCE_SHA256, REVISION

REPOSITORY = Path(__file__).resolve().parents[2]
SCOPE = ('lib/repo_map', 'lib/knowledge', 'frontend/src/runtime/sections')
OWNER = 900001


def snapshot(destination):
    paths = subprocess.check_output(
        ['/usr/bin/git', 'ls-files', '-z', '--', *SCOPE], cwd=REPOSITORY
    ).decode().split('\0')
    manifest = {}
    for relative in paths:
        if not relative or not outline._is_code_path(relative) or outline._is_generated(relative):
            continue
        content = (REPOSITORY / relative).read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest[relative] = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    assert manifest
    return manifest


def worker(source, variant, case, samples, memory=False, reference_extractor=None):
    with tempfile.TemporaryDirectory(prefix='tofu-real-index-worker-') as folder:
        root = Path(folder) / 'source'
        shutil.copytree(source, root)
        cache = Index(OWNER, str(root), 64 * 1024 * 1024)
        edited = root / 'lib/repo_map/outline.py'
        original = edited.read_text()
        focus = {'lib/repo_map/outline.py'}

        def operation(index):
            if case == 'edited':
                edited.write_text(original + f'\ndef benchmark_changed_{index}():\n    return scan\n')
            if case == 'cold':
                cache.close()
            repo = cache.scan(OWNER) if variant == 'indexed' else outline.scan(str(root))
            scores = rank.ensure_scores(repo, rank.sumbd(repo, focus))
            return render_map(repo, scores)

        # Independent exact oracle: result containers, scores and final rendering.
        for index in (-3, -2, -1):
            operation(index)
            if reference_extractor is None:
                expected = outline.scan(str(root))
            else:
                with patch.object(outline, '_extract_treesitter_tags', new=reference_extractor):
                    expected = outline.scan(str(root))
            actual = cache.scan(OWNER)
            assert actual == expected
            expected_scores = rank.ensure_scores(expected, rank.sumbd(expected, focus))
            actual_scores = rank.ensure_scores(actual, rank.sumbd(actual, focus))
            assert actual_scores == expected_scores
            assert render_map(actual, actual_scores) == render_map(expected, expected_scores)
        cache.close()
        cold_start = time.perf_counter_ns()
        cold_cpu = time.process_time_ns()
        operation(0)
        first = {'wall_ms': (time.perf_counter_ns() - cold_start) / 1e6,
                 'cpu_ms': (time.process_time_ns() - cold_cpu) / 1e6}
        for index in range(1, 21):
            operation(index)
        trace = hashlib.sha256()
        raw = []
        for index in range(21, 21 + (20 if memory else samples)):
            start = time.perf_counter_ns()
            cpu = time.process_time_ns()
            output = operation(index)
            raw.append({'wall_ms': (time.perf_counter_ns() - start) / 1e6,
                        'cpu_ms': (time.process_time_ns() - cpu) / 1e6})
            trace.update(json.dumps(output, sort_keys=True).encode())
            trace.update(b'\0')
        wall = sorted(row['wall_ms'] for row in raw)
        result = {'p50_ms': statistics.median(wall),
                  'p95_ms': wall[int((len(wall) - 1) * .95)],
                  'p99_ms': wall[int((len(wall) - 1) * .99)],
                  'cpu_ms_per_operation': statistics.mean(row['cpu_ms'] for row in raw),
                  'process_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                  'cache_bytes': cache.retained_bytes, 'hits': cache.hits, 'misses': cache.misses,
                  'first_scan_diagnostic': first, 'trace_sha256': trace.hexdigest(),
                  'raw': [] if memory else raw, 'pid': os.getpid()}
        cache.close()
        assert not cache.entries and cache.retained_bytes == 0
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', choices=('full', 'indexed', 'direct'))
    parser.add_argument('--candidate', choices=('indexed', 'direct'), default='indexed')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--case', choices=('cold', 'warm', 'edited'))
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker:
        if args.worker == 'direct':
            from direct_tags import extract
            assert extract is outline._extract_treesitter_tags, 'candidate must be the production callable'
            # Compare the untouched reference on the exact snapshot before
            # installing the experiment seam. No timed operation is profiled.
            for relative in outline.list_files(str(args.source)):
                if relative.endswith('.py') or not outline._is_code_path(relative) or outline._is_generated(relative):
                    continue
                source = outline._read_code_source(str(args.source), relative)
                if source is not None and source[1]:
                    assert extract(relative, source[0]) == extract_reference(relative, source[0])
            # A Mock side_effect records complete source arguments, distorting
            # both allocation and retained memory. Install the actual function.
            with patch.object(outline, '_extract_treesitter_tags', new=extract):
                result = worker(args.source, 'full', args.case, args.samples, args.memory, extract_reference)
        elif args.worker == 'full' and args.candidate == 'direct':
            with patch.object(outline, '_extract_treesitter_tags', new=extract_reference):
                result = worker(args.source, 'full', args.case, args.samples, args.memory)
        else:
            result = worker(args.source, args.worker, args.case, args.samples, args.memory)
        print(json.dumps(result))
        return
    assert args.samples > 0 and args.pairs > 0 and args.output
    with tempfile.TemporaryDirectory(prefix='tofu-real-index-snapshot-') as folder:
        source = Path(folder)
        report = {'format': 'tofu.repository-index/v2',
                  'scope': 'real Tofu retained source snapshot; canonical scan/rank/render',
                  'whole_system_verdict': 'incomplete', 'python': sys.version,
                  'platform': platform.platform(), 'affinity': sorted(os.sched_getaffinity(0)),
                  'revision': subprocess.check_output(['/usr/bin/git', 'rev-parse', 'HEAD'], cwd=REPOSITORY).decode().strip(),
                  'dependencies': {name: importlib.metadata.version(name) for name in
                                   ('grep-ast', 'tree-sitter', 'tree-sitter-language-pack', 'networkx', 'scipy')},
                  'corpus': snapshot(source), 'samples': args.samples, 'pairs': args.pairs,
                  'candidate': args.candidate,
                  'reference_revision': REVISION, 'reference_function_sha256': REFERENCE_SHA256,
                  'function_replacement_records_calls': False,
                  'quantile_rule': 'floor((n - 1) * percentile)',
                  'memory_scope': 'separate process peak including imports, fixture copy and oracle; not live-app retained RSS',
                  'memory_observations': 20,
                  'source_sha256': {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                    for name in ('index.py', 'real_measure.py', 'direct_tags.py', 'reference_tags.py')},
                  'production_sources': {relative: hashlib.sha256((REPOSITORY / relative).read_bytes()).hexdigest()
                                         for relative in ('lib/repo_map/outline.py', 'lib/repo_map/api.py',
                                                          'lib/repo_map/rank.py', 'lib/repo_map/render.py')}, 'cases': []}
        print('corpus', len(report['corpus']), sum(row['bytes'] for row in report['corpus'].values()), flush=True)
        for case in ((args.case,) if args.case else ('cold', 'warm', 'edited')):
            pairs = []
            report['cases'].append({'case': case, 'pairs': pairs})
            for pair in range(args.pairs):
                result = {}
                for variant in (('full', args.candidate) if pair % 2 == 0 else (args.candidate, 'full')):
                    for relative, digest in report['production_sources'].items():
                        assert hashlib.sha256((REPOSITORY / relative).read_bytes()).hexdigest() == digest, relative
                    for name, digest in report['source_sha256'].items():
                        assert hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() == digest, name
                    command = [os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable), __file__,
                               '--worker', variant, '--source', str(source), '--case', case,
                               '--candidate', args.candidate,
                               '--samples', str(args.samples)]
                    done = subprocess.run(command, check=True, capture_output=True, text=True, timeout=1800)
                    result[variant] = json.loads(done.stdout)
                    done = subprocess.run(command + ['--memory'], check=True, capture_output=True, text=True, timeout=1800)
                    memory = json.loads(done.stdout)
                    result[variant]['process_peak_rss_bytes'] = memory['process_peak_rss_bytes']
                    result[variant]['memory_pid'] = memory['pid']
                assert result['full']['trace_sha256'] == result[args.candidate]['trace_sha256']
                pairs.append(result)
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                print(case, pair, 'p50_ratio', round(result[args.candidate]['p50_ms'] / result['full']['p50_ms'], 3), flush=True)


if __name__ == '__main__':
    main()
