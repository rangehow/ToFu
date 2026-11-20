"""Bounded comparison only; no producer execution or native provenance claim.

The only authority is the tracked synthetic full/slide-2 golden operations.
Warnings and slide features are excluded, not certified empty or equivalent.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from scripts import golden_documents as gd
from tests.golden_documents.check_rust_document_candidate import CASE_ID, _finite_number

OP_KEYS = ('slides.import_pptx', 'slides.import_pptx-slide-2')
MANIFEST_FIELDS = ('pages', 'size', 'title', 'version', 'theme')


def project(value):
    manifest = value['deck']['deck.pptd']
    return {'report': {key: value['report'][key] for key in ('pages', 'theme')},
            'deck': {'deck.pptd': {key: manifest[key] for key in MANIFEST_FIELDS}}}


def _load(directory):
    with (Path(directory) / 'result.json').open(encoding='utf-8') as handle:
        return json.load(handle, parse_float=_finite_number, parse_constant=_finite_number,
                         object_pairs_hook=_unique_object)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate field: {key}')
        result[key] = value
    return result


def _exact_types(expected, actual, pointer=''):
    if type(actual) is not type(expected):
        raise ValueError(f'{pointer}: expected exact {type(expected).__name__}')
    if isinstance(expected, dict):
        if actual.keys() != expected.keys():
            raise ValueError(f'{pointer}: missing or unexpected fields')
        for key in expected:
            _exact_types(expected[key], actual[key], f'{pointer}/{key}')
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            raise ValueError(f'{pointer}: wrong array length')
        for index, (e, a) in enumerate(zip(expected, actual)):
            _exact_types(e, a, f'{pointer}/{index}')
    elif isinstance(actual, float) and not math.isfinite(actual):
        raise ValueError(f'{pointer}: non-finite number')


def _separate(directory):
    root = Path(directory).resolve()
    corpus = Path(gd.CORPUS).resolve()
    if (root.is_relative_to(corpus) or corpus.is_relative_to(root)
            or not (root / 'result.json').resolve().is_relative_to(root)):
        raise ValueError('output must be separate from corpus and locally contained')
    return root


def check_metadata(op_key, candidate_dir, *, python_dir=None):
    """Return differences, or reject invalid input with ValueError/IO/JSON errors.

    Candidate must be exactly the allowlisted projection. Optional already-produced
    Python output is projected separately and checked against frozen output first.
    Neither directory's presence certifies producer execution or source identity.
    """
    if op_key not in OP_KEYS:
        raise ValueError('only synthetic full-import and slide-2 operations are allowed')
    case = next(c for c in gd.load_json(gd.MANIFEST)['cases'] if c['id'] == CASE_ID)
    if case['visibility'] != 'public':
        raise ValueError('fixed synthetic case must remain public')
    ops = [next(o for o in case['operations'] if o['key'] == key) for key in OP_KEYS]
    reason = gd.availability({**case, 'operations': ops}, private_root='')
    if reason is not None:
        raise ValueError(reason)
    _, expected_root = gd.case_paths(case, private_root='')
    frozen = project(_load(Path(expected_root) / op_key))
    candidate = _load(_separate(candidate_dir))
    python = project(_load(_separate(python_dir))) if python_dir is not None else None
    tolerance = gd.load_tolerances()
    ctx = {'tolerance': tolerance['operations']['slides.import_pptx'],
           'rules': tolerance['rules']}
    diff = gd.Diff()
    for label, value in [('frozen', frozen), ('python', python), ('candidate', candidate)]:
        if label == 'python' and python_dir is None:
            continue
        _exact_types(frozen, value)
        manifest = value['deck']['deck.pptd']
        if value['report']['pages'] != len(manifest['pages']):
            raise ValueError(f'{label}: page count differs from selected paths')
        if value['report']['theme'] != manifest['theme']:
            raise ValueError(f'{label}: report and manifest themes differ')
    if python is not None:
        python_diff = gd.Diff()
        gd.compare_value(frozen, python, '', ctx, python_diff)
        diff.items.extend(f'python-to-frozen {item}' for item in python_diff.items)
    gd.compare_value(python if python is not None else frozen, candidate, '', ctx, diff)
    return diff.items
