"""Conversations a restart interrupted resume from their checkpoint by themselves.

docs/RUN_CONTINUITY.md, "Restart-paused conversations resume by themselves".
A stopping server records which conversations it quiesced; the next server
generation continues every main-lane tail Turn the restart settled
``interrupted`` (cause ``server_restart``) through the ordinary resume command,
exactly as the owner's Continue click would, once per interrupted Attempt.
Turns other owners resume (dispatched Workers and Coordinator wakes, Goal/Flow
runs), Turns waiting for the owner's answer, and anything the owner stopped are
never touched. Real Turn transactions on SQL and native authorities; only the
executor start is a seam (no model runs).
"""
from copy import deepcopy
import time
from types import SimpleNamespace
import uuid

import pytest

from tests.support.dx_overlay import requires_dx

from tests.turn_lifecycle.test_task_event_delivery import storage as storage
from lib import boot_identity
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.conversation_sync.execution_identity import EXECUTION_IDENTITY_CONFIG_KEY, capture_execution_identity
from lib.conversations import restart_resume
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit

__all__ = ('storage',)

OWNER = 1
RESTART = {'verdict': 'clean', 'reason': 'restart', 'restart_storm': False}


@pytest.fixture
def chats(storage, monkeypatch, tmp_path):
    """Running conversations of one owner, a stopping generation and the next one."""
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import runtime

    from lib import lifecycle_approval as gate
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=OWNER)
    monkeypatch.setattr(boot_identity, 'BOOT_ID', 'stopping-generation')

    def accepted_config(model='chat-model', **extra):
        return capture_execution_identity({'model': model, 'projectPath': str(tmp_path),
                                           'thinkingEnabled': True, **extra}, OWNER, principal)

    def executing(conversation, pair, config, task_id, *, content='', thinking='', model='chat-model',
                  toolRounds=None, steers=None):
        turns.bind_task(pair['attempt']['attemptId'], task_id, user_id=OWNER)
        task = {'id': task_id, 'convId': conversation, '_turnId': pair['turn']['turnId'],
                '_attemptId': pair['attempt']['attemptId'], '_userId': OWNER, '_attended': True,
                '_principalContext': principal.to_payload(), 'config': deepcopy(config),
                'status': 'running', 'model': model, 'content': content, 'thinking': thinking,
                'toolRounds': list(toolRounds or []),
                '_userSteerInjects': list(steers or [])}
        turns.record_task_event(task, {'type': 'thinking', 'content': thinking} if thinking
                                else {'type': 'delta', 'content': content})
        return task

    def running(conversation, *, content='', thinking='', model='chat-model',
                inputProjection=None, toolRounds=None, steers=None, **extra):
        config = accepted_config(model, **extra)
        projection = inputProjection or {'content': 'Keep working on ' + conversation}
        pair = turns.create_turn_pair(conversation, command_id=conversation + ':input',
            input_projection=projection, config=deepcopy(config),
            user_id=OWNER, conversation_defaults={'allowCreate': True, 'title': conversation,
                                                  'settings': {'projectPath': str(tmp_path)}})
        return executing(conversation, pair, config, conversation + '-task', content=content,
                         thinking=thinking, model=model, toolRounds=toolRounds, steers=steers)

    def report(conversation, title):
        """A Coordinator report: a settled Turn appended while the long Turn runs."""
        return turns.append_settled_turn(
            conversation, command_id=f'{conversation}:report:{title}', actor='assistant',
            kind='coordinator_report.progress', user_id=OWNER,
            projection={'content': title, 'origin': {'initiator': 'brain'}})['turn']

    def queue_behind(conversation, text):
        """The owner's input sent while the lane is busy: the browser's queued pair."""
        queue_id = str(uuid.uuid4())
        pair = turns.create_turn_pair(
            conversation, command_id=f'{conversation}:queued:{queue_id}',
            input_projection={'content': text}, config=deepcopy(accepted_config()), user_id=OWNER,
            queue_binding={'queueId': queue_id, 'kind': 'real', 'priority': 100,
                           'createdAt': int(time.time() * 1000), 'message': {'text': text}})
        return queue_id, pair

    def take_over(conversation, queue_id, content):
        """The queue drain activates the queued pair; its reply runs and completes."""
        activated = turns.activate_queued_turn_pair(conversation, queue_id, user_id=OWNER)
        task = executing(conversation, activated, accepted_config(), conversation + '-queued-task',
                         content=content)
        task.update(status='done', finishReason='stop')
        turns.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
        return activated

    def settle(task, abort_reason):
        task.update(status='aborted', aborted=True, _abort_reason=abort_reason)
        turns.record_task_event(task, {'type': 'aborted'})

    started = []

    def start(conv, cfg, request, abort_after, registered, **kwargs):
        task_id = f'resumed-{len(started)}'
        registered(task_id)
        started.append((conv, deepcopy(cfg)))
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda *a: {},
        was_aborted_after=lambda *a: False, start_task=start)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    # The scheduled-turn command (restart notices) binds the service at import.
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.conversation_turn_commands', commands)

    generations = []

    def next_generation():
        generations.append(len(generations) + 1)
        monkeypatch.setattr(boot_identity, 'BOOT_ID', f'generation-{generations[-1]}')
        monkeypatch.setattr(restart_resume, '_PASS_FINISHED', False)
        monkeypatch.setattr(restart_resume, '_LAST_RESULTS', [])
    return SimpleNamespace(turns=turns, running=running, settle=settle, started=started,
                           commands=commands, next_generation=next_generation, report=report,
                           queue_behind=queue_behind, take_over=take_over)


