"""Every decision not to resume an interrupted Worker lane leaves a trace (D8).

After the 2026-11-06 restart, four of five interrupted lanes got no restart
attempt, and nothing said why: ``recover_project_dispatch`` folded eight
unrelated conditions (a grant without the durable-Stop lifecycle, a revoked
grant, a passed deadline, an execution that was not restart-paused, ...) into
one ``skipped`` counter. The reason is now named per assignment, while plain
filters and settled executions stay quiet.
"""
import logging

import pytest

from lib.project_dispatch import recovery

pytestmark = pytest.mark.unit

NOW = 1_000.0


def _assignment(assignment_id, *, execution=True, revoked=False, lifecycle=1,
                deadline=None, recipient='worker-a'):
    return {
        'id': assignment_id,
        'request': {'profile': {'mode': 'project'}, 'projectPath': '/project',
                    'recipientId': recipient, 'coordinatorId': 'coordinator',
                    'budget': {'deadline': deadline}, 'dependencies': []},
        'grant': {'lifecycleVersion': lifecycle, 'owner': 1, 'revoked': revoked,
                  'createdAt': 1.0, 'revision': 1},
        'execution': {'turnId': 't', 'attemptId': 'a'} if execution else None,
        'modelAttempts': [],
    }


_TAIL = {'tail': True, 'later': 0, 'supersededBy': None, 'queued': False}


class _Repository:
    owner, project, storage_version = 1, '/project', 2

    def __init__(self, rows, paused, verdicts=None):
        self.rows, self.paused, self.verdicts = rows, paused, verdicts or {}

    def discover(self):
        return self.rows, set()

    def read_attempts(self, attempt_ids):
        return {}

    def restart_resume(self, assignment, *, attempt=None):
        if assignment['id'] not in self.paused:
            return None
        verdict, option = self.verdicts.get(assignment['id'], (_TAIL, {'operation': 'continue'}))
        return {'turn': {'turnId': 't'}, 'verdict': verdict, 'option': option}

    def stopped(self, assignment):
        return False


def _recover(rows, paused, verdicts=None, **kwargs):
    return recovery.recover_project_dispatch(
        1, '/project', repository=_Repository(rows, paused, verdicts),
        service=object(), was_aborted_after=lambda *a: False, clock=lambda: NOW, **kwargs)


def _lines(caplog):
    return {(record.levelno, record.getMessage()) for record in caplog.records
            if 'unresumed' in record.getMessage()}


def test_an_interrupted_lane_left_unresumed_names_why(caplog):
    rows = [_assignment('revoked', revoked=True),
            _assignment('expired', deadline=NOW - 1),
            _assignment('legacy', lifecycle=0)]
    with caplog.at_level(logging.DEBUG, logger='lib.project_dispatch.recovery'):
        stats = _recover(rows, paused={'revoked', 'expired', 'legacy'})

    assert stats['skipped'] == 3
    lines = _lines(caplog)
    assert (logging.WARNING, '[ProjectDispatch] recovery left assignment=revoked owner=1 '
            'unresumed: grant_revoked') in lines
    assert (logging.WARNING, '[ProjectDispatch] recovery left assignment=expired owner=1 '
            'unresumed: deadline_passed') in lines
    assert (logging.INFO, '[ProjectDispatch] recovery left assignment=legacy owner=1 '
            'unresumed: grant_without_durable_stop_lifecycle') in lines


def test_filters_and_settled_lanes_stay_quiet(caplog):
    rows = [_assignment('settled'), _assignment('elsewhere', recipient='worker-b')]
    with caplog.at_level(logging.INFO, logger='lib.project_dispatch.recovery'):
        stats = _recover(rows, paused=set(), recipient_id='worker-a')

    assert stats['skipped'] == 2
    assert _lines(caplog) == set()


def test_a_restart_paused_lane_that_cannot_continue_in_place_names_why(caplog):
    """A Worker Turn a later input superseded (the newer input owns the lane) or one
    whose settlement offers only Regenerate (2026-11-10: parallel 1be49eb0 failed with
    database_conflict at every boot) is left for its Coordinator, never resumed."""
    moved = {'tail': False, 'later': 2, 'supersededBy': 'owner-input', 'queued': True}
    rows = [_assignment('superseded'), _assignment('regenerate-only')]
    with caplog.at_level(logging.DEBUG, logger='lib.project_dispatch.recovery'):
        stats = _recover(rows, paused={'superseded', 'regenerate-only'}, verdicts={
            'superseded': (moved, None), 'regenerate-only': (_TAIL, None)})

    assert stats['skipped'] == 2 and stats['eligible'] == 0 and stats['failed'] == 0
    lines = _lines(caplog)
    assert (logging.INFO, '[ProjectDispatch] recovery left assignment=superseded owner=1 '
            'unresumed: restart_paused_turn_superseded_by:owner-input') in lines
    assert (logging.INFO, '[ProjectDispatch] recovery left assignment=regenerate-only owner=1 '
            'unresumed: restart_paused_turn_offers_no_continue') in lines


