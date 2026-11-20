"""A Coordinator continuation is anchored on its task board and on the Worker states that need it.

Spec for docs/PROJECT_COORDINATION.md "Continuation context is anchored on the task
board": the wake's input carries the open board tasks, the owner's unanswered
questions and only the actionable Worker states; the owner's request is linked,
and quoted only while the board has no open task; settled, cancelled and
delivered assignments are omitted; a normally ended wake with nothing new and
nothing to act on is not repeated, while a failed one still retries.
"""
import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, activate, controls, delegate, end)
from tests.project_mod.test_coordinator_result_wake import board as board
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.project_dispatch.delegation import WORKER_INPUT_SOURCE_HEAD

pytestmark = pytest.mark.unit

OWNER_WORDS = 'Please consult'


@pytest.fixture
def wake_inputs(board, monkeypatch):
    r = board
    r.inputs = {}
    query = r.c.client.query

    def list_records(operation, params):
        if operation != 'record.list':
            return query(operation, params)
        from copy import deepcopy
        return [dict(deepcopy(row), key=key) for (namespace, key), row in sorted(r.c.client.rows.items())
                if namespace == params['namespace'] and key.startswith(params['prefix'])
                and key > params.get('after_key', '')][:params['limit']]

    monkeypatch.setattr(r.c.client, 'query', list_records)
    create_turn = r.service.commands.create_turn

    def capture(conversation, owner, request, **kwargs):
        r.inputs.setdefault(conversation, []).append(request['inputTurn']['content'])
        return create_turn(conversation, owner, request, **kwargs)

    monkeypatch.setattr(r.service.commands, 'create_turn', capture)
    r.questions = []
    monkeypatch.setattr('lib.router.proactive.questions.open_questions',
                        lambda owner, coordinator_id=None, client=None: list(r.questions))
    return r


def _settle_request_turn(r):
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.attempts['coordinator-attempt']['settledAt'] = int(r.c.now[0] * 1000)