def _tail(c, conversation):
    page = c.turns.list_turns(conversation, user_id=OWNER, lane_id='main')
    return page['turns'][-1]


def _lane(c, conversation):
    """Every main-lane Turn with its current Attempt: what a resume could delete or supersede."""
    return [(turn['turnId'], turn['currentAttemptId'])
            for turn in c.turns.list_turns(conversation, user_id=OWNER, lane_id='main')['turns']]


def _join_settlement_drain(conversation):
    """The settlement drain runs on its own thread; wait for this conversation's."""
    import threading
    for thread in threading.enumerate():
        if thread.name == f'turn-native-queue-drain-{conversation}':
            thread.join(60)
            assert not thread.is_alive()


@pytest.mark.parametrize('previous_shutdown,resumed', [
    (None, True),
    ({'verdict': 'clean', 'reason': 'manual', 'restart_storm': False}, False),
    ({'verdict': 'crash', 'reason': 'crash', 'restart_storm': True}, False),
])
def test_a_turn_recovery_settles_in_a_serving_process_resumes_without_a_boot_pass(
        chats, monkeypatch, previous_shutdown, resumed):
    """RESTART-DRILL-1 storage-replay and kill-execution (2026-11-12): durable worker
    recovery settled owner chats and Router Turns interrupted in a SERVING process
    (their process died, or a storage restart fenced their claim), and no boot pass
    ever listed them, so none resumed. The settlement drain continues such a Turn by
    the restart rule, once, unless this generation stands down (manual shutdown or a
    restart storm)."""
    c = chats
    monkeypatch.setattr(restart_resume, '_PREVIOUS_SHUTDOWN', previous_shutdown)
    task = c.running('chat', content='Reading files')
    c.settle(task, 'restart_recovery')
    _join_settlement_drain('chat')
    turn = c.turns.get_turn('chat', task['_turnId'], user_id=OWNER)
    if not resumed:
        assert c.started == [] and turn['currentAttemptId'] == task['_attemptId']
        assert turn['status'] == 'interrupted'
        return
    assert [conversation for conversation, _ in c.started] == ['chat']
    assert turn['currentAttemptId'] != task['_attemptId']
    accepted = c.turns.get_attempt(turn['currentAttemptId'], user_id=OWNER)
    assert accepted['commandId'] == 'restart-resume:' + task['_attemptId']
    # The boot pass or a Coordinator recovery naming the same Attempt replays nothing.
    again = restart_resume.resume_recovered_turn(OWNER, 'chat', task['_turnId'], task['_attemptId'])
    assert again['outcome'] == 'not_resumed' and len(c.started) == 1


