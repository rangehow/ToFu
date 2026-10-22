#!/usr/bin/env python3
"""Paired extraction experiment; no timing profiler or allocation tracker.

Entry point: CLI --reference-source <baseline outline.py> --output <artifact>.
The reference is loaded under the same interpreter/dependencies. Raw AB/BA
latency and CPU samples, source identities, output equivalence and separate
allocation observations are retained. This is an extraction microbenchmark,
not repository-tool or agent end-to-end admission evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.repo_map import outline


def source_fixture(definitions: int) -> str:
    return '\n'.join(
        f'@decorate\ndef symbol_{i}(value: str = "中文"):\n'
        f'    """A synthetic definition with references."""\n'
        f'    return symbol_{(i + 1) % definitions}(value)\n'
        for i in range(definitions))


def percentile(samples: list[float], fraction: float) -> float:
    return sorted(samples)[min(len(samples) - 1, int((len(samples) - 1) * fraction))]


def measure(reference_path: Path, samples: int, pairs: int) -> dict:
    spec = importlib.util.spec_from_file_location('lib.repo_map.extraction_reference', reference_path)
    reference = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reference
    spec.loader.exec_module(reference)
    variants = {'reference': reference._extract_python_tags,
                'candidate': outline._extract_python_tags}
    workloads = []
    for definitions in (8, 128, 512):
        source = source_fixture(definitions)
        expected = [asdict(tag) for tag in variants['reference']('fixture.py', source)]
        observed = [asdict(tag) for tag in variants['candidate']('fixture.py', source)]
        if observed != expected:
            raise AssertionError('Extraction output mismatch')
        result = {'definitions': definitions, 'source_bytes': len(source.encode()),
                  'output_sha256': hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
                  'correctness': 'equivalent', 'pairs': []}
        for pair in range(pairs):
            raw = {name: {'wall_ms': [], 'cpu_ms': []} for name in variants}
            for operation in variants.values():
                operation('fixture.py', source)
            for iteration in range(samples):
                order = ('reference', 'candidate') if iteration % 2 == 0 else ('candidate', 'reference')
                for name in order:
                    cpu_start = time.process_time_ns()
                    started = time.perf_counter_ns()
                    variants[name]('fixture.py', source)
                    raw[name]['wall_ms'].append((time.perf_counter_ns() - started) / 1e6)
                    raw[name]['cpu_ms'].append((time.process_time_ns() - cpu_start) / 1e6)
            measurements = {}
            for name, values in raw.items():
                wall = values['wall_ms']
                measurements[name] = {
                    'p50_ms': statistics.median(wall),
                    'p95_ms': percentile(wall, .95), 'p99_ms': percentile(wall, .99),
                    'cpu_ms_per_operation': statistics.mean(values['cpu_ms']),
                    'throughput_per_second': len(wall) * 1000 / sum(wall),
                    'raw': values}
            result['pairs'].append(measurements)
        # Allocation instrumentation is deliberately outside timed samples.
        result['allocation_peak_bytes'] = {}
        for name, operation in variants.items():
            tracemalloc.start()
            operation('fixture.py', source)
            result['allocation_peak_bytes'][name] = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        workloads.append(result)
    return {
        'format': 'tofu.repository-extraction/v1', 'scope': 'microbenchmark',
        'samples_per_pair': samples, 'paired_rounds_in_one_process': pairs,
        'runtime': {'python': sys.version, 'platform': platform.platform(),
                    'cpu_affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None},
        'sources': {name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in [('reference', reference_path), ('candidate', Path(outline.__file__))]},
        'workloads': workloads}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    args = parser.parse_args()
    if args.samples < 100 or args.pairs < 3:
        parser.error('P99 comparison requires at least 100 samples and three pairs')
    result = measure(args.reference_source, args.samples, args.pairs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    for workload in result['workloads']:
        ratios = [p['candidate']['p50_ms'] / p['reference']['p50_ms'] for p in workload['pairs']]
        print(f"definitions={workload['definitions']} median ratios={','.join(f'{r:.3f}' for r in ratios)} correctness=equivalent")


if __name__ == '__main__':
    main()
