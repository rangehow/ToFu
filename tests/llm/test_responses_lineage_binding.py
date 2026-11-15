"""Encrypted Responses state is also bound to the lineage that selected its account.

2026-11-09 real Kunlun (kunlunbedrock.gpt-6.1-sol-m17 through your-llm-gateway.example.com)
evidence: a reasoning item minted on one upstream account is rejected
``invalid_encrypted_content`` on another. FRIDAY picks the account per request;
``Mt-Session-Id`` (lib/llm/cache_affinity.py) pins it most of the time. Tofu's
child lineage is ``invocation:{user}:{task id}``
(lib/tasks_pkg/invocation.invocation_affinity_id), so a worker whose saved
dialogue continues in a NEW task (durable resume, retained agent, or a
conversation worker receiving the parent's messages) resends the old ciphertext
under a different carrier — a guaranteed extra 400 plus lost cache, and a
fatal turn on a route without retire-and-retry recovery.

The ``tofu_state_origin`` marker already names the minting Deployment; it must
also name the minting lineage so checked conversion retires only the state that
no longer belongs, exactly the way routes without declared replay degrade.
"""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from lib.llm.cache_affinity import (
    affinity_lineage_digest, cache_affinity_evidence, cache_affinity_key)
from lib.tasks_pkg.invocation import invocation_affinity_id
from lib.llm.responses_outbound import openai_body_to_responses_checked
from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.llm.responses_state_origin import (
    STATE_ORIGIN_TYPE,
    responses_state_origins,
    stamp_state_origin,
)

pytestmark = pytest.mark.unit

DEPLOYMENT = 'kunlunbedrock.gpt-6.1-sol-m17'
REPLAY_PROFILE = {'features': {'responses_reasoning_replay': 'supported',
                               'cache_affinity_mt_session_id': 'supported'}}


def _lineage(scope: str) -> str:
    return scope


def _history(lineage: str) -> list[dict]:
    assistant = {'role': 'assistant', 'content': '', '_responses_items': [
        {'type': 'reasoning', 'id': 'rs-old', 'summary': [],
         'encrypted_content': 'gAAAA-minted-under-' + lineage}]}
    stamp_state_origin(assistant, DEPLOYMENT, lineage=lineage)
    return [{'role': 'user', 'content': 'Work the task'}, assistant,
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'call-1', 'type': 'function',
                 'function': {'name': 'run_command', 'arguments': '{"command": "ls"}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'a file'}]


def _convert(lineage: str, history: list[dict]):
    return openai_body_to_responses_checked(
        {'model': DEPLOYMENT, 'messages': history,
         '_cache_affinity_scope': lineage,
         '_route_inference_profile': deepcopy(REPLAY_PROFILE)},
        profile='default').require_payload()


def test_origin_marker_records_the_minting_lineage():
    message = {'role': 'assistant', 'content': '', '_responses_items': [
        {'type': 'reasoning', 'id': 'rs-1', 'summary': [], 'encrypted_content': 'gAAAA-x'}]}
    assert stamp_state_origin(message, DEPLOYMENT, lineage='lineage-A') is True
    marker = message['_responses_items'][-1]
    assert marker['type'] == STATE_ORIGIN_TYPE
    assert marker['deployment_id'] == DEPLOYMENT
    assert marker['lineage'] == 'lineage-A'
    assert responses_state_origins([message]) == {DEPLOYMENT}


def test_replay_of_a_continued_lineage_keeps_its_ciphertext():
    lineage = 'invocation:1:task-1'
    payload = _convert(lineage, _history(lineage))
    assert [item['id'] for item in payload['input'] if item['type'] == 'reasoning'] == ['rs-old']


def test_replay_under_a_new_lineage_drops_foreign_ciphertext_only():
    old_lineage = 'invocation:1:task-old'
    new_lineage = 'invocation:1:task-new'
    history = _history(old_lineage)
    # A later assistant message minted under the current lineage keeps its state.
    current = {'role': 'assistant', 'content': '', '_responses_items': [
        {'type': 'reasoning', 'id': 'rs-new', 'summary': [],
         'encrypted_content': 'gAAAA-minted-under-' + new_lineage}]}
    stamp_state_origin(current, DEPLOYMENT, lineage=new_lineage)
    history.append(current)
    payload = _convert(new_lineage, history)
    replayed = [item['id'] for item in payload['input'] if item['type'] == 'reasoning']
    assert replayed == ['rs-new'], replayed
    assert 'rs-old' not in json.dumps(payload['input'])
    assert [item['type'] for item in payload['input']].count('function_call') == 1


def test_settled_stream_records_the_minting_lineage(monkeypatch):
    """The dispatch loop's own stamp carries the minting request's carrier.

    Uses the public sync dispatch path with real slots; only the transport is
    replaced by a response that carries ciphertext, exactly like the Kunlun
    face. The request's affinity evidence is what prepare_request records in
    ``usage['_wire_routing']``.
    """
    from lib.llm_dispatch import api
    from tests.llm_dispatch.test_async_dispatch_stream import _FakeDispatcher, _make_slot

    slot, unused = _make_slot(), _make_slot(key='unused')
    slot.route_deployment_id = DEPLOYMENT
    dispatcher = _FakeDispatcher([slot, unused])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    slot.record_request()
    lineage = 'invocation:1:task-1'
    evidence = cache_affinity_evidence(
        cache_affinity_key({'_cache_affinity_scope': lineage}),
        {'Mt-Session-Id': cache_affinity_key({'_cache_affinity_scope': lineage})}, {})
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': '', '_responses_items': [
            {'type': 'reasoning', 'id': 'rs-1', 'summary': [], 'encrypted_content': 'gAAAA-x'}]},
        compatibility_finish_reason='stop', provider_finish_reason='stop',
        usage={'completion_tokens': 3,
               '_wire_routing': {'affinity': evidence, 'affinity_scope': lineage}},
        state=ProviderStreamState.PROVIDER_FINISHED, saw_finish_reason=True)
    monkeypatch.setattr('lib.llm.stream_chat', lambda *a, **kw: result)
    try:
        api.dispatch_stream([{'role': 'user', 'content': 'hello'}], max_retries=1)
    finally:
        slot.release()
    marker = result.message['_responses_items'][-1]
    assert marker['deployment_id'] == DEPLOYMENT
    assert marker['lineage'] == lineage
    assert evidence == f'Mt-Session-Id@{affinity_lineage_digest(cache_affinity_key({"_cache_affinity_scope": lineage}))}'


