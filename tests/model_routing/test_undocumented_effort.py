"""Evidence-aware default-only reasoning and route-specific supplier controls."""
import pytest

from lib.llm.reasoning import ReasoningContractError, compile_reasoning
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.inference import candidate_inference_metadata, resolve_inference_profile, validate_inference_profile
from types import SimpleNamespace
from lib.model_routing.registration import registration_missing_fields

pytestmark = pytest.mark.unit


def profile():
    return {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                          'can_disable': False, 'effort_knowledge': 'undocumented'}}


def test_default_only_admission_and_wire():
    p = profile()
    model = {'creator_id': 'unknown', 'model_id': 'example', 'release_date': '2026-01-01',
             'registration_metadata': {'release_date': '2026-01-01', 'checked_at': '2026-01-01',
                                       'source_urls': ['https://example.com']},
             'capabilities': ['text', 'thinking'], 'inference_profile': p,
             'list_pricing': {'input': 1, 'output': 2, 'currency': 'USD', 'unit': 'per_million_tokens'}}
    assert registration_missing_fields(model) == []
    for mode in ('default', 'enabled', 'off', 'lowest', 'preset'):
        intent = {'version': 1, 'mode': mode, 'strict': True}
        if mode == 'preset':
            intent['value'] = 'high'
        body = compile_reasoning({'_reasoning_intent': intent}, 'example', inference_profile=p)
        assert 'reasoning_effort' not in body
        if mode != 'default':
            assert body['_reasoning_resolution']['effective'] == 'default'
            assert body['_reasoning_resolution']['reason'] == 'effort_undocumented_fell_back_to_default'
    with pytest.raises(ReasoningContractError, match='unsupported_effort'):
        compile_reasoning({'_reasoning_intent': {'version': 1, 'mode': 'exact', 'value': 'low', 'strict': True}},
                          'example', inference_profile=p)


def test_default_only_conflicting_claims_rejected():
    for update in ({'supported_efforts': []}, {'default_effort': 'low'},
                   {'can_disable': True}, {'wire_format': 'thinking_type'}):
        p = profile()
        p['reasoning'].update(update)
        with pytest.raises(ModelRoutingError):
            validate_inference_profile(p)


@pytest.mark.parametrize('dialect', ['enable_thinking', 'chat_template_kwargs'])
def test_supplier_toggle_replaces_inherited_undocumented_marker(dialect):
    p = profile()
    supplier = {'reasoning': {'support': 'supported', 'wire_format': dialect, 'can_disable': True}}
    candidate = SimpleNamespace(model={'inference_profile': p}, offering={'inference_profile': supplier},
                                connection={}, deployment={})
    resolved = candidate_inference_metadata(candidate)['inference_profile']['reasoning']
    assert resolved == supplier['reasoning']
    assert p == profile()


@pytest.mark.parametrize('support', ['unsupported', 'unknown'])
def test_supplier_unsupported_or_unknown_clears_inherited_marker(support):
    resolved = resolve_inference_profile(model={'inference_profile': profile()},
        offering={'inference_profile': {'reasoning': {'support': support}}})
    assert 'effort_knowledge' not in resolved['reasoning']
    assert resolved['reasoning']['support'] == support


def test_compatible_partial_override_retains_marker():
    resolved = resolve_inference_profile(model={'inference_profile': profile()},
        offering={'inference_profile': {'reasoning': {'support': 'supported'}}})
    assert resolved['reasoning'] == profile()['reasoning']


def test_offering_ladder_replaces_unknown_without_modifying_model():
    p = profile()
    offering = {'inference_profile': {'reasoning': {'support': 'supported',
                  'wire_format': 'reasoning_effort', 'supported_efforts': ['low', 'high'],
                  'default_effort': 'low'}}}
    resolved = resolve_inference_profile(model={'inference_profile': p}, offering=offering)
    assert p['reasoning']['effort_knowledge'] == 'undocumented'
    assert 'effort_knowledge' not in resolved['reasoning']
    body = compile_reasoning({'_reasoning_intent': {'version': 1, 'mode': 'default', 'strict': False}},
                             'example', inference_profile=resolved)
    assert body['reasoning_effort'] == 'low'
