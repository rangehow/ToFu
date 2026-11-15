"""The token-unit facts generator measures persisted rounds faithfully.

``scripts/measure_token_units.py`` derives ``lib/model_info/token_unit_facts.json``
from conversation dumps. Rows recorded before ``admitted_local_tokens`` existed
carried the local count in ``admitted_input_tokens``; newer rows carry the
serving-unit size there and must be measured from ``admitted_local_tokens``.
"""
from __future__ import annotations

import json

import pytest

from lib.model_info.token_units import validate_token_unit_catalog
from scripts.measure_token_units import measure

pytestmark = pytest.mark.unit


def _round(tag, model, usage, *, started_ns=1_762_819_200_000_000_000):
    usage = {**usage, '_dispatch': {'logical_model': model,
                                    'stream_started_at_unix_ns': started_ns}}
    return {'round': tag, 'tag': tag, 'model': model, 'usage': usage}


def _dump(tmp_path, name, rounds):
    path = tmp_path / f'{name}.json'
    path.write_text(json.dumps({'conversationId': name, 'messages': [
        {'role': 'assistant', '_attemptId': 'attempt', 'apiRounds': rounds}]}))
    return path


def test_rows_are_measured_from_the_local_count_of_their_era(tmp_path):
    old = _dump(tmp_path, 'old-era', [
        # Before 2026-11-11: admitted_input_tokens was the local count.
        _round('R1', 'claude-opus-5.5', {
            'prompt_tokens': 15_000, '_request_identity': {'admitted_input_tokens': 10_000}}),
    ])
    new = _dump(tmp_path, 'new-era', [
        # Now: admitted_input_tokens is serving units; the local count is separate.
        _round('R1', 'claude-opus-5.5', {
            'prompt_tokens': 12_000, '_request_identity': {
                'admitted_input_tokens': 19_751, 'admitted_local_tokens': 10_000}}),
        # Provider-owned (native compaction) input is not comparable.
        _round('R2', 'claude-opus-5.5', {
            'prompt_tokens': 90_000, 'effective_prompt_tokens': 1_000,
            '_request_identity': {'admitted_local_tokens': 10_000}}),
        # Anthropic convention: cache reads sit outside input_tokens.
        _round('R3', 'kimi-k3.1-pro', {
            'input_tokens': 1_000, 'cache_read_input_tokens': 9_333,
            '_request_identity': {'admitted_local_tokens': 10_000}}),
    ])

    families = measure([old, new])

    claude = families['claude']
    assert claude['provider_per_local'] == {'min': 1.2, 'max': 1.5}
    assert claude['samples'] == 2 and claude['conversations'] == 2
    assert claude['members'] == {'claude-opus-5.5': 2}
    assert families['kimi']['provider_per_local'] == {'min': 1.0333, 'max': 1.0334}
    validate_token_unit_catalog({
        'contract': 'tofu.token-unit-facts/v1', 'definition': 'test',
        'generator': 'scripts/measure_token_units.py', 'families': families})


def test_repeated_rounds_of_one_attempt_count_once(tmp_path):
    row = _round('R1', 'kimi-k3.1-pro', {
        'prompt_tokens': 10_400, '_request_identity': {'admitted_local_tokens': 10_000},
        'trace_id': 'trace'})
    dump = _dump(tmp_path, 'duplicated', [row, row])
    assert measure([dump, dump])['kimi']['samples'] == 1