@pytest.mark.parametrize('abort_reason', ['server_shutdown', 'restart_recovery'])
def test_a_restart_interruption_that_produced_nothing_resumes_from_its_input(chats, abort_reason):
    """B7 (2026-11-12): a Turn the restart interrupted before it produced anything (a model
    that does not stream its reasoning, no call yet) offered only Regenerate, so no resume
    took it and its work was lost across the restart. It continues from its input, once,
    through the next generation's pass (quiesce) or the serving process's settlement drain
    (worker recovery), by the same command id and stand-down as any interrupted Turn."""
    c = chats
    task = c.running('slow-model')
    restart_resume.record_quiesced_conversations([task])
    c.settle(task, abort_reason)
    if abort_reason == 'server_shutdown':
        interrupted = c.turns.get_turn('slow-model', task['_turnId'], user_id=OWNER)
        assert interrupted['projection'].get('toolRounds') in (None, [])
        assert [option['operation'] for option in interrupted['settlement']['resumeOptions']] == [
            'continue', 'regenerate']
        c.next_generation()
        [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
        assert (item['outcome'], item['operation']) == ('resumed', 'continue')
    else:
        _join_settlement_drain('slow-model')
    turn = c.turns.get_turn('slow-model', task['_turnId'], user_id=OWNER)
    accepted = c.turns.get_attempt(turn['currentAttemptId'], user_id=OWNER)
    assert (accepted['commandId'], accepted['operation']) == (
        'restart-resume:' + task['_attemptId'], 'continue')
    [(conversation, config)] = c.started
    assert conversation == 'slow-model' and config['_turnOperation'] == 'continue'
    again = restart_resume.resume_recovered_turn(OWNER, 'slow-model', task['_turnId'],
                                                 task['_attemptId'])
    assert again['outcome'] == 'not_resumed' and len(c.started) == 1


@pytest.mark.parametrize('abort_reason', ['server_shutdown', 'restart_recovery'])
def test_a_restart_interruption_holding_a_cut_call_is_never_continued(chats, abort_reason):
    """RESTART-DRILL-1 on GATE-R4's B7 carry 1ae2d8667c (2026-11-13): B7 offered Continue to
    a Turn whose first call the restart cut off before its result, and that Continue ran
    the call's side effect a second time. Unlike a Turn that produced nothing, it keeps its
    held settlement: Regenerate alone, so neither the next generation's pass (quiesce) nor
    the serving process's settlement drain (worker recovery) continues it."""
    c = chats
    cut_call = {'toolName': 'run_command', 'toolCallId': 'call-write', 'status': 'running',
                'roundNum': 1, 'llmRound': 0, 'toolArgs': '{"command": "make deploy"}',
                'executionEffect': {'state': 'entered', 'recovery': 'reconcile'}}
    task = c.running('held', toolRounds=[cut_call])
    restart_resume.record_quiesced_conversations([task])
    c.settle(task, abort_reason)
    interrupted = c.turns.get_turn('held', task['_turnId'], user_id=OWNER)
    assert (interrupted['status'], interrupted['settlement']['cause']) == (
        'interrupted', 'server_restart')
    assert [option['operation'] for option in interrupted['settlement']['resumeOptions']] == [
        'regenerate']
    if abort_reason == 'server_shutdown':
        c.next_generation()
        [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
        assert item['outcome'] == 'not_resumed' and 'only regenerate' in item['reason']
    else:
        _join_settlement_drain('held')
    assert c.started == []
    turn = c.turns.get_turn('held', task['_turnId'], user_id=OWNER)
    assert (turn['status'], turn['currentAttemptId']) == ('interrupted', task['_attemptId'])


def test_a_long_turn_a_queued_input_took_over_is_reported_superseded_and_nothing_is_deleted(chats):
    """2026-11-10 04:37: Coordinator 1a390889's long Turn 02bde90f was restart-paused.
    It had Coordinator reports after it and the owner's input adcfbae3 queued behind it,
    and the queue took over at 04:37:22. The pass and the notice read only the lane tail,
    a report, and the notice said "completed (ingested)". The record names the interrupted
    Turn. turn.attempt.create deletes every later Turn of a resumed Turn's lane, so a
    non-tail Turn never resumes in place: no Attempt, no deletion, and a typed verdict."""
    c = chats
    long_turn = c.running('coordinator', content='Long request work')
    c.report('coordinator', 'Progress while the long Turn runs')
    queue_id, queued = c.queue_behind('coordinator', 'The owner\u2019s next request')
    restart_resume.record_quiesced_conversations([long_turn])
    c.settle(long_turn, 'server_shutdown')
    c.take_over('coordinator', queue_id, 'Answer to the next request')
    lane = _lane(c, 'coordinator')
    interrupted = c.turns.get_turn('coordinator', long_turn['_turnId'], user_id=OWNER)
    assert interrupted['settlement']['cause'] == 'server_restart'
    since = interrupted['createdAt'] / 1000
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    input_id = queued['submittedTurn']['turnId']
    assert item['turnId'] == long_turn['_turnId']
    assert item['outcome'] == 'superseded' and item['supersededBy'] == input_id
    assert 'superseded by queued input ' + input_id in item['reason']
    assert c.started == [] and _lane(c, 'coordinator') == lane
    assert restart_resume.describe_conversation(OWNER, 'coordinator', since).startswith(
        'superseded by queued input ' + input_id)
    # The project dispatch path names the same Turn and reaches the same verdict.
    again = restart_resume.resume_conversation(OWNER, 'coordinator', turn_id=long_turn['_turnId'],
                                               attempt_id=long_turn['_attemptId'])
    assert again['outcome'] == 'superseded' and c.started == [] and _lane(c, 'coordinator') == lane


def test_a_request_turn_followed_only_by_its_reports_resumes_in_place_and_keeps_them(chats):
    """2026-11-11 18:28: the parallel Router's request Turn 8c1d64fe, answering
    owner-forwarded input ca86e183, had posted a progress report mid-turn. Recovery
    read the report as the lane tail and never continued the Turn, so the next Turn
    on the lane was a synthetic wake scoped to another request's Workers. A resume
    preserves every later Turn, so the request Turn continues in place, with its own
    authority, and both reports stay on the lane."""
    c = chats
    long_turn = c.running('coordinator', content='Long request work')
    c.report('coordinator', 'First progress report')
    c.report('coordinator', 'Second progress report')
    restart_resume.record_quiesced_conversations([long_turn])
    c.settle(long_turn, 'server_shutdown')
    lane = _lane(c, 'coordinator')
    since = c.turns.get_turn('coordinator', long_turn['_turnId'], user_id=OWNER)['createdAt'] / 1000
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert item['turnId'] == long_turn['_turnId'] and item['outcome'] == 'resumed'
    assert item['operation'] in restart_resume.RESUME_OPERATIONS
    assert [conversation for conversation, _ in c.started] == ['coordinator']
    after = _lane(c, 'coordinator')
    # Same Turns in the same order: the request Turn under its resumed Attempt, both
    # reports untouched, and no new input (no synthetic wake) on the lane.
    assert [turn_id for turn_id, _ in after] == [turn_id for turn_id, _ in lane]
    resumed = dict(after)[long_turn['_turnId']]
    assert resumed == item['attemptId'] and resumed != long_turn['_attemptId']
    assert [attempt for turn_id, attempt in after if turn_id != long_turn['_turnId']] == [
        attempt for turn_id, attempt in lane if turn_id != long_turn['_turnId']]
    state = restart_resume.describe_conversation(OWNER, 'coordinator', since)
    assert state.startswith('resumed from its checkpoint'), state
    # Coordinator recovery names the same Turn and Attempt: one resume, never two.
    again = restart_resume.resume_conversation(OWNER, 'coordinator', turn_id=long_turn['_turnId'],
                                               attempt_id=long_turn['_attemptId'])
    assert again['outcome'] == 'not_resumed' and 'already continued' in again['reason']
    assert len(c.started) == 1


def test_the_restart_resume_pass_and_project_dispatch_resume_one_interruption_once(chats):
    """Two stopping processes recorded the same Turn, and Coordinator recovery names it too
    (continuation's resume_source): one resume Attempt, started once."""
    c = chats
    chat = c.running('chat', content='Half of the answer')
    restart_resume.record_quiesced_conversations([chat])
    restart_resume.record_quiesced_conversations([chat])
    c.settle(chat, 'server_shutdown')
    c.next_generation()
    first = restart_resume.resume_conversation(OWNER, 'chat', turn_id=chat['_turnId'],
                                               attempt_id=chat['_attemptId'])
    assert first['outcome'] == 'resumed' and first['turnId'] == chat['_turnId']
    results = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert [item['outcome'] for item in results] == ['not_resumed']
    assert 'already continued' in results[0]['reason']
    assert [conversation for conversation, _ in c.started] == ['chat']
    turn = c.turns.get_turn('chat', chat['_turnId'], user_id=OWNER)
    assert turn['currentAttemptId'] == first['attemptId']


def test_a_conversation_only_the_notice_lists_is_described_by_its_own_latest_work(chats, monkeypatch):
    """coordinator-d4e467e4 was listed only because a zombie job counted as running; its
    latest work had completed long before, and only a report followed. The notice must not
    describe the report ("completed (ingested)") as the interrupted work."""
    from lib import lifecycle_approval as gate
    c = chats
    done = c.running('quiet', content='Finished earlier')
    done.update(status='done', finishReason='stop')
    c.turns.record_task_event(done, {'type': 'done', 'finishReason': 'stop'})
    c.report('quiet', 'A later progress report')
    since = time.time() + 1
    monkeypatch.setattr(gate, 'restart_notices', lambda: [
        {'owner': OWNER, 'acceptedAt': since, 'interrupted': ['quiet']}])
    monkeypatch.setattr(boot_identity, 'BOOT_TS', since + 60)
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert item['outcome'] == 'not_resumed' and item['turnId'] == done['_turnId']
    state = restart_resume.describe_conversation(OWNER, 'quiet', since)
    assert state == 'not interrupted by this restart: its latest work completed before it', state


def test_interrupted_chats_resume_from_their_checkpoint_once(chats):
    """2026-11-08 16:55: two interrupted chats, one of them a swarm master, were never
    resumed; the Router's restart turn had ended and must not be continued."""
    c = chats
    prose = c.running('chat-prose', content='Half of the answer')
    reasoning = c.running('swarm-master', thinking='Waiting for the swarm')
    finished = c.running('finished-before-stop', content='All done')
    stopped = c.running('owner-stopped', content='Stopped by the owner')
    restart_resume.record_quiesced_conversations([prose, reasoning, finished, stopped])
    # The stopping generation settles its quiesced work; one turn had already ended
    # and the owner had stopped another just before.
    c.settle(prose, 'server_shutdown')
    c.settle(reasoning, 'server_shutdown')
    finished.update(status='done', finishReason='stop')
    c.turns.record_task_event(finished, {'type': 'done', 'finishReason': 'stop'})
    c.settle(stopped, None)
    before = {name: _tail(c, name) for name in ('chat-prose', 'swarm-master', 'owner-stopped')}
    assert before['chat-prose']['settlement']['cause'] == 'server_restart'
    assert before['owner-stopped']['settlement']['cause'] == 'user_abort'
    c.next_generation()
    results = {item['conversationId']: item for item in
               restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)}
    assert restart_resume.pass_finished() is True
    assert {name for name, item in results.items() if item['outcome'] == 'resumed'} == {
        'chat-prose', 'swarm-master'}
    assert results['owner-stopped']['outcome'] == 'not_resumed'
    assert 'user_abort' in results['owner-stopped']['reason']
    assert results['finished-before-stop']['outcome'] == 'not_resumed'
    assert sorted(conversation for conversation, _ in c.started) == ['chat-prose', 'swarm-master']
    for name in ('chat-prose', 'swarm-master'):
        interrupted, item = before[name], results[name]
        after = c.turns.get_turn(name, interrupted['turnId'], user_id=OWNER)
        assert after['currentAttemptId'] == item['attemptId'] != interrupted['currentAttemptId']
        offered = [option['operation'] for option in interrupted['settlement']['resumeOptions']
                   if option['operation'] in ('continue', 'checkpoint_resume')]
        assert item['operation'] == offered[0]
        accepted = c.turns.get_attempt(item['attemptId'], user_id=OWNER, include_execution_config=True)
        assert accepted['commandId'] == 'restart-resume:' + interrupted['currentAttemptId']
        assert accepted['operation'] == item['operation']
        assert accepted['config']['model'] == 'chat-model'
        # Its own accepted identity (never a synthetic one) and the Continue
        # receipt a detached swarm needs to re-attach to its parent.
        assert accepted['config'][EXECUTION_IDENTITY_CONFIG_KEY]['principal']['subject_id'] == 'owner'
        assert accepted['config'].get('_resumeSource')
    # What the restart notice reports for each conversation (durable Turn state).
    since = min(item['createdAt'] for item in (before['chat-prose'], before['swarm-master'])) / 1000
    assert restart_resume.describe_conversation(OWNER, 'chat-prose', since).startswith(
        'resumed from its checkpoint (')
    assert restart_resume.describe_conversation(OWNER, 'owner-stopped', since) == 'interrupted (user_abort)'
    # Exactly once: a second pass, then a later generation reading a duplicate record.
    assert restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART) == []
    restart_resume.record_quiesced_conversations([prose])
    c.next_generation()
    again = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert [item['outcome'] for item in again] == ['not_resumed'] and len(c.started) == 2


