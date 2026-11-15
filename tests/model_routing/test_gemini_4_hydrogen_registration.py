"""Gemini 4 Hydrogen: exact first-party registration on the Gemini API.

Facts checked 2026-11-13 (docs/MODEL_REGISTRATION.md, "Gemini 4 Hydrogen"):
the model page fixes the stable ``gemini-4-hydrogen`` id, the 1,048,576-token
unified input/output window, 65,536 default max output and
text/image/video/audio/PDF input; the changelog dates GA to 2026-11-12; the
pricing page lists a standard $1.50/$7.50 per million (+$0.075 cache read,
implicit caching only), flat across the whole window and with no intro price.
Unlike Argon and Carbon, the thinking guide gives Hydrogen
low/medium/high/xhigh with high as the default; Gemini 4 rejects minimal (the
OpenAI-compatible endpoint maps it to low), thinking cannot be disabled and
sampling parameters are ignored.
"""
from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'google', 'model_id': 'gemini-4-hydrogen'}
OPENROUTER_WIRE = 'google/gemini-4-hydrogen'
OMITTED_SAMPLING = {'temperature', 'top_p', 'top_k'}


def _bundle(key, templates=None):
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates,
    )
    templates = load_provider_templates() if templates is None else templates
    return compile_provider_template_bundle(
        key, official_directory=official_model_directory(templates),
        _templates=templates)


def _wire_ids(bundle):
    offerings = {o['offering_id'] for o in bundle['offerings']
                 if o.get('model') == MODEL}
    return [d['wire_model_id'] for d in bundle['deployments']
            if d['offering_id'] in offerings]


def _public_model(bundle):
    from lib.model_routing.domain import normalize_document, public_projection
    row = next(m for m in bundle['models'] if m['model_id'] == MODEL['model_id'])
    document = normalize_document({
        'contract_version': 'tofu.model-routing/v2', 'revision': 0,
        'creators': bundle['creators'], 'models': [row], 'providers': [],
        'provider_accesses': [], 'connections': [], 'credentials': [],
        'offerings': [], 'deployments': [],
    })
    return public_projection(document)['models'][0]


@pytest.fixture
def listed(monkeypatch, tmp_path):
    """Shipped gemini and openrouter templates once OpenRouter's live catalogue lists Hydrogen."""
    import lib.provider.provider_template_feed as feed
    from lib.provider.provider_template_recipes import load_provider_templates

    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '1')
    monkeypatch.setattr(feed, '_CACHE_PATH', str(tmp_path / 'provider_template_feed.json'))
    monkeypatch.setattr(feed, 'maybe_refresh_async', lambda *_args, **_kwargs: False)
    templates = [row for row in load_provider_templates() if row['key'] in ('gemini', 'openrouter')]
    row = {'id': OPENROUTER_WIRE, 'context_length': 1_048_576,
           'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
           'supported_parameters': ['reasoning'],
           'pricing': {'prompt': '0.0000015', 'completion': '0.0000075'}}
    fetched_at = time.time()
    source = {'url': feed.PUBLIC_CATALOGS['openrouter']['url'], 'fetched_at': fetched_at,
              'listed_at': {}}
    with open(feed._CACHE_PATH, 'w') as handle:
        json.dump({'version': 2, 'templates': feed.recipes_from_catalog([row]),
                   'sources': {'openrouter': source}}, handle)
    return feed.merge_feed_recipes(templates, feed.load_cached_feed(), now=fetched_at)


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    bundle = _bundle('gemini')
    assert _wire_ids(bundle) == ['gemini-4-hydrogen']
    model = _public_model(bundle)
    assert model['release_date'] == '2026-11-12'
    assert model['context_window'] == 1_048_576
    assert {'text', 'vision', 'thinking'} <= set(model['capabilities'])
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (1.5, 7.5)
    assert price['cache_read'] == pytest.approx(0.075)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == ['low', 'medium', 'high', 'xhigh']
    assert reasoning['default_effort'] == 'high'
    assert OMITTED_SAMPLING <= set(model['inference_profile']['sampling']['omit'])
    assert registration_missing_fields(model) == []


def test_hydrogen_does_not_share_the_argon_and_carbon_profile():
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    defaults = current_model_defaults()
    hydrogen = defaults.resolve(ModelRef.from_value(MODEL))
    carbon = defaults.resolve(ModelRef.from_value({**MODEL, 'model_id': 'gemini-4-carbon'}))
    assert hydrogen is not None and carbon is not None and hydrogen != carbon
    assert carbon[0]['reasoning']['supported_efforts'] == ['low', 'medium', 'high']
    assert carbon[0]['reasoning']['default_effort'] == 'medium'


def test_world_catalogue_sync_publishes_the_model():
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog

    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)

    published = {(row['creator_id'], row['model_id']): row
                 for row in repository.get(boundary).document['models']}
    assert published[(MODEL['creator_id'], MODEL['model_id'])]['registration_state'] == 'ready'


def test_an_openrouter_listing_binds_to_the_model_instead_of_being_withheld(listed):
    from lib.model_catalog.release_watch import release_candidates
    from lib.model_routing.world_catalog import withheld_catalog_models

    assert _wire_ids(_bundle('openrouter', listed)) == [OPENROUTER_WIRE]
    feeds, candidates = release_candidates(listed)
    assert set(feeds) == {'openrouter'}
    assert OPENROUTER_WIRE not in [row['id'] for row in candidates]
    assert not [row for row in withheld_catalog_models(listed)
                if any(OPENROUTER_WIRE in supply['wire_model_ids'] for supply in row['supply'])]


@pytest.mark.parametrize('prompt_tokens', [1_000, 200_001, 1_048_576])
def test_maintained_price_is_the_flat_standard_price(prompt_tokens):
    from lib.pricing import lookup_pricing

    row = lookup_pricing('gemini-4-hydrogen', prompt_tokens=prompt_tokens)
    assert (row['input'], row['output'], row['cacheReadMul']) == (1.5, 7.5, 0.05)
    assert row['cacheWriteMul'] == 1.0
    assert not row.get('contextTiers')


def test_output_is_capped_at_the_default_synchronous_maximum():
    from lib.model_info import _clamp_max_tokens

    assert _clamp_max_tokens('gemini-4-hydrogen', 10**6) == 65_536


@pytest.mark.parametrize(('depth', 'effort'), [
    ('off', 'low'), ('minimal', 'low'), ('low', 'low'), ('medium', 'medium'),
    ('high', 'high'), ('xhigh', 'xhigh'), ('max', 'xhigh'), ('ultra', 'xhigh'),
    ('default', 'high'),
])
def test_shipped_profile_compiles_the_documented_thinking_levels(depth, effort):
    from lib.llm.body import build_body
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    profile, _previous = current_model_defaults().resolve(ModelRef.from_value(MODEL))
    body = build_body('gemini-4-hydrogen', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, temperature=0.6, inference_profile=profile)
    assert body['reasoning_effort'] == effort
    assert not OMITTED_SAMPLING & body.keys()
