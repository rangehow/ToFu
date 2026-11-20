"""Cache head at the physical Codex frame, independent of search residency."""
import copy
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from tests.llm.test_responses_websocket import (
    _CODEX_BASE, _ScriptedConnection, _codex_round_one, _install_connection,
    _text_round, codex_identity,
)
__all__ = ['codex_identity']

pytestmark = pytest.mark.unit

_POLICY = 'Authorized local tools only. Do not disclose credentials.'
_QUESTION = 'Compare the two papers. 保留原文。'
_READ = {'type': 'function', 'function': {'name': 'read_files',
         'description': 'Read local research files.', 'parameters': {'type': 'object'}}}
_DOMAINS = [{'domain': 'academic', 'purpose': 'Papers and citations.'}]


def _prepare(messages, *, task='task-codex', search=True):
    from lib.llm._sse_core import prepare_request
    from lib.tools.registry import ToolContext, assemble_tool_list

    cfg = {} if search else {'toolSpecDenylist': ['search']}
    context = ToolContext(cfg=cfg, task_id=task, project_path='',
                          project_enabled=False, search_mode='multi' if search else 'off',
                          search_enabled=search, fetch_enabled=False,
                          code_exec_enabled=False, browser_enabled=False,
                          desktop_enabled=False, messages=messages)
    registry_tools, _ = assemble_tool_list(context)
    tools = [*(t for t in registry_tools or [] if t['function']['name'] == 'web_search'), _READ]
    return prepare_request({
        'model': 'gpt-6-sol', 'messages': messages, 'tools': tools,
        '_task_id': task, '_conv_id': 'golden-conversation',
    }, api_key='', base_url=_CODEX_BASE, api_protocol='responses', oauth='codex')


def _messages():
    return [{'role': 'system', 'content': _POLICY},
            {'role': 'user', 'content': _QUESTION}]


@pytest.fixture
def search_capabilities(monkeypatch):
    # A model request must ask the runtime authority, not inspect whether an
    # unrelated worker happened to import it. No real search or bridge I/O.
    monkeypatch.delitem(sys.modules, 'tofu_search', raising=False)
    domains = copy.deepcopy(_DOMAINS)
    loads = []

    def prepare_import():
        loads.append(True)
        monkeypatch.setitem(sys.modules, 'tofu_search',
                            SimpleNamespace(describe_domains=lambda: domains))

    monkeypatch.setattr('lib.search.search_runtime.prepare_search_dependency_import',
                        prepare_import)
    return domains, loads


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def _assert_head(frame):
    search, read = frame['tools']
    assert [search['name'], read['name']] == ['web_search', 'read_files']
    props = search['parameters']['properties']
    assert props['vertical']['enum'] == ['auto', 'academic', 'off']
    assert props['queries']['items']['properties']['vertical']['enum'] == props['vertical']['enum']
    assert hashlib.sha256(_compact_json(frame['tools'])).hexdigest() == (
        '88258583621468e661f59c9d6898d19e61dc9fb9e1cc638c6047958f87403dbd')
    assert frame['store'] is False
    assert frame['instructions'] == ''  # exact Codex dialect; policy is in input
    assert frame['prompt_cache_key'] == 'c2ebea05-4462-5e8a-a9dc-a4e737199602'


def test_first_request_assembles_full_schema_before_any_tool_execution(
        monkeypatch, codex_identity, search_capabilities):
    from lib.llm import responses_websocket as ws

    conn = _ScriptedConnection(_text_round())
    _install_connection(ws, monkeypatch, conn)
    plan = _prepare(_messages())
    ws.stream_responses_websocket(plan)
    frame, = conn.sent
    _assert_head(frame)
    assert frame['previous_response_id'] is None
    assert [i['content'][0]['text'] for i in frame['input']] == [_POLICY, _QUESTION]
    assert len(search_capabilities[1]) == 1


def test_tool_round_extends_same_head_and_sends_only_exact_delta(
        monkeypatch, codex_identity, search_capabilities):
    from lib.llm import responses_websocket as ws

    conn = _ScriptedConnection(_codex_round_one(), _text_round())
    _install_connection(ws, monkeypatch, conn)
    messages = _messages()
    assistant, finish, _ = ws.stream_responses_websocket(_prepare(messages))
    assert finish == 'tool_calls'
    messages += [assistant, {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'file evidence'}]
    ws.stream_responses_websocket(_prepare(messages))
    first, next_frame = conn.sent
    _assert_head(first)
    _assert_head(next_frame)
    assert next_frame['previous_response_id'] == 'resp_1'
    assert next_frame['input'] == [{'type': 'function_call_output', 'call_id': 'call_1',
                                    'output': 'file evidence'}]