def test_the_pass_summary_names_every_conversation_with_its_reason(chats, caplog):
    """2026-11-17 22:10:50: the pass logged only `1 of 21 interrupted conversation(s)
    resumed`; the other 20 (handed to project dispatch recovery, superseded by queued
    Router input) could not be told apart from lost work without reading every store.
    One summary line names each resumed conversation and each skipped one with its
    reason, and a pass that resumes nothing still logs it."""
    import logging
    c = chats
    prose = c.running('chat-prose', content='Half of the answer')
    stopped = c.running('owner-stopped', content='Stopped by the owner')
    finished = c.running('finished-before-stop', content='All done')
    restart_resume.record_quiesced_conversations([prose, stopped, finished])
    c.settle(prose, 'server_shutdown')
    c.settle(stopped, None)
    finished.update(status='done', finishReason='stop')
    c.turns.record_task_event(finished, {'type': 'done', 'finishReason': 'stop'})
    c.next_generation()
    with caplog.at_level(logging.INFO, logger='lib.conversations.restart_resume'):
        results = {item['conversationId']: item for item in
                   restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)}
    [summary] = [record.getMessage() for record in caplog.records
                 if '[RestartResume] summary:' in record.getMessage()]
    assert '1 of 3 interrupted conversation(s) resumed' in summary
    assert 'resumed=[chat-prose]' in summary
    for name in ('owner-stopped', 'finished-before-stop'):
        assert f'{name}: {results[name]["reason"]}' in summary, summary
    assert 'user_abort' in results['owner-stopped']['reason']

    # Nothing resumed: the summary still says why each one was left.
    again = c.running('owner-stopped-again', content='Stopped again')
    restart_resume.record_quiesced_conversations([again])
    c.settle(again, None)
    c.next_generation()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger='lib.conversations.restart_resume'):
        [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    [summary] = [record.getMessage() for record in caplog.records
                 if '[RestartResume] summary:' in record.getMessage()]
    assert 'resumed=[]' in summary
    assert f'skipped=[owner-stopped-again: {item["reason"]}]' in summary, summary


def test_the_next_generation_resumes_the_chat_then_wakes_the_requester_with_the_result(
        chats, monkeypatch, tmp_path):
    """The whole restart path on real Turn storage: a Settings restart is accepted
    with a running chat, the stopping generation records and settles it, the next
    generation continues it and then wakes the owner's Global Router with one
    notice Turn naming that resume."""
    from lib import lifecycle_approval as gate
    from lib.server import restart_notice
    c = chats
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, create=False, client=None: {
        'conversationId': 'router', 'availability': 'available', 'revision': 1})
    router = c.running('router', content='Restarting now as you asked.')
    router.update(status='done', finishReason='stop')
    c.turns.record_task_event(router, {'type': 'done', 'finishReason': 'stop'})
    chat = c.running('chat', content='Half of the answer')
    approval = gate.create_request('restart', origin={'conv_id': ''})
    gate.decide(approval['id'], True, provenance={
        'kind': 'human-ui', 'ownerUserId': OWNER, 'subject': 'session-key', 'credential': 'session',
        'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'})
    assert gate.consume_restart(approval['id'], notice={'owner': OWNER, 'interrupted': ['chat'],
                                                        'oldCode': None})[0]
    restart_resume.record_quiesced_conversations([chat])
    c.settle(chat, 'server_shutdown')
    # The old generation, still stopping, reports nothing.
    assert restart_notice.deliver_restart_notices() == []
    c.next_generation()
    monkeypatch.setattr(boot_identity, 'BOOT_TS', boot_identity.BOOT_TS + 3600)
    [resumed] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert resumed['conversationId'] == 'chat' and resumed['outcome'] == 'resumed'
    [delivered] = restart_notice.deliver_restart_notices()
    assert delivered['conversationId'] == 'router' and delivered['state'] == 'delivered'
    turns = c.turns.list_turns('router', user_id=OWNER, lane_id='main')['turns']
    notice_input = next(turn for turn in turns if turn.get('actor') == 'human'
                        and str((turn.get('projection') or {}).get('content', '')).startswith(
                            restart_notice.NOTICE_HEADER))
    text = notice_input['projection']['content']
    assert 'completed' in text and 'chat: resumed from its checkpoint (' in text
    from lib.turn_initiation import INITIATOR_PROACTIVE, resolve_turn_initiator
    assert resolve_turn_initiator(notice_input) == INITIATOR_PROACTIVE
    assert [conversation for conversation, _ in c.started] == ['chat', 'router']
    # Exactly one notice Turn, however often delivery runs.
    assert restart_notice.deliver_restart_notices() == []
    assert len([turn for turn in c.turns.list_turns('router', user_id=OWNER, lane_id='main')['turns']
                if str((turn.get('projection') or {}).get('content', '')).startswith(
                    restart_notice.NOTICE_HEADER)]) == 1


