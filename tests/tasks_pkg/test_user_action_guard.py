"""A Turn honors a tool error whose contract requires the user.

Incident (2026-11-13, knowledge project, conversation
worker-5254969445da0e87b6b0b572): one Worker Turn called
``mcp__xuecheng__create_doc`` 133 times with different titles ('placeholder',
then 'ignore' … 'ignore100'), interleaved with other work. 132 calls returned
the shared SSO adapter's typed ``SSO_LOGIN_NOT_APPROVED`` error, and each one
re-sent a 大象 login approval to the owner. The tool-loop breaker never
fired: the arguments differed, other tools ran in between and none was a shell
command.

The rule is the error's contract, not a count: after one result declares that
the user must act, later calls of the Turn that need the same authority are not
sent, and the Turn stops as the user-action guard, a Tofu protective stop that
names the tool, the error code and the required action
(docs/contracts/TOOL_EXECUTION_POLICY.md, "Errors that need the user").
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge
from lib.tasks_pkg.handlers.mcp import handle_mcp_tool, handle_progressive_mcp_tool
from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop
from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason
from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.result_envelope import tool_result_error
from lib.turn_verdict import AbortInitiator, abort_cause, derive_task_verdict
from tests._registered_chat_task import registered_chat_task
from tests.tasks_pkg.test_root_orchestrator_agent_loop_adapter import (
    _install_common, _install_script, _request, _state,
)

pytestmark = pytest.mark.unit

# The adapter's envelope after SSO-SESSION-REUSE-1, and the one it sent during
# the incident; both declare that the user must act.
NOT_APPROVED = {
    'ok': False, 'error_code': 'SSO_LOGIN_NOT_APPROVED',
    'error': '大象登录授权未在有效期内获批。', 'auth_source': 'mtsso_shared_session',
    'auth_state': 'login_required', 'login_attempted': False, 'retryable': False,
    'user_action_required': True, 'model_action': 'ask_user_then_request_login',
    'recovery_action': 'request_new_daxiang_login',
}
INCIDENT_ENVELOPE = {
    **NOT_APPROVED, 'login_attempted': True, 'retryable': True,
    'error': '大象登录授权未在有效期内获批；用户确认后重试原操作即可（重试会重新发送一次授权请求）。',
    'model_action': 'ask_user_to_approve_then_retry', 'recovery_action': 'approve_daxiang_login',
}
CREATED = {'ok': True, 'contentId': 4001, 'title': 'notes'}


class Server:
    """One connected MCP server answering every call with ``answer(tool, arguments)``."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        self.calls.append((name, dict(arguments or {})))
        payload = self.answer(name, arguments or {})
        return SimpleNamespace(
            content=[SimpleNamespace(type='text', text=json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload, isError=payload.get('ok') is False)


@pytest.fixture
def servers(monkeypatch):
    """xuecheng and hope behind the real bridge and MCP handlers."""
    bridge = MCPBridge()
    connected = {}

    def connect(name, answer):
        handle = mc._MCPServerHandle(name, {'timeout': 60})
        handle.session = connected[name] = Server(answer)
        bridge._servers[name] = handle
        for tool, read_only in (('create_doc', False), ('read_doc', True)):
            namespaced = f'mcp__{name}__{tool}'
            bridge._tool_index[namespaced] = {
                'server_name': name, 'tool_name': tool, 'namespaced_name': namespaced,
                'description': '', 'input_schema': {}, 'openai_def': {},
                'read_only_hint': read_only}
        return connected[name]

    monkeypatch.setattr(bridge, '_run_async', lambda coro, timeout=None, **_: asyncio.run(coro))
    monkeypatch.setattr('lib.mcp.get_bridge', lambda _owner: bridge)
    return connect


def call(index, name, arguments):
    return {'id': f'call-{index}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def tool_round(*calls):
    return ({'role': 'assistant', 'content': '', 'tool_calls': list(calls)}, 'tool_calls', {})


def incident_script(name='mcp__xuecheng__create_doc', wrap=lambda arguments: arguments):
    """create_doc with a new title each time, interleaved with other work."""
    titles = iter(['placeholder', 'ignore', 'ignore1', 'ignore2'])
    return [
        tool_round(call(0, name, wrap({'title': next(titles)}))),
        tool_round(call(1, 'read_files', {'reads': [{'path': 'notes.md'}]})),
        tool_round(call(2, name, wrap({'title': next(titles)}))),
        tool_round(call(3, 'read_files', {'reads': [{'path': 'outline.md'}]}),
                   call(4, name, wrap({'title': next(titles)}))),
        tool_round(call(5, name, wrap({'title': next(titles)}))),
        ({'role': 'assistant', 'content': 'Done.'}, 'stop', {}),
    ]


def run_turn(monkeypatch, script, *, conversation='user-action-conversation', before_round=None):
    """Run one real root Turn whose tool rounds go through the real dispatch pipeline."""
    reads = []

    def read_files(task, tc, name, call_id, arguments, number, row, *rest):
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        reads.append(call_id)
        _finalize_tool_round(task, number, row, [{'content': 'local notes'}])
        return call_id, 'local notes', False

    handlers = {'read_files': read_files, 'call_mcp_write_tool': handle_progressive_mcp_tool,
                'call_mcp_read_tool': handle_progressive_mcp_tool}
    tools = [{'type': 'function', 'function': {'name': name}}
             for name in ('read_files', 'mcp__xuecheng__create_doc', 'call_mcp_write_tool')]
    task = {
        'id': f'task-{conversation}', 'convId': conversation, 'model': 'kimi-k3',
        'config': {}, 'toolRounds': [], 'aborted': False, 'error': None,
        'abort_event': threading.Event(), '_tool_schema': tools, '_attended': False,
        'events': [], 'events_lock': threading.Lock(),
        '_tool_env': SimpleNamespace(resolve=lambda name: handlers.get(name, handle_mcp_tool)),
    }
    state, events = _state(), []
    request = _request(task, state)
    request.tool_list = tools
    request.messages.append({'role': 'user', 'content': 'Put the meeting notes into 学城.'})
    _install_common(monkeypatch, events)
    monkeypatch.setattr(root_loop, 'apply_stream_decision',
                        lambda *a, **k: ('proceed', k['premature_retry_count']))
    _install_script(monkeypatch, state, script, events)

    def dispatch(task, state, messages, search_text, *, round_num, **_):
        if before_round is not None:
            before_round(task, round_num)
        message = state.assistant_msg
        messages.append(message)
        parsed = []
        for tool_call in message['tool_calls']:
            name = tool_call['function']['name']
            arguments = json.loads(tool_call['function']['arguments'])
            _, row, _ = _build_tool_round_entry(
                name, arguments, tool_call['id'], tool_call['function']['arguments'],
                len(task['toolRounds']) + 1, False)
            row['llmRound'] = round_num
            task['toolRounds'].append(row)
            parsed.append((tool_call, name, tool_call['id'], arguments, row['roundNum'], row, None))
        return execute_tool_pipeline(
            task, parsed, cfg={'autoApply': True}, project_path=None, project_enabled=False,
            tool_list=[], messages=messages, all_search_results_text=search_text,
            round_num=round_num, model='kimi-k3')

    monkeypatch.setattr(root_loop, 'run_tool_dispatch', dispatch)
    with registered_chat_task(task, user_id=71):
        result = root_loop.run_root_agent_loop(request)
    return SimpleNamespace(task=task, result=result, messages=request.messages, reads=reads,
                           model_rounds=[event[1] for event in events if event[0] == 'llm'],
                           events=events)


def tool_message(turn, call_id):
    return next(message for message in turn.messages
                if message.get('role') == 'tool' and message.get('tool_call_id') == call_id)


def row(turn, call_id):
    return next(entry for entry in turn.task['toolRounds'] if entry.get('toolCallId') == call_id)


@pytest.mark.parametrize('envelope', [NOT_APPROVED, INCIDENT_ENVELOPE], ids=['adapter', 'incident'])
def test_the_incident_turn_stops_calling_the_tool_and_surfaces_the_need(
        monkeypatch, servers, envelope):
    xuecheng = servers('xuecheng', lambda tool, arguments: envelope)
    turn = run_turn(monkeypatch, incident_script())

    # One call reached the server; the next one that needed it was not sent.
    assert xuecheng.calls == [('create_doc', {'title': 'placeholder'})]
    assert turn.reads == ['call-1']
    blocked = tool_result_error(tool_message(turn, 'call-2')['content'])
    assert (blocked.code, blocked.retryable) == ('awaiting_user_action', False)
    assert row(turn, 'call-2')['status'] == 'rejected'
    assert row(turn, 'call-2')['rejection']['kind'] == 'awaiting_user_action'

    # The Turn stopped at that round instead of asking the model again.
    assert turn.model_rounds == [0, 1, 2]
    assert turn.result.outcome.halted
    assert turn.result.outcome.exit_reason == 'awaiting_user_action'
    assert turn.task['_abort_reason'] == 'awaiting_user_action'
    assert abort_cause(turn.task['_abort_reason']).initiator is AbortInitiator.TOFU_GUARD

    # It surfaces once, as one typed error naming the tool, the code and the action.
    error = turn.task['error']
    assert (error['kind'], error['retryable']) == ('tool_call_rejected', False)
    assert error['userAction'] == {
        'scope': 'mcp:xuecheng', 'tool': 'mcp__xuecheng__create_doc',
        'errorCode': 'SSO_LOGIN_NOT_APPROVED', 'requiredAction': envelope['recovery_action'],
        'modelAction': envelope['model_action'], 'message': envelope['error'][:500]}
    for text in (error['detail'], error['hint']):
        assert 'mcp__xuecheng__create_doc' in text and 'SSO_LOGIN_NOT_APPROVED' in text
        assert envelope['recovery_action'] in text
    assert error['failureAttribution']['toolCallId'] == 'call-2'
    stops = [event for kind, *rest in turn.events if kind == 'wire_event'
             for event in rest if event.get('reason') == 'awaiting_user_action']
    assert len(stops) == 1

    # Settlement keeps that error and fails the Turn as a Tofu guard's stop, never a person's.
    finish = _settle_post_loop_finish_reason(
        turn.task, 'tool_calls', loop_exit_reason='awaiting_user_action',
        abort_detected_phase='', model='kimi-k3', tid='user-action')
    assert finish == 'error' and turn.task['error'] is error
    verdict = derive_task_verdict({**turn.task, 'status': 'done', 'finishReason': finish})
    assert (verdict.status.value, verdict.cause) == ('failed', 'generation_error')


def test_a_progressive_wrapper_call_meets_the_same_requirement(monkeypatch, servers):
    xuecheng = servers('xuecheng', lambda tool, arguments: NOT_APPROVED)
    turn = run_turn(monkeypatch, incident_script(
        'call_mcp_write_tool',
        lambda arguments: {'name': 'mcp__xuecheng__create_doc', 'arguments': arguments}))
    assert xuecheng.calls == [('create_doc', {'title': 'placeholder'})]
    assert turn.model_rounds == [0, 1, 2]
    assert turn.task['error']['userAction']['tool'] == 'mcp__xuecheng__create_doc'


@pytest.mark.parametrize('failure', [
    {'ok': False, 'error_code': 619, 'error': '对不起，您没有查看文档的权限'},
    {'ok': False, 'error_code': 'SSO_NODE_NOT_FOUND', 'auth_state': 'blocked', 'retryable': False,
     'user_action_required': False, 'model_action': 'stop_and_report',
     'recovery_action': 'repair_agent_configuration'},
    {'ok': False, 'error_code': 'INTERNAL_ERROR', 'error': 'internal tool failure', 'retryable': True},
], ids=['per-document', 'configuration', 'retryable'])
def test_an_error_that_does_not_need_the_user_leaves_the_turn_alone(monkeypatch, servers, failure):
    xuecheng = servers('xuecheng', lambda tool, arguments: failure)
    turn = run_turn(monkeypatch, incident_script())
    assert [arguments['title'] for _tool, arguments in xuecheng.calls] == [
        'placeholder', 'ignore', 'ignore1', 'ignore2']
    assert turn.model_rounds == [0, 1, 2, 3, 4, 5]
    assert not turn.result.outcome.halted
    assert not turn.task.get('error') and not turn.task.get('_abort_reason')


def test_a_retry_after_a_transient_error_reaches_the_tool(monkeypatch, servers):
    answers = iter([{'ok': False, 'error_code': 'INTERNAL_ERROR', 'retryable': True},
                    CREATED, CREATED, CREATED])
    xuecheng = servers('xuecheng', lambda tool, arguments: next(answers))
    turn = run_turn(monkeypatch, incident_script())
    assert len(xuecheng.calls) == 4
    assert tool_result_error(tool_message(turn, 'call-2')['content']) is None
    assert not turn.task.get('error')


def test_a_requirement_holds_only_for_the_server_that_declared_it(monkeypatch, servers):
    xuecheng = servers('xuecheng', lambda tool, arguments: NOT_APPROVED)
    hope = servers('hope', lambda tool, arguments: CREATED)
    script = [
        tool_round(call(0, 'mcp__xuecheng__create_doc', {'title': 'placeholder'})),
        tool_round(call(1, 'mcp__hope__read_doc', {'job': 1}),
                   call(2, 'read_files', {'reads': [{'path': 'notes.md'}]})),
        ({'role': 'assistant', 'content': '学城 needs your 大象 approval.'}, 'stop', {}),
    ]
    turn = run_turn(monkeypatch, script)
    assert len(xuecheng.calls) == 1 and hope.calls == [('read_doc', {'job': 1})]
    assert turn.reads == ['call-2']
    # A model that answers the user itself ends its Turn normally.
    assert turn.model_rounds == [0, 1, 2]
    assert not turn.result.outcome.halted and not turn.task.get('error')


def test_a_person_steer_lifts_the_requirement(monkeypatch, servers):
    """New user input clears it: the next call reaches the server again."""
    from lib.agent_inbox import enqueue
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox

    answers = iter([NOT_APPROVED, CREATED])
    xuecheng = servers('xuecheng', lambda tool, arguments: next(answers))

    def steer_before_second_call(task, round_num):
        if round_num != 2:
            return
        assert enqueue(task['convId'], 'I approved the 大象 login; go on.', mode='user-steer')
        drain_and_inject_inbox(task=task, messages=[{'role': 'user', 'content': 'go'}],
                               round_num=round_num, tid='steer')

    script = [
        tool_round(call(0, 'mcp__xuecheng__create_doc', {'title': 'placeholder'})),
        tool_round(call(1, 'read_files', {'reads': [{'path': 'notes.md'}]})),
        tool_round(call(2, 'mcp__xuecheng__create_doc', {'title': 'notes'})),
        ({'role': 'assistant', 'content': 'Created.'}, 'stop', {}),
    ]
    turn = run_turn(monkeypatch, script, conversation='user-action-steer',
                    before_round=steer_before_second_call)
    assert [arguments['title'] for _tool, arguments in xuecheng.calls] == ['placeholder', 'notes']
    assert turn.model_rounds == [0, 1, 2, 3]
    assert not turn.result.outcome.halted and not turn.task.get('error')
