"""Service thread commands and auto-reply (C5, C6).

The 大象 account commands and supervisor restart policy moved with the adapter to
bundles/yourprovider/tests/channels/test_dx_service_and_supervisor.py (docs/PUBLIC_CORE.md).
"""
import threading
from types import SimpleNamespace

import pytest

from lib.channels import autoreply, service
from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER

pytestmark = pytest.mark.unit

COMMAND = 'dx:gid:900'
IDENTITY = {'uid': OWNER_UID, 'mis': 'owner', 'name': 'Owner'}
ALICE = {'uid': '3003', 'mis': 'alice', 'name': 'Alice'}


@pytest.fixture(autouse=True)
def isolated_notice_recovery(monkeypatch):
    """The bounded recovery registry is process state; each spec owns its own."""
    monkeypatch.setattr(autoreply, '_NOTICE_RECOVERY_PENDING', {})
    monkeypatch.setattr(autoreply, '_NOTICE_RECOVERY_WORKER', None)


def test_enabling_a_thread_does_not_replay_its_ignored_past(store, router, enabled):
    enabled()
    group = ThreadKey.parse('dx:gid:500')
    store.update_thread(group, lambda value: {**value, 'watermark': '7000000000000000001'})
    view = service.configure_thread(OWNER, str(group), mode='watch', store=store)
    assert view['mode'] == 'watch' and view['watermark'] is None
    assert store.thread(group)['sinceMs']
    assert service.configure_thread(OWNER, str(group), mode=None, store=store)['mode'] == 'ignore'
    with pytest.raises(ChannelError):
        service.configure_thread(OWNER, str(group), mode='command', store=store)


# ── auto-reply ────────────────────────────────────────────────────────

@pytest.fixture
def router_turns(monkeypatch):
    rows = {}
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda conv, turn, *, user_id, include_projection=True: rows.get((conv, turn)))

    def human(origin):
        rows[(ROUTER, 'in-1')] = {'conversationId': ROUTER, 'turnId': 'in-1', 'actor': 'human',
                                  'projection': {'content': 'q', 'origin': origin}}
        rows[(ROUTER, 'out-1')] = {'conversationId': ROUTER, 'turnId': 'out-1',
                                  'actor': 'assistant', 'parentTurnId': 'in-1',
                                  'projection': {'role': 'assistant'},
                                  'status': 'failed', 'currentAttemptId': 'attempt-1',
                                  'settlement': {'resumeOptions': [
                                      {'operation': 'continue',
                                       'anchor': {'type': 'replay_only', 'contentChars': 0}},
                                      {'operation': 'regenerate',
                                       'anchor': {'type': 'turn_start'}}]}}
    return human