def _failed_notice(chats, conversation, *, initiator='peer', steers=None,
                   tool_rounds=None, thinking='reviewed privately'):
    """Settle a Global Router notice Turn the way bounded terminal recovery leaves it.

    The real terminal writer owns status/settlement; this fixture only supplies
    the task facts of an exhausted empty_stop on a nonhuman notice.
    """
    from lib.error_envelope import make_envelope
    c = chats
    rounds = list(tool_rounds or [])
    task = c.running(conversation, thinking=thinking,
                     inputProjection={'content': '[Project report relay: nonhuman notice]',
                                      '_initiator': initiator,
                                      'origin': {'initiator': initiator,
                                                 'sourceConversationId': 'project-router'}},
                     toolRounds=rounds, steers=steers)
    task['error'] = make_envelope(
        'abnormal_stop', message='Model repeatedly returned no usable terminal response '
        '(empty_stop); bounded recovery exhausted.',
        detail='Completed tool operations and their results were preserved.',
        model='chat-model', context='round-2', source='llm-stream',
        raw='bucket=empty_stop attempts=2/2 trace_id=t-1')
    task.update(status='error', finishReason='abnormal_stop')
    # Project the already-executed read through the ordinary event fold, so the
    # durable Turn's tool rounds and checkpoint scan see the real shape.
    if rounds:
        for round_num, row in enumerate(rounds, 1):
            c.turns.record_task_event(task, {
                'type': 'tool_complete', 'toolName': row['toolName'],
                'toolCallId': row['toolCallId'], 'toolContent': row.get('toolContent') or '',
                'results': row.get('results') or [], 'roundNum': row.get('roundNum') or round_num,
                'toolArguments': row.get('toolArguments') or {}})
    c.turns.record_task_event(task, {'type': 'done', 'finishReason': 'abnormal_stop',
                                     'error': task['error']})
    return task


