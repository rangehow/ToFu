"""Claude Fable 5.2: exact first-party registration on the Claude API.

Facts checked 2026-11-12 (docs/MODEL_REGISTRATION.md, "Claude Fable 5.2"): the
release notes and model overview date the ``claude-fable-5-2`` release to
2026-11-09 (no dated snapshot); the pricing page lists $8/$40 per million with
$10 5-minute and $16 1-hour cache writes and $0.20 cache reads, flat across
the 1M window; thinking is adaptive only and always on, with efforts
low/medium/high/xhigh/max (API default high); non-default sampling returns 400;
and the preserved-thinking prefix check is enforced, so thinking blocks must be
passed back unchanged. ``claude-fable-5`` is a prefix of this id, so the
release needle must not inherit Fable 5's date.
"""
from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'anthropic', 'model_id': 'claude-fable-5-2'}
OPENROUTER_WIRE = 'anthropic/claude-fable-5.2'
OMITTED_SAMPLING = {'temperature', 'top_p', 'top_k'}
_TOOLS = [{'type': 'function', 'function': {
    'name': 'read', 'parameters': {'type': 'object', 'properties': {}}}}]


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


def _profile():
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    profile, _previous = current_model_defaults().resolve(ModelRef.from_value(MODEL))
    return profile


@pytest.fixture
def listed(monkeypatch, tmp_path):
    """Shipped anthropic and openrouter templates once OpenRouter's live catalogue lists Fable 5.2."""
    import lib.provider.provider_template_feed as feed
    from lib.provider.provider_template_recipes import load_provider_templates

    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '1')
    monkeypatch.setattr(feed, '_CACHE_PATH', str(tmp_path / 'provider_template_feed.json'))
    monkeypatch.setattr(feed, 'maybe_refresh_async', lambda *_args, **_kwargs: False)
    templates = [row for row in load_provider_templates()
                 if row['key'] in ('anthropic', 'openrouter')]
    row = {'id': OPENROUTER_WIRE, 'context_length': 1_000_000,
           'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
           'supported_parameters': ['reasoning'],
           'pricing': {'prompt': '0.000008', 'completion': '0.00004'}}
    fetched_at = time.time()
    source = {'url': feed.PUBLIC_CATALOGS['openrouter']['url'], 'fetched_at': fetched_at,
              'listed_at': {}}
    with open(feed._CACHE_PATH, 'w') as handle:
        json.dump({'version': 2, 'templates': feed.recipes_from_catalog([row]),
                   'sources': {'openrouter': source}}, handle)
    return feed.merge_feed_recipes(templates, feed.load_cached_feed(), now=fetched_at)


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    bundle = _bundle('anthropic')
    assert _wire_ids(bundle) == ['claude-fable-5-2']
    model = _public_model(bundle)
    assert model['release_date'] == '2026-11-09'
    assert model['context_window'] == 1_000_000
    assert {'text', 'vision', 'thinking'} <= set(model['capabilities'])
    assert 'cheap' not in model['capabilities']
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (8.0, 40.0)
    assert price['cache_read'] == pytest.approx(0.20)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'thinking_type'
    assert reasoning['enabled_type'] == 'adaptive'
    assert reasoning['effort_field'] == 'effort'
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == ['low', 'medium', 'high', 'xhigh', 'max']
    assert reasoning['default_effort'] == 'high'
    assert reasoning['preserve_history'] == 'replay'
    assert OMITTED_SAMPLING <= set(model['inference_profile']['sampling']['omit'])
    assert registration_missing_fields(model) == []


def test_world_catalogue_sync_publishes_the_model():
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog

    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)

    published = {(row['creator_id'], row['model_id']): row
                 for row in repository.get(boundary).document['models']}
    assert published[(MODEL['creator_id'], MODEL['model_id'])]['registration_state'] == 'ready'


def test_the_openrouter_respelling_is_no_longer_withheld(listed, monkeypatch):
    from lib.model_catalog.release_watch import release_candidates
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog, withheld_catalog_models

    feeds, candidates = release_candidates(listed)
    assert set(feeds) == {'openrouter'}
    assert OPENROUTER_WIRE not in [row['id'] for row in candidates]
    assert not [row for row in withheld_catalog_models(listed)
                if any(OPENROUTER_WIRE in supply['wire_model_ids'] for supply in row['supply'])]
    # The separator respelling folds into the registered Model: one row, official id.
    monkeypatch.setattr('lib.provider.provider_template_recipes.load_provider_templates',
                        lambda: listed)
    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)
    assert [row['model_id'] for row in repository.get(boundary).document['models']
            if row['model_id'] in ('claude-fable-5-2', 'claude-fable-5.2')] == ['claude-fable-5-2']


def test_maintained_price_bills_the_documented_cache_rates():
    from lib.cost import compute_cost

    cost = compute_cost({
        'input_tokens': 1_000_000, 'output_tokens': 1_000_000,
        'cache_creation_input_tokens': 2_000_000,
        'cache_creation': {'ephemeral_5m_input_tokens': 1_000_000,
                           'ephemeral_1h_input_tokens': 1_000_000},
        'cache_read_input_tokens': 1_000_000,
    }, 'claude-fable-5-2')
    # $8 input + $10 5m write + $16 1h write + $0.20 read + $40 output.
    assert cost['costUsd'] == pytest.approx(74.20)


@pytest.mark.parametrize(('depth', 'effort'), [
    ('off', 'low'), ('low', 'low'), ('medium', 'medium'), ('high', 'high'),
    ('xhigh', 'xhigh'), ('max', 'max'), ('ultra', 'max'),
])
def test_shipped_profile_compiles_adaptive_thinking_with_the_effort_ladder(depth, effort):
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.llm.body import build_body

    body = build_body('claude-fable-5-2', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, temperature=0.6, max_tokens=4096,
                      inference_profile=_profile())
    wire = openai_body_to_anthropic(body)
    # Thinking can't be turned off: off sends adaptive thinking at the lowest effort.
    assert wire['thinking'] == {'type': 'adaptive', 'display': 'summarized'}
    assert wire['output_config']['effort'] == effort
    assert not OMITTED_SAMPLING & wire.keys()


def test_declared_replay_keeps_signed_thinking_and_never_sends_an_unsigned_block():
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.llm.body import build_body
    from lib.model_info import model_requires_reasoning_content_replay

    assert model_requires_reasoning_content_replay('claude-fable-5-2')
    assert model_requires_reasoning_content_replay(OPENROUTER_WIRE)

    def assistant_blocks(**reasoning):
        messages = [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': '', **reasoning, 'tool_calls': [
                {'id': 'toolu_1', 'type': 'function',
                 'function': {'name': 'read', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'ok'}]
        body = build_body('claude-fable-5-2', messages, thinking_depth='high',
                          max_tokens=4096, tools=_TOOLS, inference_profile=_profile())
        return openai_body_to_anthropic(body)['messages'][1]['content']

    signed = assistant_blocks(reasoning_content='own trace', thinking_signature='SIG_A')
    assert signed[0] == {'type': 'thinking', 'thinking': 'own trace', 'signature': 'SIG_A'}
    unsigned = assistant_blocks(reasoning_content='another model trace')
    assert [block['type'] for block in unsigned] == ['tool_use']
