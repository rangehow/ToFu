"""Coordinator wake verdicts read a lane past the response bound in bounded pages.

Incident 2026-11-12 15:55: after the restart every wake of the Tofu Project
Router failed with ``turn list exceeds its bounded streamed payload``. The
lane-tail verdict of its restart-interrupted Turn (``lane_tail_verdict``), the
decision-reply check (``_human_replied``) and the owner-question answer check
asked ``list_turns`` for the Turns after one ordinal,
and ``list_turns`` read the whole lane with ``turn.list`` before filtering it.
That lane had grown past the native response bound, so every pass was refused
and kept retrying. ``BoundedReads`` applies the same deterministic refusal at a
unit-test size: every single Turn fits, the whole lane does not.
"""
from copy import deepcopy

import pytest

from tests.support.response_bound import BoundedReads
from tests.conversation.test_restart_resume import OWNER, chats as chats
from tests.turn_lifecycle.test_task_event_delivery import storage as storage

pytestmark = pytest.mark.unit

__all__ = ('chats', 'storage')

_LARGE_REPORT_TEXT = 'Worker evidence for the review. ' * 1100
CONVERSATION = 'coordinator-past-the-bound'


def _report(turns, kind, index, content):
    return turns.append_settled_turn(
        CONVERSATION, command_id=f'{CONVERSATION}:{kind}:{index}', actor='assistant',
        kind=f'coordinator_report.{kind}', user_id=OWNER,
        projection={'content': content, 'origin': {'initiator': 'brain'}})['turn']


@pytest.fixture
def lane(chats, monkeypatch, tmp_path):
    """A restart-interrupted Coordinator Turn, then its decision report and reports past the bound."""
    from types import SimpleNamespace
    import lib.storage
    from lib.project_dispatch.repository import ProjectDispatchRepository

    c = chats
    task = c.running(CONVERSATION, content='reviewing the Workers')
    c.settle(task, 'server_shutdown')
    interrupted = c.turns.get_turn(CONVERSATION, task['_turnId'], user_id=OWNER,
                                   include_projection=False)
    assert interrupted['settlement']['cause'] == 'server_restart'
    decision = _report(c.turns, 'decision', 0, 'Option A or B? I recommend A.')
    reports = [_report(c.turns, 'progress', index, _LARGE_REPORT_TEXT) for index in range(4)]

    def owner_input():
        return c.turns.create_turn_pair(
            CONVERSATION, command_id=f'{CONVERSATION}:owner-input',
            input_projection={'content': 'A newer instruction'},
            config=deepcopy(task['config']), user_id=OWNER)

    operations, refusals, paged = [], [], []
    authority = lib.storage.get_storage_client

    class Recorded(BoundedReads):
        def query(self, operation, payload, *args, **kwargs):
            operations.append(operation)
            result = super().query(operation, payload, *args, **kwargs)
            if operation == 'turn.sync.page' and isinstance(result, dict):
                paged.extend(row['turnId'] for row in result.get('turns') or ())
            return result

    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda **kwargs: Recorded(authority(**kwargs), refusals))
    return SimpleNamespace(
        c=c, task=task, interrupted=interrupted, decision=decision, reports=reports,
        owner_input=owner_input, operations=operations, refusals=refusals, paged=paged,
        repository=ProjectDispatchRepository(OWNER, str(tmp_path)))


def _assert_bounded(lane):
    assert lane.refusals == []
    assert 'turn.list' not in lane.operations


def _assert_whole_lane_refused(lane):
    from lib.storage.errors import StorageError
    with pytest.raises(StorageError, match='bounded streamed payload'):
        lane.c.turns.list_turns(CONVERSATION, user_id=OWNER, lane_id='main', light=True)