@requires_dx
def test_the_terminal_observer_recovers_a_failed_notice_through_real_turn_storage(chats):
    """The owner-facing path end to end on real storage: the terminal observer
    verifies the failed nonhuman notice, issues the ordinary resume command,
    and its command-chat warning names the continuation — not a manual resend.
    Nothing is marked reviewed; the failed Turn keeps its error and evidence.
    """
    from lib.channels import autoreply
    from tofu_yourprovider.dx import supervisor
    c = chats
    owner = OWNER
    read = {'toolName': 'get_conversation', 'status': 'done', 'toolCallId': 'call-read',
            'toolContent': 'Project report: nothing new.',
            'results': [{'toolName': 'get_conversation', 'snippet': 'nothing new'}],
            'roundNum': 1, 'toolArguments': {}}
    _failed_notice(c, 'router', tool_rounds=[read])
    tail = _tail(c, 'router')
    # The Global Router binding and the owner's enabled 大象 command chat.
    import lib.channels.delivery as delivery
    from lib.channels.store import ChannelStore
    from lib.channels import service
    store = ChannelStore(owner)
    from unittest.mock import patch
    with patch.object(delivery, 'global_router_binding', lambda *a, **k: {
            'conversationId': 'router', 'projectPath': '/router', 'availability': 'available'}), \
         patch.object(autoreply, '_command_chat_enabled', lambda store: True), \
         patch.object(supervisor, 'notify_account_changed', lambda *a, **k: None):
        service.update_account(owner, {'enabled': True}, store=store)
        store.update_account('dx', lambda value: {
            **value, 'commandChat': 'dx:gid:900', 'enabled': True,
            'identity': {'uid': '1001', 'mis': 'owner', 'name': 'Owner'}})
        task = {'id': 'router-task', '_userId': owner, 'convId': 'router',
                '_turnId': tail['turnId'], '_attemptId': tail['currentAttemptId'],
                'status': 'error', 'finishReason': 'abnormal_stop',
                'content': '', 'error': tail['settlement']['error']}
        autoreply.on_task_terminal(task)
        autoreply.flush_notice_recovery_for_test()
    after = c.turns.get_turn('router', tail['turnId'], user_id=owner)
    assert after['currentAttemptId'] != tail['currentAttemptId']
    assert [conversation for conversation, _ in c.started] == ['router']
    items = store.active_outbox()
    assert [item.text for item in items] == [
        '总负责人未能完成一条通知的审阅：原因：' + tail['settlement']['error']['message']
        + ' 已自动从断点继续处理这条通知，保留已完成的工具结果，无需重新发送。']
    # Replaying the terminal observer neither resumes twice nor duplicates it.
    autoreply.on_task_terminal(task)
    assert len(c.started) == 1 and len(store.active_outbox()) == 1


def test_a_turn_with_a_call_cut_off_mid_effect_waits_for_its_owner(chats):
    """Durable worker recovery names the calls a dead process cut off mid-effect
    (``error.unknownEffects``). The restart resume pass leaves that Turn for its
    owner instead of continuing it; every other restart interruption resumes itself."""
    from lib.error_envelope import make_envelope
    c = chats
    task = c.running('chat', content='Writing the report')
    restart_resume.record_quiesced_conversations([task])
    effects = [{'toolName': 'run_command', 'toolCallId': 'call-write', 'operationId': 'op-1'}]
    error = make_envelope('internal', message='The restart cut off calls whose side effect is unknown',
                          extensions={'code': 'worker_effect_outcome_unknown', 'unknownEffects': effects,
                                      'retryPolicy': {'mode': 'manual', 'automatic': False}})
    task.update(status='aborted', aborted=True, _abort_reason='restart_recovery', error=error)
    c.turns.record_task_event(task, {'type': 'aborted', 'error': error})
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert item['outcome'] == 'not_resumed' and c.started == []
    assert 'run_command call-write with an unknown side effect' in item['reason']


