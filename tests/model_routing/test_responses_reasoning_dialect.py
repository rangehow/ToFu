"""A Responses Connection carries reasoning only as ``reasoning.effort``.

A Model whose official profile names a Chat/Anthropic body dialect (Claude's
``thinking_type`` + ``effort``) can still be supplied on a Responses face
(TokRouter ``tokrouter.claude-opus-5.5``). The route profile must bind the
Responses effort wire, or request preparation refuses every reasoning turn
with ``reasoning_wire_unrepresentable``.
"""
import json

import pytest

from tests.support.overlay import requires_private_overlay

from lib.llm.body import build_body
from lib.model_routing.inference import resolve_inference_profile

pytestmark = pytest.mark.unit

_CLAUDE = {'inference_profile': {'reasoning': {
    'support': 'supported', 'wire_format': 'thinking_type', 'enabled_type': 'adaptive',
    'display': 'summarized', 'effort_field': 'effort', 'can_disable': False,
    'default_effort': 'medium', 'effort_map': {'ultra': 'max'},
    'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max']}}}


@pytest.mark.parametrize('protocol', ['openai_responses', 'responses'])
def test_responses_connection_rebinds_body_dialect_to_effort_wire(protocol):
    profile = resolve_inference_profile(model=_CLAUDE, connection={'protocol': protocol})
    reasoning = profile['reasoning']
    assert reasoning['wire_format'] == 'reasoning_effort'
    assert 'effort_field' not in reasoning
    assert reasoning['supported_efforts'] == ['low', 'medium', 'high', 'xhigh', 'max']


@pytest.mark.parametrize('protocol', ['anthropic', 'openai'])
def test_other_protocols_keep_the_declared_dialect(protocol):
    profile = resolve_inference_profile(model=_CLAUDE, connection={'protocol': protocol})
    assert profile['reasoning']['wire_format'] == 'thinking_type'
    assert profile['reasoning']['effort_field'] == 'effort'


def test_explicit_none_and_ladderless_dialects_are_not_rebound():
    none = resolve_inference_profile(
        model={'inference_profile': {'reasoning': {'support': 'supported', 'wire_format': 'none'}}},
        connection={'protocol': 'openai_responses'})
    assert none['reasoning']['wire_format'] == 'none'
    ladderless = resolve_inference_profile(
        model={'inference_profile': {'reasoning': {
            'support': 'supported', 'wire_format': 'thinking_type', 'can_disable': True}}},
        connection={'protocol': 'openai_responses'})
    assert ladderless['reasoning']['wire_format'] == 'thinking_type'


@pytest.mark.parametrize('depth,effort', [('xhigh', 'xhigh'), ('max', 'max'),
                                          ('default', 'medium'), ('off', 'low')])
@requires_private_overlay
def test_shipped_tokrouter_claude_route_prepares_a_responses_request(monkeypatch, depth, effort):
    from lib.llm._sse_core import prepare_request
    from lib.model_routing import compile_candidates
    from lib.model_routing.domain import NativeModelSelection, ProviderOfferingRef
    from lib.model_routing.inference import candidate_inference_metadata
    from bundles.yourprovider.tests.test_yourprovider_tokrouter_request_names import _yourprovider_document

    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **kw: {'tokens': 20})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **kw: 200000)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: 'direct')
    document = _yourprovider_document('claude-opus-5.5')
    offering = document['offerings'][0]
    candidate = next(
        row for row in compile_candidates(document, NativeModelSelection(
            None, ProviderOfferingRef('yourprovider', offering['offering_id']), ''))
        if row.deployment['wire_model_id'] == 'tokrouter.claude-opus-5.5')
    assert candidate.connection['protocol'] == 'openai_responses'
    profile = candidate_inference_metadata(candidate)['inference_profile']
    body = build_body('tokrouter.claude-opus-5.5', [{'role': 'user', 'content': 'hi'}],
                      thinking_depth=depth, inference_profile=profile)
    plan = prepare_request(body, base_url='https://example.invalid/v1',
                           api_protocol='responses')
    wire = json.loads(plan.body.payload)
    assert wire['reasoning']['effort'] == effort
    assert 'thinking' not in wire and 'effort' not in wire