def test_the_lane_tail_verdict_past_the_bound_reads_only_the_suffix(lane):
    from lib.conversations.restart_resume import read_lane_tail_verdict
    assignment = {'id': 'wake', 'review': None, 'request': {'recipientId': CONVERSATION},
                  'grant': {'owner': OWNER},
                  'execution': {'turnId': lane.task['_turnId'], 'attemptId': lane.task['_attemptId']}}

    resume = lane.repository.restart_resume(assignment)
    assert resume['verdict'] == {'tail': False, 'later': 5, 'supersededBy': None, 'queued': False}
    assert resume['option']['operation'] in ('continue', 'checkpoint_resume')
    _assert_bounded(lane)
    # Only the later Turns are materialized, never the interrupted Turn itself.
    assert sorted(lane.paged) == sorted([lane.decision['turnId'],
                                         *(report['turnId'] for report in lane.reports)])

    created = lane.owner_input()
    verdict = lane.repository.lane_tail_verdict(CONVERSATION, lane.interrupted)
    assert verdict['supersededBy'] == created['turn']['parentTurnId']
    assert verdict['later'] == 7
    assert verdict == read_lane_tail_verdict(OWNER, CONVERSATION, lane.interrupted)
    assert lane.repository.restart_resume(assignment)['option'] is None
    _assert_bounded(lane)
    _assert_whole_lane_refused(lane)


def test_a_decision_reply_past_the_bound_is_read_from_the_suffix(lane):
    from lib.project_dispatch.continuation import _human_replied

    assert _human_replied(lane.repository, CONVERSATION, lane.decision['turnId']) is False
    lane.owner_input()
    assert _human_replied(lane.repository, CONVERSATION, lane.decision['turnId']) is True
    _assert_bounded(lane)
    _assert_whole_lane_refused(lane)


def test_an_owner_answer_past_the_bound_closes_the_question(lane):
    """A question indexed before answers were recorded is decided by a bounded lane read; the
    owner's next message records its answer as the Turn is written."""
    import lib.storage
    from lib.router.proactive.questions import NAMESPACE, open_questions
    key = f'{OWNER}:{CONVERSATION}:hg-past-the-bound'
    lib.storage.get_storage_client(write=True).command('record.put', {
        'namespace': NAMESPACE, 'key': key, 'expected_version': 0,
        'value': {'owner': OWNER, 'coordinatorId': CONVERSATION, 'projectPath': '/project',
                  'guidanceId': 'hg-past-the-bound', 'turnId': lane.interrupted['turnId'], 'blocking': False,
                  'question': 'Proceed?', 'createdAt': 1}}, 'legacy-question:past-the-bound')

    assert [row['guidanceId'] for row in open_questions(OWNER, coordinator_id=CONVERSATION)] == [
        'hg-past-the-bound']
    created = lane.owner_input()
    assert open_questions(OWNER, coordinator_id=CONVERSATION) == []
    recorded = lib.storage.get_storage_client().query('record.get', {'namespace': NAMESPACE, 'key': key})
    assert recorded['value']['answeredOrdinal'] == created['submittedTurn']['ordinal']
    _assert_bounded(lane)
    _assert_whole_lane_refused(lane)


def test_a_suffix_read_returns_what_filtering_the_whole_lane_returned(chats):
    """Below the bound both reads succeed: the suffix read keeps every row, order and limit."""
    c = chats
    task = c.running(CONVERSATION, content='reviewing the Workers')
    c.settle(task, 'server_shutdown')
    for index in range(3):
        _report(c.turns, 'progress', index, f'Progress {index}')
    c.turns.create_turn_pair(CONVERSATION, command_id=f'{CONVERSATION}:owner-input',
                             input_projection={'content': 'A newer instruction'},
                             config=deepcopy(task['config']), user_id=OWNER)
    for light in (False, True):
        whole = c.turns.list_turns(CONVERSATION, user_id=OWNER, lane_id='main', light=light)['turns']
        ordinals = [row['ordinal'] for row in whole]
        assert len(whole) == 7 and ordinals == sorted(ordinals)
        for after in range(ordinals[0] - 1, ordinals[-1] + 2):
            page = c.turns.list_turns(CONVERSATION, user_id=OWNER, lane_id='main',
                                      after_ordinal=after, light=light)
            assert page['turns'] == [row for row in whole if row['ordinal'] > after]
            assert page['authoritativeFull'] is False
            assert c.turns.list_turns(CONVERSATION, user_id=OWNER, lane_id='main', after_ordinal=after,
                                      limit=2, light=light)['turns'] == page['turns'][:2]


def test_a_suffix_needs_its_lane(chats):
    """An ordinal belongs to one lane; a cross-lane ordinal filter has no meaning."""
    with pytest.raises(ValueError, match='lane_id'):
        chats.turns.list_turns(CONVERSATION, user_id=OWNER, after_ordinal=3)
