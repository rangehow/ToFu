"""Inert, test-only comparison of one public synthetic operation's output.

No producer is run: missing output fails, and a match attests neither Rust
execution nor source identity. Uses the delivered golden runner's comparator
and unchanged tolerance authority (docs/GOLDEN_DOCUMENTS.md).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from scripts import golden_documents as gd

CASE_ID = 'synthetic-pptx-import-fidelity'
OP_KEY = 'slides.import_pptx'


def _finite_number(text):
    number = float(text)
    if not math.isfinite(number):
        raise ValueError('non-finite candidate number')
    return number


def check_candidate(actual_dir: Path) -> list[str]:
    case = next(case for case in gd.load_json(gd.MANIFEST)['cases']
                if case['id'] == CASE_ID)
    if case['visibility'] != 'public':
        raise ValueError('the fixed synthetic case must remain public')
    op = next(op for op in case['operations'] if op['key'] == OP_KEY)
    reason = gd.availability({**case, 'operations': [op]}, private_root='')
    if reason is not None:
        raise ValueError(reason)
    input_path, expected_root = gd.case_paths(case, private_root='')
    expected_dir = str(Path(expected_root) / OP_KEY)
    root = actual_dir.resolve()
    corpus = Path(gd.CORPUS).resolve()
    if (root.is_relative_to(corpus) or corpus.is_relative_to(root)
            or not (root / 'result.json').resolve().is_relative_to(root)):
        raise ValueError('candidate output must be separate from corpus and locally contained')
    with (actual_dir / 'result.json').open(encoding='utf-8') as handle:
        actual = json.load(handle, parse_float=_finite_number, parse_constant=_finite_number)
    pending = [actual]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            if gd.BLOB not in value:
                pending.extend(value.values())
                continue
            if set(value) != {gd.BLOB, 'kind'}:
                raise ValueError('blob fields must be exactly $blob and kind')
            path = Path(value[gd.BLOB])
            if path.is_absolute() or not (root / path).resolve().is_relative_to(root):
                raise ValueError('blob must stay inside candidate directory')
    tolerances = gd.load_tolerances()
    tolerance = tolerances['operations'][op['op']]
    actual = gd.normalize_result(actual, str(actual_dir), input_path, tolerance)
    expected = gd.load_json(str(Path(expected_dir) / 'result.json'))
    diff = gd.Diff()
    gd.compare_value(expected, actual, '', {
        'expected_dir': expected_dir, 'actual_dir': str(actual_dir),
        'tolerance': tolerance, 'rules': tolerances['rules']}, diff)
    return diff.items


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-dir', required=True, type=Path,
                        help='already-produced slides.import_pptx output directory')
    args = parser.parse_args(argv)
    try:
        diffs = check_candidate(args.candidate_dir)
    except (OSError, ValueError, TypeError, KeyError, StopIteration, RecursionError) as error:
        print(f'REJECTED: {error}', file=sys.stderr)
        return 2
    if diffs:
        print('\n'.join(diffs))
        return 1
    print(f'MATCH {CASE_ID} {OP_KEY}: comparison only; Rust producer/source unverified')
    return 0


if __name__ == '__main__':
    sys.exit(main())