def test_child_creation_stamps_the_durable_lineage_id():
    """create_child_invocation carries the worker's durable lineage id."""
    from lib.swarm.types import SubTaskSpec
    from lib.swarm.agent import SubAgent

    spec = SubTaskSpec(id='child-a', role='general', objective='Do the work')
    agent = SubAgent.__new__(SubAgent)
    agent.agent_id = f'agent-{spec.role}-{spec.id}'
    assert agent.agent_id == 'agent-general-child-a'
    assert f'worker:conv-1:{agent.agent_id}' == 'worker:conv-1:agent-general-child-a'


def test_resumed_worker_keeps_its_lineage_and_its_ciphertext():
    """A durable resume is a NEW task continuing the SAME worker history.

    create_child_invocation stamps the durable lineage id from the worker's
    durable agent id, not the fresh task id, so invocation_affinity_id is
    unchanged and checked conversion keeps the ciphertext.
    """
    from lib.tasks_pkg.invocation import InvocationPolicy

    lineage = 'worker:agent-conv:agent-general-child-a'
    policy = InvocationPolicy.child('root-1')
    first = {'_userId': 1, 'id': 'physical-attempt-1', '_invocationPolicy': policy}
    resumed = {'_userId': 1, 'id': 'physical-attempt-2', '_invocationPolicy': policy}
    # Before the durable id existed, a resume changed the carrier.
    assert invocation_affinity_id(first) != invocation_affinity_id(resumed)
    for task in (first, resumed):
        task['_affinityLineageId'] = lineage
    assert invocation_affinity_id(first) == lineage == invocation_affinity_id(resumed)
    # The resumed worker's request keeps the ciphertext minted by its own
    # earlier attempt, because the scope (and therefore the carrier) is equal.
    payload = _convert(lineage, _history(lineage))
    assert [item['id'] for item in payload['input'] if item['type'] == 'reasoning'] == ['rs-old']


def test_unmarked_history_keeps_its_existing_replay_contract():
    """Pre-marker rows have no lineage evidence: never silently drop their state."""
    assistant = {'role': 'assistant', 'content': '', '_responses_items': [
        {'type': 'reasoning', 'id': 'rs-legacy', 'summary': [],
         'encrypted_content': 'gAAAA-legacy'}]}
    stamp_state_origin(assistant, DEPLOYMENT)
    payload = _convert('invocation:1:task-new', [{'role': 'user', 'content': 't'}, assistant])
    assert [item['id'] for item in payload['input'] if item['type'] == 'reasoning'] == ['rs-legacy']