def test_a_restart_paused_lane_with_a_call_cut_off_mid_effect_waits_for_review(caplog):
    """A Worker Turn whose settlement names a call the restart cut off mid-effect is
    left for its Coordinator to check, never continued automatically; every other
    restart interruption still resumes by itself."""
    effects = [{'toolName': 'run_command', 'toolCallId': 'call-write', 'operationId': 'op-1'}]

    class Repository(_Repository):
        def restart_resume(self, assignment, *, attempt=None):
            resume = super().restart_resume(assignment)
            return None if resume is None else {**resume, 'option': None, 'review': effects}

    with caplog.at_level(logging.DEBUG, logger='lib.project_dispatch.recovery'):
        stats = recovery.recover_project_dispatch(
            1, '/project', repository=Repository([_assignment('cut-off')], {'cut-off'}),
            service=object(), was_aborted_after=lambda *a: False, clock=lambda: NOW)
    assert stats['skipped'] == 1 and stats['eligible'] == 0 and stats['failed'] == 0
    assert (logging.WARNING, '[ProjectDispatch] recovery left assignment=cut-off owner=1 '
            'unresumed: restart_paused_turn_effect_unknown') in _lines(caplog)


def test_a_restart_paused_lane_its_own_records_follow_is_resumed(caplog):
    """Records the Worker Turn appended after it (no later input) never stop it: a
    resume preserves every later Turn, so recovery continues it in place."""
    followed = {'tail': False, 'later': 1, 'supersededBy': None, 'queued': False}
    dispatched = []

    class Service:
        def dispatch_authorized(self, binding):
            dispatched.append(binding['assignmentId'])
            return {'dispatch': 'accepted'}

    class Repository(_Repository):
        def admission(self, assignment_id):
            return {'assignmentId': assignment_id}

    with caplog.at_level(logging.DEBUG, logger='lib.project_dispatch.recovery'):
        stats = recovery.recover_project_dispatch(
            1, '/project', repository=Repository([_assignment('followed')], {'followed'},
                                                 {'followed': (followed, {'operation': 'continue'})}),
            service=Service(), was_aborted_after=lambda *a: False, clock=lambda: NOW)
    assert stats['accepted'] == 1 and stats['skipped'] == 0 and dispatched == ['followed']
    assert _lines(caplog) == set()


def test_a_pass_reads_the_attempts_of_every_lane_it_decides_in_one_exchange():
    """Boot recovery read each lane's execution attempt in its own round trip."""
    rows = [{**_assignment(name), 'execution': {'turnId': 't', 'attemptId': 'attempt-' + name}}
            for name in ('one', 'two', 'three')]
    rows.append({**_assignment('lead'), 'request': {**rows[0]['request'], 'profile': {'mode': 'coordinator'}}})
    batches, seen = [], {}

    class Repository(_Repository):
        def read_attempts(self, attempt_ids):
            batches.append(list(attempt_ids))
            return {attempt_id: {'attemptId': attempt_id, 'status': 'completed'} for attempt_id in batches[-1]}

        def restart_resume(self, assignment, *, attempt=None):
            seen[assignment['id']] = attempt
            return None

    stats = recovery.recover_project_dispatch(
        1, '/project', repository=Repository(rows, set()), service=object(),
        was_aborted_after=lambda *a: False, clock=lambda: NOW)
    assert batches == [['attempt-one', 'attempt-two', 'attempt-three']]
    assert seen == {name: {'attemptId': 'attempt-' + name, 'status': 'completed'}
                    for name in ('one', 'two', 'three')}
    assert stats['skipped'] == 4 and stats['failed'] == 0


def test_an_attempt_the_batch_could_not_read_fails_only_its_own_lane():
    rows = [{**_assignment(name), 'execution': {'turnId': 't', 'attemptId': 'attempt-' + name}}
            for name in ('lost', 'kept')]

    class Repository(_Repository):
        def read_attempts(self, attempt_ids):
            return {'attempt-lost': LookupError('Attempt not found'),
                    'attempt-kept': {'attemptId': 'attempt-kept', 'status': 'completed'}}

        def restart_resume(self, assignment, *, attempt=None):
            assert attempt is not None
            return None

    stats = recovery.recover_project_dispatch(
        1, '/project', repository=Repository(rows, set()), service=object(),
        was_aborted_after=lambda *a: False, clock=lambda: NOW)
    assert stats['failed'] == 1 and stats['skipped'] == 1