def test_a_failed_notice_review_is_recoverable_in_place_and_resumes_once(chats):
    """2026-11-09: exhausted terminal recovery left nonhuman notices as unread
    failed cards whose only next action was the owner's resend. The failed Turn
    keeps its evidence and completed tool results and resumes in place through
    the existing resume command, once per failed Attempt; nothing is acked as
    reviewed, and no synthetic human reply is created."""
    c = chats
    read = {'toolName': 'get_conversation', 'status': 'done', 'toolCallId': 'call-read',
            'toolContent': 'Project report: nothing new.',
            'results': [{'toolName': 'get_conversation', 'snippet': 'nothing new'}],
            'roundNum': 1, 'toolArguments': {}}
    _failed_notice(c, 'router', tool_rounds=[read])
    tail = _tail(c, 'router')
    assert tail['status'] == 'failed'
    assert tail['settlement']['error']['kind'] == 'abnormal_stop'
    from lib.turn_initiation import INITIATOR_PEER, resolve_turn_initiator
    source = next(turn for turn in c.turns.list_turns('router', user_id=OWNER)['turns']
                  if turn.get('actor') == 'human')
    assert resolve_turn_initiator(source) == INITIATOR_PEER
    # No silent review note and no deliverable answer: the notice is unreviewed.
    assert not tail['projection'].get('content')
    segments = tail['projection'].get('segments') or []
    assert all(segment.get('noteKind') != 'deliberate-silence' for segment in segments)

    item = restart_resume.resume_failed_notice_turn(OWNER, 'router')
    assert item['outcome'] == 'resumed' and item['operation'] in restart_resume.RESUME_OPERATIONS
    accepted = c.turns.get_attempt(item['attemptId'], user_id=OWNER, include_execution_config=True)
    assert accepted['operation'] == item['operation']
    assert accepted['commandId'] == 'notice-review-resume:' + tail['currentAttemptId']
    assert accepted['config']['_resumeSource']
    # The completed read's result survives in the resume checkpoint; the
    # ordinary command owner, not this gate, replays it without re-executing.
    # The accepted attempt carries the exact settlement anchor; the executor's
    # checkpoint replay is derived from the settled projection by the ordinary
    # command owner, so the completed read is never re-executed.
    assert accepted['resumeAnchor'] == next(
        option['anchor'] for option in tail['settlement']['resumeOptions']
        if option['operation'] == item['operation'])
    from lib.conversation_sync.command_service import _resume_executor_config
    checkpoint = _resume_executor_config(item['operation'], tail['projection'])
    replayed = checkpoint.get('checkpointToolRounds') or []
    assert [row['toolCallId'] for row in replayed] == ['call-read']
    assert replayed[0]['toolContent'] == 'Project report: nothing new.'
    after = c.turns.get_turn('router', tail['turnId'], user_id=OWNER)
    assert after['currentAttemptId'] == item['attemptId'] != tail['currentAttemptId']
    assert [conversation for conversation, _ in c.started] == ['router']
    # A replay of the same pass (restart, lost ACK) does not resume it twice.
    again = restart_resume.resume_failed_notice_turn(OWNER, 'router')
    assert again['outcome'] == 'not_resumed' and len(c.started) == 1


def test_a_failed_notice_review_a_later_record_follows_resumes_in_place_and_keeps_it(chats):
    """A resume preserves every later Turn: a failed review with an appended record
    after it resumes in place, and the later record stays where it is."""
    c = chats
    read = {'toolName': 'get_conversation', 'status': 'done', 'toolCallId': 'call-read',
            'toolContent': 'Project report: nothing new.',
            'results': [{'toolName': 'get_conversation', 'snippet': 'nothing new'}],
            'roundNum': 1, 'toolArguments': {}}
    _failed_notice(c, 'router', tool_rounds=[read])
    failed = _tail(c, 'router')
    c.report('router', 'A later relay')
    lane = _lane(c, 'router')
    item = restart_resume.resume_failed_notice_turn(OWNER, 'router', failed['turnId'])
    assert item['outcome'] == 'resumed' and [conversation for conversation, _ in c.started] == ['router']
    after = _lane(c, 'router')
    assert [turn_id for turn_id, _ in after] == [turn_id for turn_id, _ in lane]
    assert dict(after)[failed['turnId']] == item['attemptId'] != failed['currentAttemptId']
    assert after[-1] == lane[-1]


def test_a_failed_notice_without_a_resume_option_is_left_with_its_evidence(chats):
    """Reasoning-only or provider errors that offer no checkpoint resume are
    never acked: they keep their failure and the existing owner next action."""
    c = chats
    _failed_notice(c, 'router', tool_rounds=[], thinking='')
    item = restart_resume.resume_failed_notice_turn(OWNER, 'router')
    assert item['outcome'] == 'not_resumed' and 'resume' in item['reason']
    assert c.started == []
    assert _tail(c, 'router')['status'] == 'failed'


def test_owner_authored_turns_are_never_resumed_by_the_notice_recovery(chats):
    """Silent completion and this recovery apply only to nonhuman notices. A
    failed owner command keeps its ordinary failure and manual Continue."""
    c = chats
    _failed_notice(c, 'router', initiator='human', tool_rounds=[{
        'toolName': 'get_conversation', 'status': 'done', 'toolCallId': 'call-read',
        'toolContent': 'answer', 'results': [], 'roundNum': 1, 'toolArguments': {}}])
    item = restart_resume.resume_failed_notice_turn(OWNER, 'router')
    assert item['outcome'] == 'not_resumed' and 'nonhuman' in item['reason']
    assert c.started == []


