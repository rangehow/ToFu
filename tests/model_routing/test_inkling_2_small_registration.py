"""Thinking Machines Inkling 2 Small: exact first-party knowledge for relay supply.

Facts checked 2026-11-13 (docs/MODEL_REGISTRATION.md, "Inkling 2 Small"): the
model card dates the release to 2026-11-05; the announcement gives a context
window of up to 1M tokens (config.json ``model_max_length`` 1,048,576), image
input and variable effort; Tinker serverless inference (beta) lists $0.30
input, $0.03 cached input and $1.00 output per million. Tinker's
OpenAI-compatible endpoint takes ``reasoning_effort`` none through xhigh and
defaults to 0.9 (high); none only conditions the model toward no reasoning,
so it is no disable rung. No official provider template exists: the Model
reaches the registry through public relay listings such as OpenRouter's
``thinkingmachines/inkling-2-small``.
"""
from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'thinkingmachines', 'model_id': 'inkling-2-small'}
OPENROUTER_WIRE = 'thinkingmachines/inkling-2-small'
LADDER = ['minimal', 'low', 'medium', 'high', 'xhigh']


def _bundle(key, templates):
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import compile_provider_template_bundle
    return compile_provider_template_bundle(
        key, official_directory=official_model_directory(templates),
        _templates=templates)


@pytest.fixture
def listed(monkeypatch, tmp_path):
    """The shipped openrouter template once OpenRouter's live catalogue lists Inkling 2 Small.

    The relay quotes its own price, so a relay price leaking into the Model shows.
    """
    import lib.provider.provider_template_feed as feed
    from lib.provider.provider_template_recipes import load_provider_templates

    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '1')
    monkeypatch.setattr(feed, '_CACHE_PATH', str(tmp_path / 'provider_template_feed.json'))
    monkeypatch.setattr(feed, 'maybe_refresh_async', lambda *_args, **_kwargs: False)
    templates = [row for row in load_provider_templates() if row['key'] == 'openrouter']
    row = {'id': OPENROUTER_WIRE, 'context_length': 1_048_576,
           'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
           'supported_parameters': ['reasoning'],
           'pricing': {'prompt': '0.0000004', 'completion': '0.0000012'}}
    fetched_at = time.time()
    source = {'url': feed.PUBLIC_CATALOGS['openrouter']['url'], 'fetched_at': fetched_at,
              'listed_at': {}}
    with open(feed._CACHE_PATH, 'w') as handle:
        json.dump({'version': 2, 'templates': feed.recipes_from_catalog([row]),
                   'sources': {'openrouter': source}}, handle)
    templates = feed.merge_feed_recipes(templates, feed.load_cached_feed(), now=fetched_at)
    monkeypatch.setattr('lib.provider.provider_template_recipes.load_provider_templates',
                        lambda: templates)
    return templates


def test_world_catalogue_sync_publishes_the_listed_model_ready(listed):
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.domain import public_projection
    from lib.model_routing.registration import registration_missing_fields
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog

    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)

    document = repository.get(boundary).document
    published = {(row['creator_id'], row['model_id']): row
                 for row in public_projection(document)['models']}
    assert (MODEL['creator_id'], OPENROUTER_WIRE) not in published
    model = published[(MODEL['creator_id'], MODEL['model_id'])]
    assert model['registration_state'] == 'ready'
    assert model['release_date'] == '2026-11-05'
    assert model['context_window'] == 1_048_576
    assert {'text', 'vision', 'thinking'} <= set(model['capabilities'])
    assert 'list_pricing' not in model
    price = model['official_pricing']
    assert (price['input'], price['output']) == (0.30, 1.00)
    assert price['cache_read'] == pytest.approx(0.03)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == LADDER
    assert reasoning['default_effort'] == 'high'
    assert registration_missing_fields(model) == []


def test_the_relay_price_stays_on_the_relay_offering(listed):
    bundle = _bundle('openrouter', listed)
    offering = next(row for row in bundle['offerings'] if row.get('model') == MODEL)
    wires = [row['wire_model_id'] for row in bundle['deployments']
             if row['offering_id'] == offering['offering_id']]
    assert wires == [OPENROUTER_WIRE]
    assert (offering['actual_pricing']['input'], offering['actual_pricing']['output']) == (0.4, 1.2)


def test_the_listing_is_no_longer_a_release_watch_candidate(listed):
    from lib.model_catalog.release_watch import release_candidates
    from lib.model_routing.world_catalog import withheld_catalog_models

    feeds, candidates = release_candidates(listed)
    assert set(feeds) == {'openrouter'}
    assert OPENROUTER_WIRE not in [row['id'] for row in candidates]
    assert (MODEL['creator_id'], MODEL['model_id']) not in {
        (row['creator_id'], row['model_id']) for row in withheld_catalog_models(listed)}


def test_maintained_price_is_the_tinker_serverless_list_price():
    from lib.pricing import lookup_pricing

    row = lookup_pricing('inkling-2-small', prompt_tokens=1_000)
    assert (row['input'], row['output'], row['cacheReadMul']) == (0.30, 1.00, 0.10)
    assert row['cacheWriteMul'] == 1.0


def test_no_reasoning_replay_is_required():
    from lib.model_info import model_requires_reasoning_content_replay

    assert not model_requires_reasoning_content_replay('inkling-2-small')


@pytest.mark.parametrize(('depth', 'effort'), [
    ('off', 'minimal'), ('minimal', 'minimal'), ('low', 'low'), ('medium', 'medium'),
    ('high', 'high'), ('xhigh', 'xhigh'), ('max', 'xhigh'), ('ultra', 'xhigh'),
    ('default', 'high'),
])
def test_shipped_profile_compiles_the_documented_efforts(depth, effort):
    from lib.llm.body import build_body
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    profile, _previous = current_model_defaults().resolve(ModelRef.from_value(MODEL))
    body = build_body('inkling-2-small', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, temperature=0.6, inference_profile=profile,
                      extra={'top_p': 0.9})
    assert body['reasoning_effort'] == effort
    # No sampling restriction is documented, so the caller's values pass through.
    assert (body['temperature'], body['top_p']) == (0.6, 0.9)
