"""Meta Muse Ember: exact first-party registration on the Meta Model API.

Facts checked 2026-11-12 (docs/MODEL_REGISTRATION.md, "Muse Ember"): the
research blog and launch post release ``muse-ember-1.0`` on the Meta Model API
on 2026-11-09; the model page gives a 1,048,576-token window, 524,288 max
output and text/image/video/audio/PDF input; the pricing page bills
$0.10/$0.50 per million (cached $0.004) up to 200K input tokens and
$0.30/$1.50 (cached $0.012) for the whole of a longer request. Reasoning is
always on (``none`` returns 400) with efforts minimal to max, default max;
Chat Completions cannot carry reasoning across turns for external keys,
accepts temperature or top_p but not both, and rejects the penalties and stop
on reasoning models.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MODEL = {'creator_id': 'meta', 'model_id': 'muse-ember-1.0'}
OMITTED_SAMPLING = {'temperature', 'presence_penalty', 'frequency_penalty', 'stop'}


def _bundle(key):
    from lib.model_routing.model_defaults import official_model_directory
    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle, load_provider_templates,
    )
    templates = load_provider_templates()
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


def test_official_template_registers_complete_first_party_facts():
    from lib.model_routing.registration import registration_missing_fields

    bundle = _bundle('meta')
    assert _wire_ids(bundle) == ['muse-ember-1.0']
    model = _public_model(bundle)
    assert model['release_date'] == '2026-11-09'
    assert model['context_window'] == 1_048_576
    assert {'text', 'vision', 'video', 'thinking', 'cheap'} <= set(model['capabilities'])
    price = model.get('list_pricing') or model.get('official_pricing')
    assert (price['input'], price['output']) == (0.10, 0.50)
    assert price['cache_read'] == pytest.approx(0.004)
    assert (price['currency'], price['unit']) == ('USD', 'per_million_tokens')
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert reasoning['can_disable'] is False
    assert reasoning['supported_efforts'] == ['minimal', 'low', 'medium', 'high', 'xhigh', 'max']
    assert reasoning['default_effort'] == 'max'
    assert reasoning['preserve_history'] == 'none'
    assert set(model['inference_profile']['sampling']['omit']) == OMITTED_SAMPLING
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


@pytest.mark.parametrize(('prompt_tokens', 'rates'), [
    (1_000, (0.10, 0.50)), (200_000, (0.10, 0.50)),
    (200_001, (0.30, 1.50)), (1_000_000, (0.30, 1.50)),
])
def test_maintained_price_selects_the_documented_context_tier(prompt_tokens, rates):
    from lib.pricing import lookup_pricing

    row = lookup_pricing('muse-ember-1.0', prompt_tokens=prompt_tokens)
    assert (row['input'], row['output']) == rates
    assert row['cacheReadMul'] == pytest.approx(0.04)


def test_a_long_context_request_is_billed_at_the_long_context_rate_in_full():
    from lib.cost import compute_cost

    short = compute_cost({'prompt_tokens': 150_000, 'completion_tokens': 10_000}, 'muse-ember-1.0')
    long = compute_cost({'prompt_tokens': 300_000, 'completion_tokens': 10_000}, 'muse-ember-1.0')
    assert short['costUsd'] == pytest.approx(150_000 * 0.10 / 1e6 + 10_000 * 0.50 / 1e6)
    assert long['costUsd'] == pytest.approx(300_000 * 0.30 / 1e6 + 10_000 * 1.50 / 1e6)


def test_output_is_capped_at_the_documented_maximum_and_no_trace_is_replayed():
    from lib.model_info import _clamp_max_tokens, model_requires_reasoning_content_replay

    assert _clamp_max_tokens('muse-ember-1.0', 10**6) == 524_288
    assert not model_requires_reasoning_content_replay('muse-ember-1.0')


@pytest.mark.parametrize(('depth', 'effort'), [
    ('off', 'minimal'), ('minimal', 'minimal'), ('low', 'low'), ('medium', 'medium'),
    ('high', 'high'), ('xhigh', 'xhigh'), ('max', 'max'), ('ultra', 'max'), ('default', 'max'),
])
def test_shipped_profile_compiles_the_always_on_effort_ladder(depth, effort):
    from lib.llm.body import build_body
    from lib.model_routing.domain import ModelRef
    from lib.model_routing.model_defaults import current_model_defaults

    profile, _previous = current_model_defaults().resolve(ModelRef.from_value(MODEL))
    body = build_body('muse-ember-1.0', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, temperature=0.6, inference_profile=profile,
                      extra={'top_p': 0.9, 'presence_penalty': 0.1,
                             'frequency_penalty': 0.1, 'stop': ['END']})
    assert body['reasoning_effort'] == effort
    assert not OMITTED_SAMPLING & body.keys()
    # Temperature or top_p, never both: the caller's top_p survives alone.
    assert body['top_p'] == 0.9