def test_board_questions_and_only_actionable_workers_anchor_the_wake(wake_inputs):
    r = wake_inputs
    accepted, waiting, revoked = delegate(r, 'accepted'), delegate(r, 'waiting'), delegate(r, 'revoked')
    end(r, accepted); end(r, waiting)
    assert r.call('review_project_work', assignment_id=accepted['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    assert r.call('cancel_project_work', assignment_id=revoked['id'])['revoked']
    r.publish([{'title': 'Ship the leaner continuation', 'status': 'in_progress',
                'progress': 'review pending'},
               {'title': 'Verify the snapshot', 'status': 'pending',
                'blocker': {'kind': 'needs_external_resource', 'resource': 'the signed export',
                            'check': 'it verifies against the published digest',
                            'ownerAction': 'send the signed export from the owner mailbox'},
                'reason': 'The signed export is not available yet.'},
               {'title': 'Already done', 'status': 'completed'}])
    r.questions = [{'question': 'Restart now?', 'default': 'wait for the host move', 'turnId': 'ask-1'}]
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    brief = controls(r)[-1]['request']['brief']
    assert content.startswith(brief) and WORKER_INPUT_SOURCE_HEAD + 'coordinator; Turn human-turn' in content
    # The owner's request is linked, not repeated, while the board says what is open.
    assert OWNER_WORDS not in content and 'Original human request' not in content
    assert '[in_progress] Ship the leaner continuation' in content
    assert ('[pending] Verify the snapshot: Waiting for: the signed export (proved by: it verifies against the '
            'published digest); you: send the signed export from the owner mailbox') in content
    # Free-text reason and progress are not evidence; only the typed blocker is projected.
    assert 'The signed export is not available.' not in content
    assert 'review pending' not in content
    assert 'Already done' not in content
    assert 'Restart now? (your default meanwhile: wait for the host move) [Turn ask-1]' in content
    assert waiting['id'] + ': result to review' in content
    # Settled (reviewed and delivered) and cancelled assignments are not listed.
    assert accepted['id'] not in content and revoked['id'] not in content
    assert 'Current results' not in brief and accepted['id'] not in brief
    assert 'Open board outcomes with unknown ownership' in content
    assert '- [ownership_unknown] Ship the leaner continuation' in content


def test_dropped_open_outcome_is_visible_until_owned_or_cancelled(wake_inputs, monkeypatch):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    dropped = {'title': 'Paper B: the missing gate verdict', 'status': 'in_progress', 'key': 'paper-b'}
    monkeypatch.setattr('lib.conversations.coordinator_task_history.CoordinatorTaskHistory.read',
                        lambda self, **kwargs: {'tasks': [dropped], 'nextCursor': None})
    r.publish([{'title': 'Main paper: finish the table', 'status': 'in_progress'}])
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert 'Open outcomes dropped from the board' in content
    assert '- [dropped] Paper B: the missing gate verdict' in content
    assert '- [unowned] Paper B: the missing gate verdict' not in content


def test_a_live_legacy_worker_with_an_identical_title_leaves_ownership_unknown(wake_inputs):
    r = wake_inputs
    title = 'Paper B: the missing gate verdict'
    r.publish([{'title': title, 'status': 'in_progress'}])
    legacy = r.call('delegate_project_work', **{**r.args, 'title': title})
    assert legacy['dispatch'] == 'accepted'
    end(r, delegate(r, 'wake-trigger'))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert f'- [ownership_unknown] {title}' in content
    assert '[active_owner]' not in content and '[unowned]' not in content


def test_request_turn_history_supplies_a_dropped_outcome_the_head_never_recorded(wake_inputs):
    """A drop is visible even when durable task history does not know the row."""
    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    turn = next(row['execution']['turnId'] for row in r.c.repo.list()
                if row['request']['profile']['mode'] == 'coordinator')
    r.turns[turn]['toolRounds'] = [
        {'toolName': 'update_task_progress', 'tEnd': 1001,
         'toolArgs': {'summary': 'Both papers', 'tasks': [
             {'title': 'Paper B: the missing gate verdict', 'status': 'in_progress'},
             {'title': 'Main paper: the table', 'status': 'in_progress'}]},
         'toolContent': {'changed': True, 'revision': 1}},
        {'toolName': 'update_task_progress', 'tEnd': 1002,
         'toolArgs': {'summary': 'Only the main paper', 'tasks': [
             {'title': 'Main paper: the table', 'status': 'in_progress'}]},
         'toolContent': {'changed': True, 'revision': 2}},
    ]
    from lib.conversations.coordinator_task_history import title_key
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr('lib.conversations.coordinator_task_history.CoordinatorTaskHistory.read',
                        lambda self, **kwargs: {'tasks': [], 'nextCursor': None, 'recorded': True})
    r.publish([{'title': 'Main paper: the table', 'status': 'in_progress'}])
    from lib.conversations.coordinator_continuation import continuation_context
    content = continuation_context(r.c.repo, controls(r)[-1])
    assert '- [dropped] Paper B: the missing gate verdict' in content
    assert '- [dropped] Please consult' not in content
    monkeypatch.undo()


def test_an_owner_request_without_a_board_outcome_is_stated_not_lost(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    r.publish([{'title': 'Paper B: the missing gate verdict', 'status': 'in_progress'}])
    from lib.conversations.coordinator_continuation import continuation_context
    content = continuation_context(r.c.repo, controls(r)[-1])
    assert '- [dropped] Please consult' in content
    assert 'ownership unknown' in content and 'assign an owner now' not in content


def test_a_finished_request_is_not_reopened_as_a_stated_outcome(wake_inputs):
    from tests.project_mod.test_coordinator_continuation import report
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    activate(r)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    assert r.call('finish_project_request', **report(r))['reason'] == 'complete'
    end(r, controls(r)[-1], delivered=False)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    # The origin's finish receipt is durable: no request text is resurrected.
    from lib.conversations.coordinator_continuation import _stated_outcomes
    requests = [row for row in r.c.repo.list() if row['request']['profile']['mode'] == 'coordinator']
    assert _stated_outcomes(r.c.repo, requests, set()) == []


def _delegate_link(r, key, *, idempotency_key='linked'):
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': idempotency_key,
                                               'board_task_keys': [key]})
    assert result['dispatch'] == 'accepted', result
    row = r.c.repo.read(result['assignment_id'])
    assert row['request']['boardTaskKeys'] == [key]
    return row


def test_exact_link_survives_rename_and_does_not_claim_duplicate_or_finish_board(wake_inputs):
    from lib.conversations.coordinator_continuation import continuation_context
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress

    r = wake_inputs
    r.publish([{'title': 'Original outcome', 'status': 'in_progress'}])
    progress = CoordinatorTaskProgress(r.c.repo.owner, r.c.repo.project, client=r.c.repo.client,
                                       bindings=r.c.repo.binding_repository)
    key, = progress.read()['taskKeys']
    linked = _delegate_link(r, key)
    r.publish([{'title': 'Renamed outcome', 'status': 'in_progress'}])
    r.publish([{'title': 'Renamed outcome', 'status': 'in_progress'},
               {'title': 'Renamed outcome', 'status': 'pending',
                'blocker': {'kind': 'next_action', 'action': 'Compare the two results'}}])
    kept, duplicate = progress.read()['taskKeys']
    assert kept == key and duplicate != key
    end(r, delegate(r, 'wake-trigger'))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert f'- [active_owner] {key}: {linked["id"]}' in content
    assert f'- [ownership_unknown] Renamed outcome (task key {duplicate})' in content
    assert f'- [ownership_unknown] Renamed outcome (task key {key})' not in content
    assert '[unowned]' not in content and 'assign an owner now' not in content

    row = linked
    end(r, row, delivered=True)
    from lib.project_dispatch.continuation import worker_summaries
    summary, = worker_summaries(r.c.repo, [row])
    assert summary['status'] == 'completed' and summary['delivery']['status'] == 'delivered'
    content = continuation_context(r.c.repo, controls(r)[-1])
    assert '[active_owner]' not in content
    assert [task['status'] for task in progress.read()['board']['tasks']] == ['in_progress', 'pending']


def test_returning_title_cannot_inherit_the_worker_of_a_renamed_task(wake_inputs):
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress

    r = wake_inputs
    repo = r.c.repo
    progress = CoordinatorTaskProgress(repo.owner, repo.project, client=repo.client,
                                       bindings=repo.binding_repository)
    a = {'title': 'Original outcome', 'status': 'in_progress'}
    b = {'title': 'Renamed outcome', 'status': 'in_progress'}
    r.publish([a])
    original, = progress.read()['taskKeys']
    worker = _delegate_link(r, original)
    r.publish([b])
    r.publish([b, a])
    kept, distinct = progress.read()['taskKeys']
    assert kept == original and distinct != original
    r.publish([a])
    r.publish([])
    r.publish([a])
    assert progress.read()['taskKeys'] == [distinct]
    end(r, delegate(r, 'wake-trigger'))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert f'- [ownership_unknown] {a["title"]} (task key {distinct})' in content
    assert f'- [active_owner] {original}: {worker["id"]}' in content  # Still linked to dropped B.
    assert f'- [active_owner] {distinct}:' not in content


def test_supported_delegation_link_survives_rename_and_restoration(wake_inputs):
    r = wake_inputs
    key, = r.publish([{'title': 'Original', 'status': 'in_progress'}])['taskKeys']
    worker = r.call('delegate_project_work', **{**r.args, 'board_task_keys': [key]})
    assert worker.get('dispatch') == 'accepted', worker
    row = r.c.repo.read(worker['assignment_id'])
    assert row['request']['boardTaskKeys'] == [key]
    r.publish([{'title': 'Renamed', 'status': 'in_progress'}])
    r.publish([])
    assert r.publish([{'title': 'Renamed', 'status': 'in_progress'}])['taskKeys'] == [key]
    end(r, delegate(r, 'wake-trigger'))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert f'- [active_owner] {key}: {row["id"]}' in content
    assert '[ownership_unknown] Renamed' not in content and '[dropped] Renamed' not in content


def test_renamed_and_restored_task_keeps_its_committed_active_worker(wake_inputs):
    r = wake_inputs
    r.publish([{'title': 'Original', 'status': 'in_progress'}])
    key, = r.publish([{'title': 'Renamed', 'status': 'in_progress'}])['taskKeys']
    row = _delegate_link(r, key)
    r.publish([])
    assert r.publish([{'title': 'Renamed', 'status': 'in_progress'}])['taskKeys'] == [key]
    end(r, delegate(r, 'wake-trigger'))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert f'- [active_owner] {key}: {row["id"]}' in content
    assert '[ownership_unknown] Renamed' not in content and '[dropped] Renamed' not in content


@pytest.mark.parametrize('foreign', ['owner', 'tenant', 'binding', 'epochless', 'project', 'coordinator'])
def test_active_link_never_crosses_its_assignment_scope(wake_inputs, monkeypatch, foreign):
    from lib.conversations.coordinator_continuation import continuation_context
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.project_dispatch import history

    r = wake_inputs
    repo = r.c.repo
    r.publish([{'title': 'Scoped outcome', 'status': 'in_progress'}])
    key, = CoordinatorTaskProgress(repo.owner, repo.project, client=repo.client,
                                   bindings=repo.binding_repository).read()['taskKeys']
    row = _delegate_link(r, key)
    r.attempts[row['execution']['attemptId']]['status'] = 'pending'
    end(r, delegate(r, 'wake-trigger'))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    assert f'- [active_owner] {key}: {row["id"]}' in r.inputs['coordinator'][-1]
    page = history.page

    def foreign_page(*args, **kwargs):
        result_page = page(*args, **kwargs)
        for item in result_page['rows']:
            if item['id'] != row['id']:
                continue
            if foreign == 'epochless':
                item['grant'].pop('bindingRevision')
            elif foreign == 'project':
                item['request']['projectPath'] = '/another-project'
            elif foreign == 'coordinator':
                item['request']['coordinatorId'] = 'another-coordinator'
            else:
                field, value = {'owner': ('owner', 999), 'tenant': ('tenant', 'other-tenant'),
                                'binding': ('bindingRevision', 0)}[foreign]
                item['grant'][field] = value
        return result_page

    monkeypatch.setattr(history, 'page', foreign_page)
    content = continuation_context(repo, controls(r)[-1])
    assert f'- [ownership_unknown] Scoped outcome (task key {key})' in content
    assert '[active_owner]' not in content


def test_an_empty_board_quotes_the_owner_request(wake_inputs):
    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert 'Your board has no open task' in content and OWNER_WORDS in content


def test_a_worker_input_carries_its_brief_and_a_source_link_only(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    content = r.inputs[worker['request']['recipientId']][-1]
    assert content.startswith(worker['request']['brief'] + WORKER_INPUT_SOURCE_HEAD)
    assert OWNER_WORDS not in content and 'get_conversation' in content
    assert 'not a new human authorization' in content


def test_a_failure_already_judged_is_not_listed_again(wake_inputs):
    r = wake_inputs
    end(r, delegate(r, 'old'), 'failed')
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    failed = controls(r)[-1]
    assert 'ended' in r.inputs['coordinator'][-1]
    activate(r)
    newer = delegate(r, 'new')
    r.c.now[0] += 10
    end(r, failed, delivered=False)
    end(r, newer)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert newer['id'] + ': result to review' in content and ': ended' not in content


def test_nothing_actionable_after_a_normal_turn_wakes_no_repeat(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    # The turn ended normally with the same evidence and nothing to act on.
    assert r.wake() == [] and not r.scheduled and not r.reports
    assert len(controls(r)) == 1


def test_a_failed_turn_with_nothing_actionable_still_retries(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], 'failed', delivered=False)
    assert r.wake() == [] and r.scheduled, 'a failed Coordinator turn backs off and retries'


def test_temporal_overlap_alone_is_not_routed_provenance():
    from lib.conversations.coordinator_continuation import _routed_keys

    source = ('router', 'human-turn', None, None)
    outcomes = [{'key': 'independent-board-task', 'firstSeenAt': 2000},
                {'key': 'exact-request', 'sourceIdentity': source}]
    assert _routed_keys({source}, outcomes) == {'exact-request'}


def test_forwarding_one_steer_does_not_cover_another_steer_in_same_reply(wake_inputs, monkeypatch):
    from copy import deepcopy
    from lib.conversations import coordinator_continuation as continuation
    from lib.turn_initiation import STEER_BLOCK_PREFIX

    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    wake = deepcopy(controls(r)[-1])
    source = wake['request']['authorizationSource']
    source.update(turnId='router-reply', steerId='steer-B')
    r.turns['router-reply'] = {'conversationId': 'coordinator', 'turnId': 'router-reply', 'actor': 'assistant',
        'kind': 'reply', 'createdAt': 1000, 'updatedAt': 3000, 'status': 'completed',
        'projection': {'_userSteerInjects': [
            {'blockId': STEER_BLOCK_PREFIX + steer, 'commandId': steer, 'author': {'actor': 'human', 'text': text}}
            for steer, text in [('steer-A', 'Forwarded A'), ('steer-B', 'Independent B')]]}}
    forwarded = {'conversationId': 'coordinator', 'turnId': 'router-reply', 'steerId': 'steer-A'}
    r.turns['lead-input'] = {'projection': {'origin': {'forwardedFrom': forwarded}}}
    monkeypatch.setattr('lib.router.home.is_router_home', lambda *_: True)
    monkeypatch.setattr(continuation, '_lead_live_sources', lambda *_: iter([
        ('lead', {'conversationId': 'lead', 'turnId': 'lead-input'})]))
    monkeypatch.setattr(continuation, '_binding_rows', lambda *_: [wake])
    assert '- [dropped] Independent B' in continuation.continuation_context(r.c.repo, wake)
    del forwarded['steerId']
    assert '- [dropped] Independent B' in continuation.continuation_context(r.c.repo, wake)
    forwarded['steerId'] = 'steer-B'
    assert '- [dropped] Independent B' not in continuation.continuation_context(r.c.repo, wake)


def test_independent_board_task_stays_unknown_during_a_live_forward(wake_inputs, monkeypatch):
    """A real forward is evidence about its source Turn, not nearby board edits."""
    from copy import deepcopy
    from lib.conversations import coordinator_continuation
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    title = 'Tofu: connect several devices'
    r.publish([{'title': title, 'status': 'in_progress'}])
    head = CoordinatorTaskProgress(r.c.repo.owner, r.c.repo.project, client=r.c.repo.client,
                                   clock=r.c.repo.clock)._head()['value']
    r.turns['human-turn'].update(kind='input', createdAt=1000, ordinal=0, laneId='main')
    reply = r.turns['coordinator-turn']
    reply.update(createdAt=1001, updatedAt=3000, status='completed', ordinal=1, parentTurnId='human-turn')
    r.turns['lead-input'] = {
        'turnId': 'lead-input', 'conversationId': 'coordinator-tofu', 'actor': 'human', 'kind': 'input',
        'projection': {'content': OWNER_WORDS, 'origin': {
            'initiator': 'human', 'forwardedFrom': {'conversationId': 'coordinator', 'turnId': 'human-turn'}}}}
    record = {'key': head['taskKeys'][0], 'title': title, 'status': 'in_progress', 'firstSeenAt': 2000}
    monkeypatch.setattr('lib.conversations.coordinator_task_history.CoordinatorTaskHistory.read',
                        lambda self, group=None, **kwargs: {
                            'tasks': [record] if group == 'in_progress' else [], 'nextCursor': None,
                            'recorded': True})
    monkeypatch.setattr('lib.router.home.is_router_home', lambda owner, project: True)
    monkeypatch.setattr('lib.turn_lifecycle.list_turns', lambda conversation, **kwargs: {
        'turns': [deepcopy(reply)] if kwargs['after_ordinal'] == 0 else []})
    live = []
    monkeypatch.setattr(coordinator_continuation, '_lead_live_sources', lambda owner, tenant, client: iter(live),
                        raising=False)
    content = coordinator_continuation.continuation_context(r.c.repo, controls(r)[-1])
    assert '- [ownership_unknown] ' + title in content, 'there is no live scoped route yet'
    live.append(('coordinator-tofu', {'conversationId': 'coordinator-tofu', 'turnId': 'lead-input'}))
    content = coordinator_continuation.continuation_context(r.c.repo, controls(r)[-1])
    assert '- [ownership_unknown] ' + title in content
    assert '- [dropped] ' + OWNER_WORDS not in content  # The exact forwarded request is covered.


@pytest.mark.parametrize('race', ['advance', 'reject'])
def test_committed_history_and_visible_continuation_agree_after_head_race(wake_inputs, monkeypatch, race):
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.coordinator_task_history import CoordinatorTaskHistory
    from lib.storage.errors import StorageError

    r = wake_inputs
    repo = r.c.repo
    progress = CoordinatorTaskProgress(repo.owner, repo.project, client=repo.client,
                                       bindings=repo.binding_repository)
    anchor = {'title': 'Anchor', 'status': 'pending',
              'blocker': {'kind': 'blocked_by_dependency', 'assignmentId': 'assignment-anchor', 'revision': 1}}
    r.publish([anchor, {'title': 'Original', 'status': 'in_progress'}])
    _, key = progress.read()['taskKeys']
    linked = _delegate_link(r, key)
    r.publish([anchor, {'title': 'Renamed', 'status': 'in_progress'}])
    r.publish([anchor])
    command = r.c.client.command
    competed = False

    def competing_head(operation, payload, command_id):
        nonlocal competed
        if command_id.startswith('coordinator-progress-cas:'):
            if race == 'reject':
                raise StorageError('database_conflict', 'rejected candidate')
            if not competed:
                competed = True
                r.publish([{**anchor, 'progress': 'Competing revision'}])
        return command(operation, payload, command_id)

    monkeypatch.setattr(r.c.client, 'command', competing_head)
    if race == 'reject':
        with pytest.raises(StorageError, match='changed concurrently'):
            r.publish([anchor, {'title': 'Never committed', 'status': 'pending',
                               'blocker': {'kind': 'next_action', 'action': 'Draft the comparison'}}])
    else:
        result = r.publish([anchor, {'title': 'Renamed', 'status': 'in_progress'}])
        assert competed and result['board']['revision'] == 5
        assert progress.read()['taskKeys'][1] == key
    monkeypatch.setattr(r.c.client, 'command', command)
    entries = CoordinatorTaskHistory(repo.owner, repo.project, client=repo.client,
        bindings=repo.binding_repository).tasks('coordinator')
    by_title = {entry['title']: entry for entry in entries}
    assert 'Never committed' not in by_title and 'Original' not in by_title
    assert by_title['Renamed']['key'] == key
    assert by_title['Renamed']['onBoard'] == (race == 'advance')
    assert all(entry['status'] != 'completed' for entry in entries)
    end(r, delegate(r, 'wake-trigger'))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert f'- [active_owner] {key}: {linked["id"]}' in content
    assert 'Never committed' not in content and '[ownership_unknown] Renamed' not in content
    if race == 'advance':
        assert '- [in_progress] Renamed' in content
    else:
        assert '- [in_progress] Renamed' not in content
    assert '- [ownership_unknown] Anchor' in content


@pytest.mark.parametrize('settled_first', [False, True])
def test_independent_request_origins_remain_visible_once_per_source(wake_inputs, monkeypatch, settled_first):
    from copy import deepcopy
    from lib.conversations.coordinator_continuation import _stated_outcomes

    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    first = deepcopy(controls(r)[-1])
    second = deepcopy(first)
    second['id'] = 'second-request'
    second['request']['delegation']['attemptId'] = 'second-origin'
    second['request']['authorizationSource']['turnId'] = 'second-owner-input'
    r.turns['second-owner-input'] = {'actor': 'human', 'projection': {'content': 'Independent second request'}}
    if settled_first:
        value, version = r.c.repo._load()
        value.setdefault('coordinatorSettlements', []).append({
            'sourceAttemptId': first['request']['delegation']['attemptId']})
        monkeypatch.setattr(r.c.repo, '_load', lambda: (value, version))
    repeated = deepcopy(second)
    repeated['id'] = 'second-request-wake'
    outcomes = _stated_outcomes(r.c.repo, [first, second, repeated], set())
    titles = [entry['title'] for entry in outcomes]
    assert titles.count('Independent second request') == 1
    assert (OWNER_WORDS in titles) == (not settled_first)


@pytest.mark.parametrize('verdict', ['pass', 'fail', 'impasse'])
@pytest.mark.parametrize('receipt_status', ['pending', 'blocked', 'skipped'])
def test_accepted_result_only_audit_does_not_prescribe_checkpoint_publication(wake_inputs, verdict, receipt_status):
    from copy import deepcopy
    from lib.conversations.coordinator_continuation import continuation_context
    from lib.project_dispatch import admission

    r = wake_inputs
    r.args = {**r.args, 'brief': 'Audit another author checkpoint ' + 'a' * 40
              + ' independently. Read-only: return findings and a verdict, not project files.'}
    callbacks = []
    r.service.publication_wake = lambda: callbacks.append('wake')
    r.service.publication_release = lambda *args, **kwargs: callbacks.append('release')
    reviewer = delegate(r, 'independent-audit')
    end(r, reviewer, delivered=False)
    turn = r.turns[reviewer['execution']['turnId']]
    turn['projection']['content'] = ('Read-only independent audit of another author checkpoint '
                                      + 'a' * 40 + '.\nREVIEW_VERDICT: ' + verdict)
    note = f'Verdict {verdict} recorded; no files to publish.'
    assert r.call('review_project_work', assignment_id=reviewer['id'], result_revision=1,
                  decision='accept', note=note)['decision'] == 'accept'
    work_id = reviewer['execution']['taskId']
    r.workspaces[work_id] = {'task_id': work_id, 'user_id': 7, 'project_root': '/project',
        'checkpoint_sha': 'b' * 40,
        'origin': {admission.KEY: r.c.repo.admission(reviewer['id']),
                   'delivery': {'state': receipt_status, 'taskOutcome': 'completed'}}}
    before = deepcopy(r.c.repo.read(reviewer['id']))
    content = continuation_context(r.c.repo, reviewer)
    assert reviewer['id'] in content  # An unresolved receipt is evidence, not silently settled.
    assert 'accepted result, no publication requested' in content
    assert 'inspect get_project_assignment' in content
    assert 'repeat review_project_work' not in content
    assert 'checkpoint_sha' not in content
    assert r.c.repo.read(reviewer['id']) == before
    assert before['review']['note'] == note and before.get('publication') is None
    assert not callbacks
    assert r.workspaces[work_id]['origin']['delivery']['state'] == receipt_status


@pytest.mark.parametrize('receipt_status', ['pending', 'blocked', 'unknown'])
def test_unpinned_authored_or_unknown_delivery_stays_actionable_without_using_note_prose(wake_inputs, receipt_status):
    from lib.conversations.coordinator_continuation import continuation_context
    from lib.project_dispatch import admission

    r = wake_inputs
    author = delegate(r, 'author-with-delta')
    end(r, author, delivered=False)
    assert r.call('review_project_work', assignment_id=author['id'], result_revision=1,
                  decision='accept', note='Read-only reviewer: no files to publish.')['decision'] == 'accept'
    work_id = author['execution']['taskId']
    r.workspaces[work_id] = {'task_id': work_id, 'user_id': 7, 'project_root': '/project',
        'checkpoint_sha': 'c' * 40,
        'origin': {admission.KEY: r.c.repo.admission(author['id']),
                   'delivery': {'state': receipt_status, 'taskOutcome': 'completed'}}}
    content = continuation_context(r.c.repo, author)
    assert author['id'] in content
    assert 'workspace receipt is not final' in content
    assert 'inspect get_project_assignment' in content


@pytest.mark.parametrize('receipt_status, expected', [('pending', 'accepted, publishing'),
                                                       ('blocked', 'delivery held')])
def test_pinned_real_publication_keeps_its_delivery_action(wake_inputs, receipt_status, expected):
    from lib.conversations.coordinator_continuation import continuation_context
    from tests.project_mod.test_coordinator_dependency_links import completed

    r = wake_inputs
    author, pin = completed(r)
    result = r.call('review_project_work', assignment_id=author['id'], result_revision=1,
                    decision='accept', note='Verified authored files.', checkpoint_sha=pin['checkpointSha'])
    assert result['checkpoint_sha'] == pin['checkpointSha']
    r.workspaces[author['execution']['taskId']]['origin']['delivery'].update(
        state=receipt_status, reason='synthetic hold' if receipt_status == 'blocked' else '')
    content = continuation_context(r.c.repo, author)
    assert author['id'] + ': ' + expected in content
    assert 'no publication requested' not in content


def test_canonical_no_change_audit_without_publication_is_omitted(wake_inputs):
    from copy import deepcopy
    from lib.conversations.coordinator_continuation import continuation_context

    r = wake_inputs
    reviewer = delegate(r, 'read-only-audit')
    end(r, reviewer)
    workspace = r.workspaces[reviewer['execution']['taskId']]
    workspace['origin']['delivery'].update(publicationMode='working_tree', phase='no_changes')
    receipt = r.call('review_project_work', assignment_id=reviewer['id'], result_revision=1,
                     decision='accept', note='Audit received.')
    assert receipt['canonical_delivery'] is True and receipt['checkpoint_sha'] is None
    assert receipt['delivery']['status'] == 'delivered'
    before = deepcopy(r.c.repo.read(reviewer['id']))
    content = continuation_context(r.c.repo, reviewer)
    assert reviewer['id'] not in content
    assert 'No Worker needs you now' in content
    assert r.c.repo.read(reviewer['id']) == before
    assert before.get('publication') is None


def _board_line(task, *, dispatched=None, assignment_ids=(), active_ids=(), wake_times=()):
    from lib.conversations.coordinator_continuation import _board_task_line
    return _board_task_line(task, dispatched or {}, set(assignment_ids), set(active_ids),
                            list(wake_times))


def test_an_abstract_wait_reason_is_idle_and_its_prose_is_never_projected():
    task = {'title': 'Recover the failed sessions', 'status': 'in_progress', 'key': 'a' * 32,
            'firstSeenAt': 1000, 'reason': 'The original route and upstream readiness cannot be proved.',
            'progress': '10 sessions not replayed.'}
    line = _board_line(task, wake_times=[500, 1000, 2000, 3000])
    assert line.startswith('- [in_progress] Recover the failed sessions:')
    assert 'idle: dispatch or cancel' in line
    assert 'dispatched: no' in line
    assert 'open 3 continuation turns without a dispatch' in line
    assert '(task key ' + 'a' * 32 + ')' in line
    assert 'original route' not in line and '10 sessions' not in line


def test_a_running_assignment_is_shown_by_id_and_the_row_is_not_idle():
    task = {'title': 'Verify the snapshot', 'status': 'in_progress', 'key': 'b' * 32,
            'firstSeenAt': 1000, 'blocker': {'kind': 'active_dispatch', 'assignmentId': 'assignment-7'}}
    line = _board_line(task, dispatched={'b' * 32: [('assignment-7', 'running')]},
                       assignment_ids={'assignment-7'}, active_ids={'assignment-7'},
                       wake_times=[1000, 2000])
    assert 'Waiting for: Worker assignment-7 is running' in line
    assert 'idle' not in line
    assert 'dispatched: yes' in line
    assert 'open 0 continuation turns without a dispatch' in line
    queued = {**task, 'blocker': {'kind': 'active_dispatch', 'assignmentId': 'assignment-8'}}
    assert 'idle' not in _board_line(queued, assignment_ids={'assignment-8'},
                                     active_ids={'assignment-8'})


def test_an_owner_question_blocker_renders_its_plain_language_question():
    task = {'title': 'Restart the host', 'status': 'pending', 'key': 'c' * 32, 'firstSeenAt': 1000,
            'blocker': {'kind': 'needs_owner_decision', 'questionId': 'ask-42',
                        'question': 'May I restart the host now?'}}
    line = _board_line(task, wake_times=[1000, 2000])
    assert 'Waiting for: your answer to ask-42: May I restart the host now?' in line
    assert 'idle' not in line and 'dispatched: no' in line


def test_an_external_resource_blocker_renders_the_exact_resource_check_and_owner_action():
    task = {'title': 'Fix the macOS installer', 'status': 'pending', 'key': 'd' * 32,
            'firstSeenAt': 1000, 'blocker': {
                'kind': 'needs_external_resource', 'resource': 'Apple signing certificate',
                'check': 'security find-identity -v -p codesigning',
                'ownerAction': 'confirm the certificate on the Mac'}}
    line = _board_line(task, wake_times=[1000])
    assert ('Waiting for: Apple signing certificate (proved by: security find-identity -v -p codesigning); '
            'you: confirm the certificate on the Mac') in line
    assert 'idle' not in line and 'dispatched: no' in line


def test_an_unavailable_resource_is_a_limit_not_a_wait():
    task = {'title': 'Replay the lost month', 'status': 'pending', 'key': 'k' * 32,
            'firstSeenAt': 1000, 'blocker': {
                'kind': 'needs_external_resource', 'resource': 'the deleted upstream archive',
                'check': 'the archive lists the missing month', 'unavailable': True,
                'limit': 'replay before March cannot be verified'}}
    line = _board_line(task, wake_times=[1000, 2000])
    assert line.startswith('- [pending, limit] Replay the lost month:')
    assert ('Limit: the deleted upstream archive cannot be obtained now; stays unverified: '
            'replay before March cannot be verified') in line
    assert 'Waiting for:' not in line and 'idle: dispatch or cancel' not in line
    assert 'dispatched: no' in line


def test_a_legacy_row_without_a_typed_blocker_is_idle_but_preserved():
    task = {'title': 'Run benchmark evaluations', 'status': 'pending', 'key': 'e' * 32,
            'firstSeenAt': 1000, 'reason': 'No fixed task list, matching build or isolation proof yet.',
            'progress': 'Offline qualification matrix reviewed.'}
    line = _board_line(task, wake_times=[1000, 2000])
    assert line.startswith('- [pending] Run benchmark evaluations: idle: dispatch or cancel')
    assert 'dispatched: no' in line and 'open 2 continuation turns without a dispatch' in line
    assert 'No fixed task list' not in line and 'Offline qualification' not in line


def test_a_typed_dependency_is_a_wait_not_an_idle_row():
    task = {'title': 'Land the migration', 'status': 'pending', 'key': 'f' * 32, 'firstSeenAt': 1000,
            'blocker': {'kind': 'blocked_by_dependency', 'assignmentId': 'assignment-9', 'revision': 4}}
    line = _board_line(task, wake_times=[1000])
    assert 'Waiting for: assignment assignment-9 at revision 4' in line
    assert 'idle' not in line


def test_unreadable_dispatch_facts_never_read_as_a_dispatch_or_as_idle():
    task = {'title': 'Recover the failed sessions', 'status': 'pending', 'key': 'j' * 32,
            'firstSeenAt': 1000, 'reason': 'The original route cannot be proved.'}
    from lib.conversations.coordinator_continuation import _board_task_line
    line = _board_task_line(task, None, None, None, [])
    assert 'dispatch facts unreadable' in line
    assert 'idle' not in line and 'dispatched:' not in line


def test_a_concrete_next_action_is_actionable_while_a_generic_one_is_idle():
    concrete = {'title': 'Write the report', 'status': 'pending', 'key': 'g' * 32, 'firstSeenAt': 1000,
                'blocker': {'kind': 'next_action', 'action': 'Run the offline export and attach its log.'}}
    generic = {'title': 'Monitor the queue', 'status': 'pending', 'key': 'h' * 32, 'firstSeenAt': 1000,
               'blocker': {'kind': 'next_action', 'action': 'continue review'}}
    assert 'idle' not in _board_line(concrete, wake_times=[1000])
    assert 'idle: dispatch or cancel' in _board_line(generic, wake_times=[1000])


@pytest.mark.parametrize('receipt_status', ['pending', 'blocked', 'unknown'])
@pytest.mark.parametrize('association', ['reviewer', 'other-coordinator', 'author', 'unavailable'])
@pytest.mark.parametrize('earlier_request', [False, True])
@pytest.mark.parametrize('currency', ['current', 'follow-up', 'unknown-birth', 'stale', 'display-only',
                                     'unreadable', 'missing-revision', 'consumed-steer',
                                     'consumed-assign', 'submitted-steer', 'other-attempt-steer',
                                     'mailbox-unavailable', 'delivered-human-steer', 'pending-human-steer',
                                     'direct-considered', 'unknown-considerer'])
def test_historical_reviewer_contract_uncertainty_preserves_other_worker_actions(
        wake_inputs, monkeypatch, receipt_status, association, earlier_request, currency):
    from copy import deepcopy
    from lib.conversations import review_loops
    from lib.conversations.coordinator_continuation import _workers
    from lib.project_dispatch import admission

    r = wake_inputs
    callbacks = []
    r.service.publication_wake = lambda: callbacks.append('wake')
    r.service.publication_release = lambda *args, **kwargs: callbacks.append('release')
    completed = delegate(r, 'completed-reviewer')
    active = delegate(r, 'active-reviewer')
    unreviewed = delegate(r, 'unreviewed-reviewer')
    author = delegate(r, 'ordinary-author')
    pinned = delegate(r, 'explicit-publication')
    known_reviewed = delegate(r, 'known-reviewed-delivered')
    end(r, known_reviewed)
    assert r.call('review_project_work', assignment_id=known_reviewed['id'], result_revision=1,
                  decision='accept', note='Received canonical result.')['canonical_delivery'] is True
    for row in (completed, unreviewed, author, pinned):
        end(r, row, delivered=False)
        if row == completed and currency == 'follow-up':
            from types import SimpleNamespace
            class FollowUpCommands:
                def create_turn(self, conversation, owner, body, **kwargs):
                    trusted = kwargs['trusted_dispatch']
                    trusted.capture({}, owner, kwargs['principal'], conversation)
                    r.turns['repair-turn'] = {'conversationId': conversation, 'turnId': 'repair-turn',
                        'actor': 'assistant', 'projectionRevision': 1}
                    r.attempts['repair-attempt'] = {'conversationId': conversation, 'turnId': 'repair-turn',
                        'attemptId': 'repair-attempt', 'taskId': '', 'status': 'pending',
                        'commandId': body['commandId']}
                    trusted.continue_execution('repair-turn', 'repair-attempt')
                    return SimpleNamespace(value={})
            original_commands = r.service.commands
            r.service.commands = FollowUpCommands()
            previous = row['execution']
            r.service.follow_up(row['id'], turn_id=previous['turnId'], attempt_id=previous['attemptId'],
                expected_revision=1, instruction='Repair the source file.', command_id='synthetic-follow-up')
            r.service.commands = original_commands
            completed = row = r.c.repo.read(row['id'])
            assert row['execution']['attemptId'] == 'repair-attempt'
            r.attempts['repair-attempt']['taskId'] = row['execution']['taskId']
            end(r, row, delivered=False)
        if row != unreviewed:
            assert r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                          decision='accept', note='Received.')['decision'] == 'accept'
        work_id = row['execution']['taskId']
        r.workspaces[work_id] = {'task_id': work_id, 'user_id': 7, 'project_root': '/project',
            'checkpoint_sha': 'b' * 40,
            'origin': {admission.KEY: r.c.repo.admission(row['id']),
                       'delivery': {'state': receipt_status, 'taskOutcome': 'completed',
                                    'reason': 'project_publication_authority_missing'}}}
    r.workspaces[pinned['execution']['taskId']]['origin']['delivery']['reason'] = 'synthetic hold'
    # The projection consumes a pre-existing publication pin, without requesting publication.
    def pin(value):
        row = r.c.repo._find(value, pinned['id'])
        row['publication'] = {'checkpointSha': 'b' * 40, 'resultRevision': 1,
                              'grantRevision': row['grant']['revision'], 'requestedAt': 1001,
                              'subject': 'coordinator:coordinator-task'}
    r.c.repo._change(pin)
    scope = {'user_id': 7, 'project_path': '/project',
             'conversation_id': 'other' if association == 'other-coordinator' else 'coordinator'}
    loop_id = 'review-loop-synthetic'
    loop = review_loops.create(scope, loop_id=loop_id, author_assignment_id=author['id'],
        title='Unrelated title', brief='Synthetic audit', review={'reviewers': [{'role': 'custom',
        'focus': 'Synthetic scope'}]}, client=r.c.repo.client)
    members = (active, unreviewed, pinned, known_reviewed) if association == 'author' else (
        completed, active, unreviewed, pinned, known_reviewed)
    loop['rounds'] = [{'round': 1, 'author': {'assignmentId': completed['id'] if association == 'author'
                                          else author['id']},
                       'reviewers': [{'label': 'custom1', 'assignmentId': row['id'], 'verdict': None}
                                     for row in members], 'findings': None, 'rework': None}]
    assert review_loops.save(scope, loop, 1, client=r.c.repo.client) == 2
    assert review_loops.read(scope, loop_id, client=r.c.repo.client)[0] == loop
    attempt = r.attempts[completed['execution']['attemptId']]
    if currency != 'follow-up':
        attempt['commandId'] = 'project-dispatch:' + completed['id']
    turn = r.turns[completed['execution']['turnId']]
    turn['modelContextRevision'] = 1
    if currency == 'unknown-birth':
        attempt.pop('commandId')
    elif currency == 'stale':
        turn['modelContextRevision'] = 2
    elif currency == 'display-only':
        turn['projectionRevision'] = 9
    elif currency == 'missing-revision':
        turn.pop('modelContextRevision')
        turn.pop('projectionRevision')
    if currency == 'unreadable':
        original_turn_get = r.c.repo.turn_get
        def unreadable(conversation, turn_id, **kwargs):
            if turn_id == completed['execution']['turnId']:
                raise RuntimeError('synthetic unavailable Turn metadata')
            return original_turn_get(conversation, turn_id, **kwargs)
        monkeypatch.setattr(r.c.repo, 'turn_get', unreadable)
    if currency in ('consumed-steer', 'consumed-assign', 'submitted-steer', 'other-attempt-steer'):
        from lib.conversations.project_messages import ProjectMailbox
        mailbox = ProjectMailbox(7, '/project', completed['request']['recipientId'], client=r.c.repo.client)
        message = mailbox.submit('coordinator', {'kind': 'assign' if currency == 'consumed-assign' else 'steer',
            'content': 'Synthetic instruction', 'idempotency_key': 'instruction'}, 'synthetic-lead-task')
        if currency != 'submitted-steer':
            mailbox.confirm([message['id']], attempt['taskId'], 1,
                turn_id=attempt['turnId'] if currency != 'other-attempt-steer' else 'other-turn',
                attempt_id=attempt['attemptId'] if currency != 'other-attempt-steer' else 'other-attempt')
            confirmed = mailbox.get(message['id'])
            assert confirmed['state'] == 'input_confirmed' and confirmed['decision'] == 'accepted'
            turn['modelContextRevision'] = 2
            def accept_current(value):
                r.c.repo._find(value, completed['id'])['review']['resultRevision'] = 2
            r.c.repo._change(accept_current)
    if currency in ('delivered-human-steer', 'pending-human-steer'):
        turn['projection']['_userSteerInjects'] = [{'blockId': 'injection:user-steer:synthetic',
            'commandId': 'synthetic', 'deliveryState': 'delivered' if currency == 'delivered-human-steer'
            else 'pending', 'author': {'actor': 'human', 'text': 'Synthetic instruction'}}]
    if currency in ('direct-considered', 'unknown-considerer'):
        from lib.conversations.project_messages import ProjectMailbox
        mailbox = ProjectMailbox(7, '/project', completed['request']['recipientId'], client=r.c.repo.client)
        message = mailbox.submit('coordinator', {'kind': 'steer', 'content': 'Synthetic instruction',
            'idempotency_key': 'direct-read'}, 'synthetic-lead-task')
        assert mailbox.get(message['id'])['state'] == 'submitted'
        mailbox.consider({'message_id': message['id'], 'decision': 'accepted', 'note': 'Read through tool',
            'expected_revision': message['revision']}, attempt['taskId'] if currency == 'direct-considered'
            else 'unknown-task')
        assert mailbox.get(message['id'])['state'] == 'submitted'
    if currency in ('delivered-human-steer', 'direct-considered', 'unknown-considerer'):
        turn['modelContextRevision'] = 2
        def accept_redirected(value):
            r.c.repo._find(value, completed['id'])['review']['resultRevision'] = 2
        r.c.repo._change(accept_redirected)
    if currency == 'mailbox-unavailable':
        from lib.conversations.project_messages import ProjectMailbox
        def unavailable_mailbox(*args, **kwargs):
            raise RuntimeError('synthetic unreadable mailbox')
        monkeypatch.setattr(ProjectMailbox, 'read', unavailable_mailbox)
    before = deepcopy((r.c.client.rows, r.workspaces, r.attempts, r.turns))
    from lib.conversations.project_messages import CONTRACT as MESSAGE_CONTRACT
    query = r.c.client.query
    extra_reads = []
    def no_purpose_scan(operation, params):
        if params.get('namespace') in (review_loops.NAMESPACE, MESSAGE_CONTRACT['namespace']):
            extra_reads.append(operation)
            raise AssertionError('Continuation must not scan instruction history')
        return query(operation, params)
    monkeypatch.setattr(r.c.client, 'query', no_purpose_scan)
    wake = deepcopy(completed)
    if earlier_request:
        wake['request']['delegation']['attemptId'] = 'another-request'
    phases = {identity: phase for identity, phase, _ in _workers(r.c.repo, wake)}
    assert phases[completed['id']] == 'unpinned'
    assert known_reviewed['id'] not in phases
    assert phases[author['id']] == 'unpinned'
    assert (phases.get(active['id']) == 'running') == (not earlier_request)
    assert phases[unreviewed['id']] == 'needs_review'
    assert (phases.get(pinned['id']) in ('held', 'delivering')) == (not earlier_request)
    assert (r.c.client.rows, r.workspaces, r.attempts, r.turns) == before
    assert callbacks == []
    assert extra_reads == []
def test_the_wake_brief_wires_typed_blockers_and_dispatch_facts(wake_inputs, monkeypatch):
    from lib.conversations import coordinator_continuation as continuation

    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    wake = controls(r)[-1]
    monkeypatch.setattr(continuation, '_board', lambda repository, assignment: [
        {'title': 'Verify the snapshot', 'status': 'in_progress', 'key': 'b' * 32,
         'firstSeenAt': 1000, 'blocker': {'kind': 'active_dispatch', 'assignmentId': 'assignment-7'}}])
    monkeypatch.setattr(continuation, '_ownership', lambda *args, **kwargs: {
        'active': {}, 'unknown': [], 'dropped': [],
        'dispatched': {'b' * 32: [('assignment-7', 'running')]},
        'activeIds': {'assignment-7'}, 'assignmentIds': {'assignment-7'}})
    content = continuation.continuation_context(r.c.repo, wake)
    assert '[in_progress] Verify the snapshot: Waiting for: Worker assignment-7 is running' in content
    assert 'dispatched: yes' in content and 'idle' not in content


def test_the_memory_subject_keeps_the_typed_wait_but_drops_computed_facts():
    from lib.conversations.coordinator_continuation import (
        HEADER, _BOARD_HEADING, continuation_context_subject)

    task = {'title': 'Verify the snapshot', 'status': 'in_progress', 'key': 'i' * 32,
            'firstSeenAt': 1000, 'blocker': {'kind': 'active_dispatch', 'assignmentId': 'assignment-7'}}
    line = _board_line(task, dispatched={'i' * 32: [('assignment-7', 'running')]},
                       assignment_ids={'assignment-7'}, active_ids={'assignment-7'})
    subject = continuation_context_subject(HEADER + _BOARD_HEADING + '\n' + line)
    assert 'Verify the snapshot' in subject
    assert 'Waiting for: Worker assignment-7 is running' in subject
    assert 'dispatched:' not in subject and 'continuation turn' not in subject