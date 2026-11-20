"""A lead's project message commits when its own arguments are complete (S2-13).

docs/PROJECT_COORDINATION.md, "A project message starts when its arguments are
complete": send_project_message declares a stream-start port; the real SSE
accumulator fires it the moment one call's arguments finish, so a busy
recipient's next round sees the message instead of waiting for the sender's
whole response. The ordinary dispatch of the same call adopts it, and a
message no dispatch adopted is retracted by an explicit correction. Calls whose
effect has no withdrawal (route_to_project, channel_send) stay held.
"""
import json
from types import SimpleNamespace
import time

import pytest

from lib.conversations import project_messages as pm
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from lib.tools.registry import settle_stream_starts, stream_start_port
from tests.project_mod.test_project_messages import setup

__all__ = ('setup',)

pytestmark = pytest.mark.unit


@pytest.fixture
def streaming(setup, monkeypatch):
    """A sender lead whose recipient lead is busy, plus the request's contract catalog."""
    from lib.tools.contracts import compile_execution_contract_documents
    monkeypatch.setattr(pm, 'lead_role', lambda owner, conv: ('project_router', '/project')
                        if conv == 'recipient' else None)
    wakes = []

    def busy_lane(conversation_id, message, tools, *, user_id, command_id, log_prefix):
        wakes.append(command_id)
        return SimpleNamespace(disposition='busy')
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', busy_lane)
    setup.sender['_toolContractDocumentsByName'] = compile_execution_contract_documents(pm.TOOLS)
    setup.accumulator = StreamingToolAccumulator(setup.sender, '/project')
    setup.wakes = wakes
    return setup


def _args(key='one', content='Check alpha'):
    return {'conversation_id': 'recipient', 'content': content, 'idempotency_key': key}