def _select_archive(messages, *, compacted):
    from lib.tasks_pkg.compaction._resume_projection import (
        CHECKPOINT_IMPLEMENTATION, compaction_resume_witness, select_retained_text_archive,
    )

    config = {'model': 'gpt-6-sol', '_modelReplayBase': [['human', 'a' * 64]]}
    witness = compaction_resume_witness({'config': config, '_turnId': 'turn'}, rounds=[])
    receipt = {'status': 'completed', 'resumeWitness': witness}
    if compacted:
        receipt['summary'] = {'accepted': True, 'complete': True}
    else:
        receipt['implementation'] = CHECKPOINT_IMPLEMENTATION
    stored = json.loads(json.dumps([{
        'archive': {'convId': 'golden-conversation', 'id': 'archive',
                    'createdAt': 42, 'receipt': receipt, 'resultStatus': 'completed'},
        'postMessages': messages,
    }]))
    args = {'conversation_id': 'golden-conversation', 'user_id': 1, 'turn_id': 'turn'}
    assert select_retained_text_archive(
        stored, {**config, 'systemPrompt': 'changed policy'}, [], **args) == (
            'indeterminate', None)
    status, archive = select_retained_text_archive(stored, config, [], **args)
    assert status == 'selected'
    return archive['postMessages']


def _new_attempt_frames(monkeypatch, before, after):
    from lib.llm import responses_websocket as ws

    first = _ScriptedConnection(_text_round())
    second = _ScriptedConnection(_text_round())
    _install_connection(ws, monkeypatch, first, second)
    ws.stream_responses_websocket(_prepare(before))
    # Simulate a cold execution process resuming the same retained view.
    monkeypatch.delitem(sys.modules, 'tofu_search', raising=False)
    try:
        ws.stream_responses_websocket(_prepare(after, task='new-attempt'))
    finally:
        ws.release_task_websockets('new-attempt')
    a, b = first.sent[0], second.sent[0]
    _assert_head(a)
    _assert_head(b)
    assert b['previous_response_id'] is None
    return a, b


def test_continue_retains_head_and_exact_working_view(
        monkeypatch, codex_identity, search_capabilities):
    messages = [*_messages(), {'role': 'assistant', 'content': 'Prior answer.'}]
    restored = _select_archive(messages, compacted=False)
    before, after = _new_attempt_frames(monkeypatch, messages, restored)
    assert after['input'] == before['input']
    assert [i['content'][0]['text'] for i in after['input']] == [
        _POLICY, _QUESTION, 'Prior answer.']


def test_compaction_preserves_head_without_suppressing_the_authorized_change(
        monkeypatch, codex_identity, search_capabilities):
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff

    messages = [*_messages(), {'role': 'assistant', 'content': 'Retained anchor.'}]
    before = [*messages, {'role': 'user', 'content': 'Older source detail.'}]
    handoff = make_compaction_handoff('Verified summary of earlier sources.', phase='mid_turn')
    restored = _select_archive([*messages, handoff], compacted=True)
    a, b = _new_attempt_frames(monkeypatch, before, restored)
    assert b['input'][:3] == a['input'][:3]
    assert b['input'][3]['content'][0]['text'] == handoff['content']
    assert a['input'][3]['content'][0]['text'] == 'Older source detail.'


def test_real_availability_change_is_not_frozen_and_denial_stays_denied(
        monkeypatch, codex_identity, search_capabilities):
    from lib.llm import responses_websocket as ws

    conn = _ScriptedConnection(_text_round())
    _install_connection(ws, monkeypatch, conn)
    domains, loads = search_capabilities
    assert not loads
    ws.stream_responses_websocket(_prepare(_messages(), search=False))
    assert not loads
    assert [t['name'] for t in conn.sent[0]['tools']] == ['read_files']
    assert _prepare(_messages()).body['tools'][0]['parameters']['properties']['vertical']['enum'] == [
        'auto', 'academic', 'off']
    domains.clear()  # the actual authority changed, not merely module residence
    plan = _prepare(_messages())
    assert plan.body['tools'][0]['parameters']['properties']['vertical']['enum'] == ['auto', 'off']
    assert loads == [True, True]