def test_a_notice_with_a_current_owner_steer_is_not_resumed_automatically(chats):
    c = chats
    from lib.error_envelope import make_envelope
    from lib.turn_initiation import steer_author
    task = c.running('router', thinking='reviewed privately',
                     inputProjection={'content': '[Project report relay: nonhuman notice]',
                                      '_initiator': 'peer',
                                      'origin': {'initiator': 'peer',
                                                 'sourceConversationId': 'project-router'}},
                     toolRounds=[{'toolName': 'get_conversation', 'status': 'done',
                                  'toolCallId': 'call-read', 'toolContent': 'answer',
                                  'results': [], 'roundNum': 1, 'toolArguments': {}}])
    # A genuine current-Turn owner steer is committed while the window is open.
    c.turns.commit_user_steer(
        'router', task['_attemptId'], command_id='channel-owner-1',
        text='Answer me first', user_id=OWNER,
        author=steer_author({'content': 'Answer me first'}, actor='human'))
    task['error'] = make_envelope(
        'abnormal_stop', message='Model repeatedly returned no usable terminal response '
        '(empty_stop); bounded recovery exhausted.',
        detail='Completed tool operations and their results were preserved.',
        model='chat-model', context='round-2', source='llm-stream',
        raw='bucket=empty_stop attempts=2/2 trace_id=t-1')
    task.update(status='error', finishReason='abnormal_stop')
    c.turns.record_task_event(task, {'type': 'done', 'finishReason': 'abnormal_stop',
                                     'error': task['error']})
    assert _tail(c, 'router')['projection'].get('_userSteerInjects')
    item = restart_resume.resume_failed_notice_turn(OWNER, 'router')
    assert item['outcome'] == 'not_resumed' and 'steer' in item['reason']
    assert c.started == []


def test_a_conversation_the_owner_moved_on_from_is_not_resumed(chats):
    c = chats
    task = c.running('moved-on', content='Interrupted work')
    restart_resume.record_quiesced_conversations([task])
    c.settle(task, 'server_shutdown')
    # Before the pass ran, the owner sent a new message: the lane advanced.
    newer = c.turns.create_turn_pair('moved-on', command_id='moved-on:second-input',
        input_projection={'content': 'Forget that; do this instead'}, config={'model': 'chat-model'},
        user_id=OWNER)
    lane = _lane(c, 'moved-on')
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART)
    assert item['outcome'] == 'superseded' and c.started == [] and _lane(c, 'moved-on') == lane
    assert item['turnId'] == task['_turnId']
    assert item['supersededBy'] == newer['submittedTurn']['turnId']
    assert item['reason'] == 'superseded by newer input ' + newer['submittedTurn']['turnId']


@pytest.mark.parametrize('previous, reason', [
    ({'verdict': 'clean', 'reason': 'manual', 'restart_storm': False}, 'manual shutdown'),
    ({'verdict': 'unclean', 'restart_storm': True}, 'restart storm'),
])
def test_a_manual_shutdown_or_restart_storm_resumes_nothing(chats, previous, reason):
    c = chats
    task = c.running('after-shutdown', content='Interrupted work')
    restart_resume.record_quiesced_conversations([task])
    c.settle(task, 'server_shutdown')
    c.next_generation()
    [item] = restart_resume.resume_restart_paused_turns(previous_shutdown=previous)
    assert item['outcome'] == 'not_resumed' and reason in item['reason']
    assert c.started == [] and restart_resume.pass_finished() is True
    # The records were consumed: a later restart does not resurrect this stand-down.
    c.next_generation()
    assert restart_resume.resume_restart_paused_turns(previous_shutdown=RESTART) == []


def test_boot_settled_orphans_of_a_killed_server_resume_too(chats, monkeypatch):
    """A killed server quiesced nothing; boot recovery settles its orphans."""
    c = chats
    task = c.running('killed', content='Interrupted work')
    c.turns.recover_running_attempts()
    c.next_generation()
    monkeypatch.setattr(restart_resume, '_task_owner', lambda task_id: OWNER if task_id == task['id'] else None)
    [item] = restart_resume.resume_restart_paused_turns(
        previous_shutdown={'verdict': 'unclean', 'restart_storm': False},
        boot_recovered=[{'taskId': task['id'], 'conversationId': 'killed'}])
    assert item['outcome'] == 'resumed' and [conv for conv, _ in c.started] == ['killed']


@pytest.mark.parametrize('config, settlement, reason', [
    ({'_projectDispatchAdmission': {'assignmentId': 'a-1'}},
     {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]}, 'project dispatch'),
    ({'autopilot': True}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]}, 'Goal'),
    ({'activeFlow': 'review-flow'}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]},
     'Flow'),
    ({'flowBuiltin': 'deep-research'}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'continue'}]},
     'Flow'),
    ({}, {'cause': 'server_restart', 'resumeOptions': [
        {'operation': 'continue'}, {'operation': 'checkpoint_resume', 'anchor': {}},
        {'operation': 'answer_guidance', 'anchor': {'guidanceId': 'g-1'}}]}, 'answer'),
    ({}, {'cause': 'server_restart', 'resumeOptions': [{'operation': 'regenerate'}]}, 'regenerate'),
])
def test_work_other_owners_resume_or_the_owner_must_answer_is_left_alone(monkeypatch, config, settlement,
                                                                         reason):
    turn = {'turnId': 't-1', 'conversationId': 'c-1', 'laneId': 'main', 'actor': 'assistant',
            'status': 'interrupted', 'currentAttemptId': 'a-1', 'projectionRevision': 3,
            'settlement': settlement}
    monkeypatch.setattr(restart_resume, '_interrupted_turn',
                        lambda owner, conversation, turn_id: (dict(turn), []))
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', lambda attempt, **kwargs: {
        'attemptId': 'a-1', 'conversationId': 'c-1', 'turnId': 't-1', 'status': 'interrupted',
        'config': {'model': 'm', '_turnOwnerUserId': OWNER, **config}})
    monkeypatch.setattr('lib.conversation_sync.runtime.conversation_turn_commands', SimpleNamespace(
        create_attempt=lambda *args, **kwargs: pytest.fail('nothing may resume ' + reason)))
    item = restart_resume.resume_conversation(OWNER, 'c-1')
    assert item['outcome'] == 'not_resumed' and reason in item['reason']
