"""Qwen4-Max, Muse Spark 1.2, MiniMax M3.1 Flash, Hy3 and Hy4 Preview facts.

These five models are the OpenCode Go roster's registration gaps. Each now
has exact maintained facts: a release date, a list price, capabilities,
context, and a declared reasoning contract. The executable acceptance is that
the OpenCode Go template compiles every Deployment admitted as ``ready``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODELS = {
    ('alibaba', 'qwen4-max'): {
        'release': '2026-11-16',
        'context': 1_000_000,
        'capabilities': {'text', 'vision', 'thinking'},
        'pricing': (1.50, 4.50, 0.05),
        'efforts': ['none', 'low', 'high', 'xhigh'],
        'default': 'high',
        'disabled': 'none',
    },
    ('meta', 'muse-spark-1.2'): {
        'release': '2026-08-05',
        'context': 1_048_576,
        'capabilities': {'text', 'vision', 'thinking'},
        'pricing': (1.25, 4.25, 0.12),
        'efforts': ['minimal', 'low', 'medium', 'high', 'xhigh'],
        'default': 'high',
    },
    ('minimax', 'MiniMax-M3.1-Flash'): {
        'release': '2026-10-19',
        'context': 1_000_000,
        'capabilities': {'text', 'vision', 'video', 'thinking'},
        'pricing': (0.15, 0.60, 0.10),
        'efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
        'default': 'max',
    },
    ('tencent', 'hy3'): {
        'release': '2026-07-06',
        'context': 256_000,
        'capabilities': {'text', 'thinking'},
        'pricing': (0.132, 0.528, 0.25),
        'efforts': ['no_think', 'low', 'high'],
        'default': 'high',
        'disabled': 'no_think',
    },
    ('tencent', 'hy4-preview'): {
        'release': '2026-08-28',
        'context': 1_000_000,
        'capabilities': {'text', 'thinking'},
        'pricing': (0.829, 2.486, 0.05),
        'efforts': ['no_think', 'high'],
        'default': 'high',
        'disabled': 'no_think',
    },
}


@pytest.fixture(scope='module')
def directory():
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import load_provider_templates

    return {(row['creator_id'], row['model_id']): row
            for row in official_model_directory(load_provider_templates())}


def _opencode_document():
    import json

    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates,
        normalize_provider_template,
    )
    from lib.model_routing.registration import prepare_registration_document

    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / 'lib' / 'model_info' / 'data'
    template = normalize_provider_template(
        json.loads((source / 'opencode_go.json').read_text(encoding='utf-8')))
    templates = [row for row in load_provider_templates()
                 if row['key'] != 'opencode_go'] + [template]
    bundle = compile_provider_template_bundle('opencode_go', _templates=templates)
    for credential in bundle['credentials']:
        credential['secret_reference'] = 'mrs_' + 'a' * 32
    document = {
        'contract_version': 'tofu.model-routing/v2', 'revision': 0,
        'creators': bundle['creators'], 'models': bundle['models'],
        'providers': [bundle['provider']],
        'provider_accesses': [bundle['provider_access']],
        'connections': bundle['connections'],
        'credentials': bundle['credentials'],
        'offerings': bundle['offerings'],
        'deployments': bundle['deployments'],
    }
    empty = {**document, **{
        key: [] for key in (
            'creators', 'models', 'providers', 'provider_accesses',
            'connections', 'credentials', 'offerings', 'deployments')}}
    return prepare_registration_document(document, previous=empty)


def test_each_model_has_release_context_price_and_reasoning(directory):
    from lib.model_info import release_date

    for identity, expected in MODELS.items():
        row = directory[identity]
        assert release_date(row['model_id']) == expected['release'], identity
        assert row['context_window'] == expected['context'], identity
        assert expected['capabilities'] <= set(row['capabilities']), identity
        price = row['list_pricing']
        assert (price['input'], price['output']) == expected['pricing'][:2], identity
        assert price['currency'] == 'USD' and price['unit'] == 'per_million_tokens'
        reasoning = row['inference_profile']['reasoning']
        assert reasoning['wire_format'] == 'reasoning_effort', identity
        assert reasoning['supported_efforts'] == expected['efforts'], identity
        assert reasoning['default_effort'] == expected['default'], identity
        assert reasoning.get('disabled_effort') == expected.get('disabled'), identity


def test_maintained_prices_carry_the_documented_rates():
    from lib.pricing import MODEL_PRICING

    for identity, expected in MODELS.items():
        row = MODEL_PRICING[identity[1]]
        assert (row['input'], row['output'], row['cacheReadMul']) == expected['pricing']


def test_opencode_go_admits_every_model_as_ready(directory):
    document = _opencode_document()
    states = {(model['creator_id'], model['model_id']): model.get('registration_state')
              for model in document['models']}
    for identity in MODELS:
        assert states[identity] == 'ready', (identity, document['models'])
    assert all(model.get('registration_issues') == []
               for model in document['models'])
    assert not any(deployment.get('registration_issues')
                   for deployment in document['deployments'])