@pytest.fixture
def command_origin(router_turns):
    return {'blockId': 'origin', 'initiator': 'human',
            'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}}


def test_task_event_forwards_only_canonical_intermediate_text_once(
        store, router, enabled, router_turns, command_origin, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    task = {'id': 'task-1', '_userId': OWNER, 'convId': ROUTER, '_turnId': 'out-1',
            'segments': [
                {'type': 'thinking', 'blockId': 'thinking:1', 'deliverable': False,
                 'text': 'private reasoning'},
                {'type': 'text', 'blockId': 'text:round-1', 'deliverable': False,
                 'text': 'Checking the project now.'},
                {'type': 'text', 'blockId': 'text:discarded', 'deliverable': False,
                 '_providerAttemptDiscarded': True, 'text': 'Discarded provider attempt'},
                {'type': 'text', 'blockId': 'text:terminal', 'deliverable': True,
                 'text': 'Final answer'},
            ]}
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get_owned', lambda *a, **k: task)
    payload = {'type': 'round_committed', '_ownerUserId': str(OWNER), 'conversationId': ROUTER,
               'turnId': 'out-1'}
    autoreply.on_task_event('chat', 'task-1', payload)
    autoreply.on_task_event('chat', 'task-1', payload)
    [item] = store.active_outbox()
    assert item.text == 'Checking the project now.'
    assert item.origin['kind'] == 'router_content'
    assert 'private reasoning' not in item.text and 'Final answer' not in item.text


def test_task_event_forwards_a_progress_board_summary_once(
        store, router, enabled, router_turns, command_origin, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    task = {'id': 'task-1', '_userId': OWNER, 'convId': ROUTER, '_turnId': 'out-1', 'segments': []}
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get_owned', lambda *a, **k: task)

    class Board:
        def __init__(self, owner, project):
            assert project == '/srv/router-home'
        def read(self):
            return {'board': {'revision': 3, 'summary': 'Two fixes are verified.'}}
    monkeypatch.setattr('lib.conversations.coordinator_progress.CoordinatorTaskProgress', Board)
    payload = {'type': 'tool_complete', '_ownerUserId': str(OWNER), 'conversationId': ROUTER,
               'turnId': 'out-1', 'toolName': 'update_task_progress',
               'toolContent': '{"revision": 3, "changed": true}'}
    autoreply.on_task_event('chat', 'task-1', payload)
    autoreply.on_task_event('chat', 'task-1', payload)
    [item] = store.active_outbox()
    assert item.text == '进度：Two fixes are verified.'
    assert item.origin['kind'] == 'router_progress'


def test_installed_push_observer_receives_a_real_chat_frame(
        store, router, enabled, router_turns, command_origin, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    task = {'id': 'task-1', '_userId': OWNER, 'convId': ROUTER, '_turnId': 'out-1',
            '_attemptId': 'attempt-1', 'segments': [{
                'type': 'text', 'blockId': 'text:round-1', 'deliverable': False,
                'text': 'I am checking it.'}]}
    monkeypatch.setattr('lib.tasks_pkg.manager.runtime.chat_task_runtime.get_owned', lambda *a, **k: task)
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt',
                        lambda attempt_id, *, user_id: {'commandId': 'channel-owner-command'})
    autoreply.acknowledge_command(OWNER, ROUTER, SimpleNamespace(
        channel='dx', thread=COMMAND, event_id=f'{COMMAND}/1'), 'started')
    monkeypatch.setattr(autoreply, '_INSTALLED', False)
    monkeypatch.setattr('lib.tasks_pkg.manager._terminal.register_terminal_listener', lambda listener: True)
    from lib.agent_core.push import PushHub
    hub = PushHub()
    monkeypatch.setattr('lib.agent_core.push.hub', hub)
    assert autoreply.install()
    hub.push_event('chat', 'task-1', {'type': 'tool_start', 'conversationId': ROUTER,
                                     'turnId': 'out-1'}, user_id=OWNER)
    items = store.active_outbox()
    assert [item.text for item in items] == ['收到，总负责人已开始处理。', 'I am checking it.']
    hub.stop()


def test_task_event_ignores_another_conversation(
        store, router, enabled, router_turns, command_origin):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    autoreply.on_task_event('chat', 'task-1', {
        'type': 'round_committed', '_ownerUserId': str(OWNER),
        'conversationId': 'another-conversation', 'turnId': 'out-1'})
    assert store.active_outbox() == []


def _done(conversation=ROUTER, **extra):
    # A completed Turn carries its provider finish (turn_verdict completion evidence).
    return {'id': 't', 'status': 'done', 'finishReason': 'stop', '_userId': OWNER,
            'convId': conversation, '_turnId': 'out-1', 'content': 'The answer', **extra}


def test_router_answer_to_a_command_chat_message_returns_to_that_thread(store, router, enabled, router_turns):
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'origin', 'initiator': 'human',
                  'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}})
    autoreply.on_task_terminal(_done())
    autoreply.on_task_terminal(_done())  # observer replay stays exactly once
    items = store.active_outbox()
    assert [item.text for item in items] == ['The answer', '总负责人处理完成。']
    assert [item.idempotency_key for item in items] == ['out-1', 'out-1:terminal:done']
    assert [item.origin['kind'] for item in items] == ['router_reply', 'router_status']


@pytest.mark.parametrize('origin, task', [
    ({'blockId': 'origin', 'initiator': 'channel',
      'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}}, _done()),
    ({'blockId': 'origin', 'initiator': 'human'}, _done()),
    ({'blockId': 'origin', 'initiator': 'human',
      'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}}, _done(conversation='worker')),
])
def test_no_auto_reply_for_nonhuman_input_other_conversations_or_failures(
        store, router, enabled, router_turns, origin, task, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns(origin)
    monkeypatch.setattr(autoreply, '_notify_notice_failure', lambda *args: None)
    autoreply.on_task_terminal(task)
    assert store.active_outbox() == []


def test_human_steer_into_a_notice_returns_the_final_answer_to_its_command_thread(
        store, router, enabled, monkeypatch):
    enabled(command_chat=COMMAND)
    from lib.turn_initiation import steer_author
    author = steer_author({'content': 'Answer in 大象', 'origin': {
        'initiator': 'human', 'channel': {
            'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/42']}}},
        actor='human')
    rows = {
        (ROUTER, 'in-1'): {
            'conversationId': ROUTER, 'turnId': 'in-1', 'actor': 'human',
            'projection': {'content': 'project notice', 'origin': {'initiator': 'peer'}},
        },
        (ROUTER, 'out-1'): {
            'conversationId': ROUTER, 'turnId': 'out-1', 'actor': 'assistant',
            'parentTurnId': 'in-1', 'projection': {'role': 'assistant', '_userSteerInjects': [{
                'blockId': 'injection:user-steer:channel-owner-42',
                'commandId': 'channel-owner-42', 'deliveryState': 'delivered',
                'author': author}]},
        },
    }
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda conv, turn, *, user_id, include_projection=True: rows[(conv, turn)])
    autoreply.on_task_terminal(_done(content='Here is the answer.'))
    items = store.active_outbox()
    assert [item.thread for item in items] == [COMMAND, COMMAND]
    assert [item.text for item in items] == ['Here is the answer.', '总负责人处理完成。']
    autoreply.on_task_terminal(_done(content='', status='error', finishReason='error'))
    [item] = store.active_outbox()[2:]
    assert item.thread == COMMAND and item.origin['kind'] == 'router_status'


@pytest.mark.parametrize('origin, outcome, status, reason', [
    ({'blockId': 'notice', 'initiator': 'channel', 'channel': {
        'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/99']}},
     'error', 'error', '尚未完成审阅'),
    ({'blockId': 'report', 'initiator': 'peer'}, 'interrupted', 'interrupted', '处理已超时或停止响应'),
])
def test_failed_notice_review_warns_the_command_chat_even_without_a_reply_origin(
        store, router, enabled, router_turns, origin, outcome, status, reason):
    enabled(command_chat=COMMAND)
    router_turns(origin)
    terminal = _done(content='', _silentTurn=True, status=status, finishReason='')
    if outcome == 'interrupted':
        # An invocation guard stop (stall/deadline) is the interrupted outcome.
        terminal.update(aborted=True, _invocationStopReason='stalled')
    for _ in range(3):
        autoreply.on_task_terminal(terminal)
    [item] = store.active_outbox()
    assert item.thread == COMMAND
    assert '未能完成一条通知的审阅' in item.text and reason in item.text
    assert item.idempotency_key == f'out-1:notice-review:{outcome}'
    assert item.reply_to == (f'{COMMAND}/99' if outcome == 'error' else None)


def test_native_rejected_human_declaration_does_not_report_completion(
        store, router, enabled, router_turns):
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'origin', 'initiator': 'human',
                  'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': []}})
    autoreply.on_task_terminal(_done(content='', finishReason='abnormal_stop',
                                    error={'kind': 'abnormal_stop', 'detail': 'Invalid control declaration'}))
    [item] = store.active_outbox()
    assert item.text == autoreply._TERMINAL_LABELS['error']
    assert item.idempotency_key == 'out-1:terminal:error'


def test_an_empty_stop_notice_failure_schedules_a_resume_and_warns_with_its_next_action(
        store, router, enabled, router_turns, monkeypatch):
    """An unreviewed empty_stop notice is never acked or left stranded: the
    existing resume command continues it in place as a bounded follow-up of
    the terminal observer, and the owner warning names that continuation
    rather than asking for a manual resend."""
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'report', 'initiator': 'peer'})
    resumed = []
    monkeypatch.setattr('lib.conversations.restart_resume.resume_failed_notice_turn',
                        lambda owner, conversation_id, turn_id=None: (
                            resumed.append((owner, conversation_id, turn_id)) or {
                                'outcome': 'resumed', 'operation': 'continue'}))
    task = _done(content='', status='error', finishReason='abnormal_stop',
                 error={'kind': 'abnormal_stop',
                        'message': 'Model repeatedly returned no usable terminal response '
                                   '(empty_stop); bounded recovery exhausted.',
                        'raw': 'bucket=empty_stop attempts=2/2 trace_id=t-1'})
    autoreply.on_task_terminal(task)
    autoreply.flush_notice_recovery_for_test()
    assert resumed == [(OWNER, ROUTER, 'out-1')]
    [item] = store.active_outbox()
    assert item.origin['kind'] == 'router_status'
    assert '未能完成一条通知的审阅' in item.text and 'empty_stop' in item.text
    assert '继续处理' in item.text and '请重新发送' not in item.text
    # Observer replay neither resumes twice nor duplicates the warning.
    autoreply.on_task_terminal(task)
    autoreply.flush_notice_recovery_for_test()
    assert len(resumed) == 1 and len(store.active_outbox()) == 1


