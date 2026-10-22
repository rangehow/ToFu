#!/usr/bin/env python3
"""Independent-process comparison of HTML metadata and combined extraction.

The reference is a retained html_extract.py supplied explicitly. Source/fixture
identity, exact text/date equality, raw AB/BA latency/CPU samples and separate
RSS observations are recorded. The trafilatura safety lock stays in both paths.
No network, model call or durable user data is involved.
"""
from __future__ import annotations

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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packages/tofu-search'))
from tofu_search.fetch import html_extract
from benchmarks.repository_pipeline import load_reference, retained_size


def variants(reference, fixture, combined):
    previous = load_reference(reference, 'publication_reference')
    source = fixture.read_text()
    def operation(module):
        date = module.extract_html_publish_date(source)
        if combined:
            return module.extract_html_text(source, 0, url='https://fixture.example/article'), date
        return date
    return {'reference': lambda: operation(previous), 'candidate': lambda: operation(html_extract)}


def worker(args):
    operations = variants(args.reference_source, args.fixture, args.combined)
    identity = uuid.uuid4().hex
    def prepare():
        if args.combined:
            # The upstream dedup cache spans calls; compare the same fresh
            # document state instead of letting AB order change extracted text.
            from trafilatura.deduplication import LRU_TEST
            LRU_TEST.clear()
    if args.memory_variant:
        prepare()
        value = operations[args.memory_variant]()
        gc.collect()
        args.output.write_text(json.dumps({'process_identity': identity,
            'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
            'retained_output_bytes': retained_size(value)}))
        return
    prepare()
    expected = operations['reference']()
    prepare()
    assert expected == operations['candidate'](), 'HTML text/date mismatch'
    if args.combined:
        assert isinstance(expected[0], str) and len(expected[0]) > 100, 'Missing combined body extraction'
    for _ in range(20):
        for operation in operations.values():
            prepare()
            operation()
    raw = {name: {'wall_samples_ms': [], 'cpu_samples_ms': []} for name in operations}
    for iteration in range(args.samples):
        order = ('reference', 'candidate') if iteration % 2 == 0 else ('candidate', 'reference')
        for name in order:
            prepare()
            cpu = time.process_time_ns()
            started = time.perf_counter_ns()
            value = operations[name]()
            wall = (time.perf_counter_ns() - started) / 1e6
            elapsed_cpu = (time.process_time_ns() - cpu) / 1e6
            assert value == expected, 'Extraction changed across samples'
            raw[name]['wall_samples_ms'].append(wall)
            raw[name]['cpu_samples_ms'].append(elapsed_cpu)
    output = {}
    for name, values in raw.items():
        wall = values['wall_samples_ms']; ordered = sorted(wall)
        output[name] = {**values, 'process_identity': identity,
            'p50_ms': statistics.median(wall), 'p95_ms': ordered[int((args.samples - 1) * .95)],
            'p99_ms': ordered[int((args.samples - 1) * .99)],
            'cpu_ms_per_operation': statistics.mean(values['cpu_samples_ms']),
            'throughput_per_second': args.samples * 1000 / sum(wall)}
    args.output.write_text(json.dumps({'correctness': 'equivalent', 'measurements': output}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--combined', action='store_true')
    parser.add_argument('--memory-variant', choices=['reference', 'candidate'], default='')
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if args.samples < 100 or args.pairs < 3:
        parser.error('100 samples and three independent process pairs required')
    report = {'format': 'tofu.publication-metadata-experiment/v1', 'whole_system_verdict': 'incomplete',
              'scope': 'publication metadata and combined HTML extraction, not fetch/PDF throughput',
              'body_duplicate_cache': 'reset in isolated process before each operation, outside timing',
              'runtime': {'python': sys.version, 'platform': platform.platform(), 'affinity': sorted(os.sched_getaffinity(0))},
              'sources': {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in
                          [('reference', args.reference_source), ('candidate', Path(html_extract.__file__)), ('runner', Path(__file__))]},
              'workloads': []}
    with tempfile.TemporaryDirectory(prefix='tofu-publication-measure-') as temporary:
        directory = Path(temporary)
        for case in ('article', 'json_ld', 'absent'):
            carrier = ('<meta property="article:published_time" content="2024-02-03T01:00:00+03:00">'
                       if case == 'article' else '<script type="application/ld+json">{"datePublished":"2024-02-03"}</script>'
                       if case == 'json_ld' else '')
            source = '<html><head>' + carrier + '</head><body><article>' + ''.join(
                f'<p>Paragraph {i}. Evidence about data processing and performance. 中文文本. '
                f'<a href="/source/{i}">source evidence</a></p>' for i in range(180)) + '</article></body></html>'
            fixture = directory / (case + '.html'); fixture.write_text(source)
            for combined in (False, True):
                row = {'case': case, 'combined': combined, 'fixture_sha256': hashlib.sha256(source.encode()).hexdigest(), 'pairs': []}
                base = [sys.executable, str(Path(__file__).resolve()), '--reference-source', str(args.reference_source.resolve()),
                        '--fixture', str(fixture), '--samples', str(args.samples), '--worker'] + (['--combined'] if combined else [])
                for pair in range(args.pairs):
                    target = directory / 'pair.json'
                    subprocess.run(base + ['--output', str(target)], check=True, timeout=300)
                    measured = json.loads(target.read_text())
                    for name in ('reference', 'candidate'):
                        subprocess.run(base + ['--output', str(target), '--memory-variant', name], check=True, timeout=300)
                        memory = json.loads(target.read_text())
                        measured['measurements'][name].update({
                            'peak_rss_bytes': memory['peak_rss_bytes'], 'retained_output_bytes': memory['retained_output_bytes'],
                            'memory_process_identity': memory['process_identity']})
                    row['pairs'].append(measured)
                    before, after = measured['measurements']['reference'], measured['measurements']['candidate']
                    print(case, combined, pair, json.dumps({key: after[key] / before[key] for key in
                          ('p50_ms', 'p99_ms', 'cpu_ms_per_operation', 'peak_rss_bytes')}), flush=True)
                report['workloads'].append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