def _sse(calls):
    """OpenAI chat-completions frames: each call's header, then its arguments in two fragments."""
    frames = []
    for index, (call_id, name, arguments) in enumerate(calls):
        text = json.dumps(arguments)
        frames.append([{'index': index, 'id': call_id, 'type': 'function',
                        'function': {'name': name, 'arguments': ''}}])
        frames.append([{'index': index, 'function': {'arguments': text[:len(text) // 2]}}])
        frames.append([{'index': index, 'function': {'arguments': text[len(text) // 2:]}}])
    return ['data: ' + json.dumps({'id': 'resp', 'object': 'chat.completion.chunk', 'model': 'gpt-4o',
                                   'choices': [{'index': 0, 'delta': {'tool_calls': delta}}]})
            for delta in frames]


def _provider_stream(on_tool_call_ready):
    from lib.llm._sse_core import SSEAccumulator, prepare_request
    plan = prepare_request({'model': 'gpt-4o', 'stream': True, 'max_tokens': 64,
                            'messages': [{'role': 'user', 'content': 'relay'}]},
                           base_url='https://example.invalid/v1', api_key='test-key', api_protocol='openai')
    return SSEAccumulator(plan.body, plan.trace_id, plan.raw_dumper, plan.wire_translator, plan.t0,
                          on_tool_call_ready=on_tool_call_ready)


def _inbox(setup):
    return pm.execute('get_project_messages', {}, setup.recipient)['messages']


def _eventually(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(.005)
    return True


def _stream(setup, calls, upto=None):
    stream = _provider_stream(setup.accumulator.on_tool_call_ready)
    lines = _sse(calls)
    for line in lines[:upto]:
        stream.feed_line(line)
    if upto is None:
        stream.fire_final_tool_callback()
    return stream, lines


def test_the_message_reaches_a_busy_recipient_before_the_next_call_finishes_streaming(streaming):
    s = streaming
    # Call 1 complete; the long second call's header and half its arguments have streamed.
    _stream(s, [('call-1', 'send_project_message', _args()),
                ('call-2', 'send_project_message', _args('two', 'x' * 4000))], upto=5)
    assert _eventually(lambda: [m['content'] for m in _inbox(s)] == ['Check alpha']), \
        'the first message is durable for the busy recipient while the response still streams'
    assert s.wakes, 'the busy lead was offered the wake (it takes the message at its next round)'


def test_the_ordinary_dispatch_adopts_the_streamed_message(streaming):
    s = streaming
    _stream(s, [('call-1', 'send_project_message', _args())])
    assert _eventually(lambda: len(_inbox(s)) == 1)
    adopted = pm.execute('send_project_message', _args(), s.sender)
    settle_stream_starts(s.sender)
    [message] = _inbox(s)
    assert adopted['message']['id'] == message['id']


def test_a_discarded_response_retracts_its_message(streaming):
    s = streaming
    _stream(s, [('call-1', 'send_project_message', _args())])
    assert _eventually(lambda: len(_inbox(s)) == 1)
    original = _inbox(s)[0]
    settle_stream_starts(s.sender)  # the response never reached its dispatch
    messages = _inbox(s)
    assert len(messages) == 2
    retraction = next(m for m in messages if m['id'] != original['id'])
    assert retraction['kind'] == 'steer' and original['id'] in retraction['content']
    settle_stream_starts(s.sender)
    assert len(_inbox(s)) == 2, 'a retraction is sent once'


def test_a_reissue_with_changed_content_is_refused_and_the_streamed_message_retracted(streaming):
    s = streaming
    _stream(s, [('call-1', 'send_project_message', _args())])
    assert _eventually(lambda: len(_inbox(s)) == 1)
    with pytest.raises(pm.ProjectMessageError, match='changed'):
        pm.execute('send_project_message', _args(content='Check beta instead'), s.sender)
    settle_stream_starts(s.sender)
    assert len(_inbox(s)) == 2


def _bound_continuation(s, monkeypatch, mode):
    """The sender runs as a dispatch-bound continuation Turn, as every lead's
    "Continue the request" wake does (``KEY`` in its config, a granted role)."""
    from lib.project_dispatch import admission, scope
    binding = {'assignmentId': 'continuation-1', 'projectPath': '/project', 'owner': 1, 'tenant': 'default'}
    s.sender['config'] = {**s.sender['config'], admission.KEY: binding}
    s.sender[admission.ROLE_KEY] = {'binding': binding, 'mode': mode, 'coordinatorScope': None}
    admitted = []

    def admit_call(task, name, args, project_path=None, *, admission=None):
        admitted.append((name, project_path))
        return None, object()
    monkeypatch.setattr(scope, 'admit_call', admit_call)
    return admitted


def test_a_bound_coordinator_continuation_commits_its_message_mid_round(streaming, monkeypatch):
    """GR toolu_0151j3FajP71FwiMBuSyt4Gu: a send emitted mid-round in a continuation
    Turn waited 331 s for the round to end; it commits while the response streams."""
    s = streaming
    admitted = _bound_continuation(s, monkeypatch, 'coordinator')
    _stream(s, [('call-1', 'send_project_message', _args()),
                ('call-2', 'send_project_message', _args('two', 'x' * 4000))], upto=5)
    assert _eventually(lambda: [m['content'] for m in _inbox(s)] == ['Check alpha']), \
        'the continuation\'s first message is durable before its response ends'
    assert admitted == [('send_project_message', '/project')], \
        'the start passed the same dispatch admission its ordinary dispatch runs'


def test_a_continuation_whose_grant_refuses_the_call_holds_it(streaming, monkeypatch):
    from lib.project_dispatch import scope
    s = streaming
    _bound_continuation(s, monkeypatch, 'coordinator')

    def refuse(task, name, args, project_path=None, *, admission=None):
        raise scope.ToolAdmissionRefused('Coordinator call exceeds its original continuation policy',
                                         tool_name=name)
    monkeypatch.setattr(scope, 'admit_call', refuse)
    _stream(s, [('call-1', 'send_project_message', _args())])
    time.sleep(.2)
    assert _inbox(s) == []


def test_a_dispatched_worker_never_stream_starts(streaming, monkeypatch):
    s = streaming
    admitted = _bound_continuation(s, monkeypatch, 'project')
    _stream(s, [('call-1', 'send_project_message', _args())])
    time.sleep(.2)
    assert _inbox(s) == [] and admitted == [], 'a Worker\'s send keeps its fence until dispatch'


@pytest.mark.parametrize('name', ['route_to_project', 'channel_send', 'consider_project_message'])
def test_calls_without_a_withdrawal_wait_for_their_final_response(name):
    assert stream_start_port(name) is None
    assert stream_start_port('send_project_message') is not None


def test_a_keyed_write_keeps_its_worker_fence():
    """The port names its key instead of declaring the send idempotent: a dispatched
    Worker's unconfirmed send stays reconciled (test_tool_effect_declarations)."""
    from lib.tools.registry import StreamStartPort, ToolSpec, declared_effect_recovery
    from lib.tools.registry import _spec
    assert stream_start_port('send_project_message').key_argument == 'idempotency_key'
    assert declared_effect_recovery('send_project_message') == 'reconcile'
    keyed = ToolSpec('stream-start-keyed-probe', lambda ctx: [], provides=frozenset({'keyed_probe'}),
                     write_tools=frozenset({'keyed_probe'}), stream_start_by_name={'keyed_probe': StreamStartPort(
                         start=lambda *a: None, settle=lambda task: None, key_argument='key')})
    specs, keys = list(_spec._TOOL_SPECS), set(_spec._REGISTERED_KEYS)
    try:
        _spec._register_tool_spec_unlocked(keyed, replace=False)
        assert keyed in _spec._TOOL_SPECS
    finally:
        _spec._TOOL_SPECS[:] = specs
        _spec._REGISTERED_KEYS.clear()
        _spec._REGISTERED_KEYS.update(keys)
