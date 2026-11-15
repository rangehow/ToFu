#!/usr/bin/env python3
"""Measure equivalent test-index work on one immutable Git revision.

The ordinary selector and content-addressed index must produce identical full
reference sets. Alternate cold runs; report warm and explicitly scoped runs
separately. This measures discovery, not test execution or end-to-end delivery.
Temporary caches are removed; an optional JSON output is the only run artifact.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.project_mod import landing_verification as landing  # noqa: E402
from lib.project_mod import verification_index  # noqa: E402


def measure(root, revision='HEAD', repetitions=3):
    head = landing._git(root, 'rev-parse', '--verify', f'{revision}^{{commit}}')
    if head.returncode:
        raise ValueError(head.stderr.strip())
    head = head.stdout.strip()
    timings = {name: [] for name in ('ordinary_cold', 'content_cold', 'content_warm', 'scoped_cold')}
    with landing.merge_checkout(root, head) as checkout, tempfile.TemporaryDirectory() as temporary:
        selector = landing._load_selector(checkout)
        expected = None
        parsed = []
        for repeat in range(repetitions):
            cache = Path(temporary) / f'index-{repeat}.json'
            order = ('ordinary_cold', 'content_cold') if repeat % 2 == 0 else ('content_cold', 'ordinary_cold')
            for kind in order:
                started = time.perf_counter()
                if kind == 'ordinary_cold':
                    index = selector.build_index(str(checkout / 'tests'), None)
                else:
                    result = verification_index.build_index(root, head, selector, cache)
                    index = result.index
                    parsed.append(result.parsed)
                timings[kind].append(time.perf_counter() - started)
                refs = {path: row['refs'] for path, row in index.items()}
                if expected is None:
                    expected = refs
                if refs != expected:
                    raise AssertionError('index reference sets differ')
            started = time.perf_counter()
            warm = verification_index.build_index(root, head, selector, cache)
            timings['content_warm'].append(time.perf_counter() - started)
            assert warm.parsed == 0 and {p: row['refs'] for p, row in warm.index.items()} == expected
            first = sorted(expected)[0]
            started = time.perf_counter()
            scoped = verification_index.build_index(root, head, selector,
                Path(temporary) / f'scoped-{repeat}.json', [first])
            timings['scoped_cold'].append(time.perf_counter() - started)
            assert scoped.index == {first: {'refs': expected[first]}} and scoped.parsed == 1
        return {'revision': head, 'python': sys.version, 'files': len(expected),
                'equivalent_reference_sets': True, 'cold_blobs_parsed': parsed,
                'cache_bytes': cache.stat().st_size,
                'samples_seconds': timings,
                'median_seconds': {name: statistics.median(values) for name, values in timings.items()},
                'scope': 'Index preparation only; scoped mode selects one test file.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--into', default='.')
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error('--repetitions must be positive')
    result = measure(Path(args.into).resolve(), args.revision, args.repetitions)
    encoded = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return 0


if __name__ == '__main__':
    sys.exit(main())