def test_a_notice_recovery_refusal_keeps_the_owner_resend_next_action(
        store, router, enabled, router_turns, monkeypatch):
    """If the durable Turn no longer admits the in-place resume (owner steer,
    owner-authored Turn, only Regenerate), the recovery leaves it alone and the
    warning keeps the existing manual next action — never a claimed success."""
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'report', 'initiator': 'peer'})
    monkeypatch.setattr('lib.conversations.restart_resume.resume_failed_notice_turn',
                        lambda *args, **kwargs: {'outcome': 'not_resumed',
                                                 'reason': 'a current-Turn owner steer must be answered'})
    autoreply.on_task_terminal(_done(
        content='', status='error', finishReason='abnormal_stop',
        error={'kind': 'abnormal_stop', 'message': '(empty_stop) bounded recovery exhausted',
               'raw': 'bucket=empty_stop attempts=2/2'}))
    autoreply.flush_notice_recovery_for_test()
    [item] = store.active_outbox()
    assert '请重新发送该通知或在 Tofu 总负责人对话中处理' in item.text
    assert '继续处理' not in item.text


def test_a_genuine_provider_error_is_never_resumed_by_the_notice_recovery(
        store, router, enabled, router_turns, monkeypatch):
    """Reasoning-only/provider failures outside the exhausted empty_stop class
    keep their ordinary failed card and warning; no automatic continuation."""
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'report', 'initiator': 'peer'})
    monkeypatch.setattr('lib.conversations.restart_resume.resume_failed_notice_turn',
                        lambda *args, **kwargs: pytest.fail('genuine errors are not resumed'))
    autoreply.on_task_terminal(_done(
        content='', status='error', finishReason='error',
        error={'kind': 'provider_error', 'message': 'The provider returned 500.'}))
    autoreply.flush_notice_recovery_for_test()
    [item] = store.active_outbox()
    assert '请重新发送该通知' in item.text and 'provider returned 500' in item.text


def test_execution_limited_notice_warns_with_its_real_reason(
        store, router, enabled, router_turns):
    enabled(command_chat=COMMAND)
    router_turns({'initiator': 'peer'})
    autoreply.on_task_terminal(_done(
        content='', status='aborted', aborted=True, finishReason='aborted',
        _executionLimitReason='execution_tool_budget_exhausted'))
    [item] = store.active_outbox()
    assert 'execution_tool_budget_exhausted' in item.text
    assert item.idempotency_key == 'out-1:notice-review:error'


def test_missing_notice_turn_fails_closed_without_a_listener_exception(
        store, router, enabled, router_turns, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns({'initiator': 'peer'})
    from lib.turn_lifecycle import get_turn
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda conv, turn, *, user_id, include_projection=True: None)
    autoreply.on_task_terminal(_done(status='error', finishReason='error'))
    assert store.active_outbox() == []


def test_real_terminal_listener_survives_a_notice_storage_error(
        store, router, enabled, router_turns, monkeypatch):
    enabled(command_chat=COMMAND)
    router_turns({'initiator': 'peer'})
    from lib.tasks_pkg.manager import _terminal
    monkeypatch.setattr(_terminal, '_TERMINAL_LISTENERS', [])
    monkeypatch.setattr(_terminal, '_TERMINAL_REGISTRATIONS', {})
    monkeypatch.setattr(_terminal, '_TERMINAL_WITHDRAWN', set())
    monkeypatch.setattr('lib.agent_core.terminal_delivery.deliver_terminal', lambda task: None)
    monkeypatch.setattr('lib.turn_lifecycle.get_turn',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('storage unavailable')))
    assert _terminal.register_terminal_listener(autoreply.on_task_terminal)
    try:
        _terminal.notify_terminal_listeners(_done(
            status='error', finishReason='error', finished_at=1))
    finally:
        assert _terminal.unregister_terminal_listener(autoreply.on_task_terminal)


def test_deliberate_silence_leaves_no_empty_reply_or_false_activity_completion(
        store, router, enabled, router_turns, command_origin):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    autoreply.on_task_terminal(_done(content='', _silentTurn=True,
                                    _turnId='notice-out'))
    assert store.active_outbox() == []


def test_empty_success_still_reports_terminal_without_an_empty_reply(
        store, router, enabled, router_turns, command_origin):
    enabled(command_chat=COMMAND)
    router_turns(command_origin)
    autoreply.on_task_terminal(_done(content=''))
    [item] = store.active_outbox()
    assert item.text == '总负责人处理完成。'


@pytest.mark.parametrize('status, aborted, finish_reason, expected', [
    ('error', False, 'error', '总负责人处理失败，请重新发送或到 Tofu 查看详情。'),
    ('aborted', True, 'aborted', '总负责人已中止处理。'),
])
def test_failed_or_aborted_router_turn_reports_its_terminal_status(
        store, router, enabled, router_turns, status, aborted, finish_reason, expected):
    enabled(command_chat=COMMAND)
    router_turns({'blockId': 'origin', 'initiator': 'human',
                  'channel': {'channel': 'dx', 'thread': COMMAND, 'eventIds': [f'{COMMAND}/1']}})
    autoreply.on_task_terminal(_done(status=status, aborted=aborted, finishReason=finish_reason,
                                     content='partial answer'))
    [item] = store.active_outbox()
    assert item.text == expected
    assert item.origin['kind'] == 'router_status'
    assert item.idempotency_key == f'out-1:terminal:{status}'
