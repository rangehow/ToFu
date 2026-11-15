"""Restart and Stop inside a Worker call's dispatch admission.

Incident 2026-11-13 04:28 (RESTART-CHECK): the restart's quiesce stamped
``server_shutdown`` while four Worker calls were inside dispatch tool-intent
admission. Each resumed attempt read "do not resume" in its stored tool result
and stopped, although none of the calls had started. ba37ee75e7 names the
restart in the covering admission and rewords the envelope. These specs drive
the real shutdown quiesce and the real owner Stop inside a call's admission, at
both live validations of its tool intent.

Nothing starts, no intent stays open, the conversation is recorded for resume,
the call that never started is stored repeatable, and a continuation admits the
repeated call without a fence and runs it exactly once. An owner Stop keeps its
cancellation and is not repeated. A call whose intent may still be open (its
not_entered settlement was lost) is never stored repeatable. The refusal names
its stop with a typed cause, and each identity fact refuses on its own.
"""
import pytest

from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission
from lib.tasks_pkg.executor._effects import tool_exception_result
from lib.tools.result_envelope import tool_result_error
from tests.project_mod.test_coordinator_workers import (
    _restart_continuation, _worker_task, base_runtime, context, runtime,
)
from tests.project_mod.test_project_dispatch_effect_fence import DOCUMENTS, ledger, lose_completions

__all__ = ('base_runtime', 'context', 'runtime')
pytestmark = pytest.mark.unit

# The live validation the call's tool intent runs before (pre_intent) and after
# (effect_fence) its intent is durable; the incident's calls met both.
WINDOWS = {'pre_intent': 1, 'effect_fence': 2}


@pytest.fixture
def worker(runtime, monkeypatch):
    """A bound Worker registered in a private chat runtime, so the real shutdown
    quiesce and owner Stop act on it alone, plus a handler counting real entries."""
    from lib.tasks_pkg.manager import reopen_task_admission
    yield _registered_worker(runtime, monkeypatch)
    reopen_task_admission()


@pytest.fixture
def reserving_worker(runtime, monkeypatch):
    """The same Worker under a human request's shared budget: each call first writes
    its tool reservation, before its intent (the drill's Workers had one)."""
    from lib.tasks_pkg.manager import reopen_task_admission
    from tests.project_mod.test_coordinator_request_budget import bind
    bind(runtime)
    yield _registered_worker(runtime, monkeypatch)
    reopen_task_admission()


def _registered_worker(runtime, monkeypatch):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.tasks_pkg.executor import _execute as executor
    from lib.tasks_pkg.manager import _registry
    task, row = _worker_task(runtime)
    task['_toolContractDocumentsByName'] = DOCUMENTS
    tasks = TaskRuntime('chat')
    assert tasks.adopt(task)
    monkeypatch.setattr(_registry, 'chat_task_runtime', tasks)
    paused = []
    monkeypatch.setattr('lib.conversations.restart_resume.record_quiesced_conversations',
                        lambda quiesced: paused.extend(item['id'] for item in quiesced))
    entered = []

    def handler(task_, tc, name, call_id, *rest):
        entered.append((task_['_attemptId'], name, call_id))
        return call_id, 'ok', False
    monkeypatch.setattr(executor, '_dispatch_admitted_tool_one', handler)
    return runtime, task, row, tasks, paused, entered


def _quiesce():
    from lib.tasks_pkg.manager import quiesce_running_tasks
    quiesce_running_tasks(reason='server_shutdown')


def _owner_stop(tasks, task):
    return lambda: tasks.abort_owned(task['id'], user_id=7, source='user')


def _stop_inside_admission(r, monkeypatch, window, stop):
    """Run ``stop`` while the call's live grant validation reads storage."""
    validate, seen = r.c.repo.validate, []

    def slow_validate(*args, **kwargs):
        result = validate(*args, **kwargs)
        seen.append(1)
        if len(seen) == WINDOWS[window]:
            stop()
        return result
    monkeypatch.setattr(r.c.repo, 'validate', slow_validate)


def _call(task, round_entry, *, round_num, call_id='toolu_run'):
    from lib.tasks_pkg.executor._execute import _execute_admitted_tool_one
    return _execute_admitted_tool_one(
        task, {}, 'run_command', call_id, {'command': 'make test'}, round_num,
        round_entry, {}, None, False, None)


def _refused_in_window(r, monkeypatch, task, window, stop):
    """The refusal a stop inside the call's admission raises, and the tool result stored for it."""
    round_entry = {}
    if window == 'covering':
        # The incident path in ba37ee75e7's report: the call's own admission
        # rechecks the stopped task without a storage read.
        decided = admission.admit_dispatch(task)
        stop()
        with pytest.raises(AbortedError) as refused:
            admission.validate_task(task, admission=decided)
    else:
        _stop_inside_admission(r, monkeypatch, window, stop)
        with pytest.raises(AbortedError) as refused:
            _call(task, round_entry, round_num=7)
    stored = tool_exception_result(task, 'run_command', round_entry, refused.value)
    return refused.value, stored


def _stored_call(task, round_entry, *, round_num):
    """What the pipeline stores for one call: its returned result, or its refusal projected."""
    try:
        return _call(task, round_entry, round_num=round_num)[1]
    except AbortedError as refused:
        return tool_exception_result(task, 'run_command', round_entry, refused)


def _continuation_repeats(stored):
    """A continuation that follows the stored verdict repeats only a retryable call."""
    error = tool_result_error(stored)
    return error is not None and error.retryable


def _expected_ledger(window, *later):
    first = [] if window == 'pre_intent' else [('run_command', 1, 'not_entered')]
    return first + list(later)


# Coverage ----------------------------------------------------------------------

@pytest.mark.parametrize('window', WINDOWS)
def test_a_restart_quiesce_in_admission_starts_nothing_and_a_continuation_runs_the_call_once(
        worker, monkeypatch, window):
    r, task, row, _tasks, paused, entered = worker
    _refusal, stored = _refused_in_window(r, monkeypatch, task, window, _quiesce)

    assert task['_abort_reason'] == 'server_shutdown' and paused == [task['id']]
    assert entered == [] and ledger(r, row) == _expected_ledger(window)
    error = tool_result_error(stored)
    assert 'do not resume' not in error.next_action.lower()
    assert 'not a revoked authorization' in error.next_action

    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    resumed['_toolContractDocumentsByName'] = DOCUMENTS
    assert resumed['_interruptedToolEffects'] == []
    assert _call(resumed, {}, round_num=8) == ('toolu_run', 'ok', False)
    assert entered == [('resumed-attempt', 'run_command', 'toolu_run')]
    ordinal = 1 if window == 'pre_intent' else 2
    assert ledger(r, row) == _expected_ledger(window, ('run_command', ordinal, 'executor_returned'))
    assert not resumed.get('aborted')


@pytest.mark.parametrize('window', WINDOWS)
def test_an_owner_stop_in_the_same_window_keeps_its_cancellation_and_is_not_repeated(
        worker, monkeypatch, window):
    r, task, row, tasks, paused, entered = worker
    _refusal, stored = _refused_in_window(r, monkeypatch, task, window, _owner_stop(tasks, task))

    assert not task.get('_abort_reason') and paused == []
    error = tool_result_error(stored)
    assert error.code == 'tool_execution_cancelled' and error.retryable is False
    assert 'Respect the cancellation' in error.next_action
    # The owner's later Continue reads the same verdict: the call is not repeated.
    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    assert not _continuation_repeats(stored)
    assert entered == [] and ledger(r, row) == _expected_ledger(window)


@pytest.mark.parametrize('window', [*WINDOWS, 'covering'])
def test_a_restart_cut_call_that_never_started_is_stored_repeatable(worker, monkeypatch, window):
    r, task, _row, _tasks, _paused, entered = worker
    _refusal, stored = _refused_in_window(r, monkeypatch, task, window, _quiesce)
    error = tool_result_error(stored)
    assert entered == []
    assert error.code == 'tool_execution_interrupted' and error.retryable is True
    assert 'Repeat the call' in error.next_action and 'never started' in error.message
    assert _continuation_repeats(stored)


def test_a_restart_cut_call_whose_intent_stays_open_is_never_repeatable(worker, monkeypatch):
    """The intent is durable but its not_entered settlement is lost: the call may not repeat.

    A continuation settles the open intent as an unknown effect and names it, so the
    model inspects the operation instead of repeating it on the stored verdict.
    """
    r, task, row, _tasks, _paused, entered = worker
    restore = []

    def quiesce_and_lose_the_settlement():
        _quiesce()
        restore.append(lose_completions(r, monkeypatch))
    _refusal, stored = _refused_in_window(
        r, monkeypatch, task, 'effect_fence', quiesce_and_lose_the_settlement)
    restore[0]()

    assert entered == [] and ledger(r, row) == [('run_command', 1, 'intent')]
    error = tool_result_error(stored)
    assert error.code == 'tool_execution_cancelled' and error.retryable is False
    assert not _continuation_repeats(stored)
    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    assert ledger(r, row) == [('run_command', 1, 'interrupted_effect_unknown')]
    [settled] = resumed['_interruptedToolEffects']
    assert (settled['tool_name'], settled['tool_call_id']) == ('run_command', 'toolu_run')


def test_a_restart_that_also_stops_storage_before_the_intent_keeps_the_call_repeatable(
        worker, monkeypatch):
    """Drill admission-cut, window A (RESTART-DRILL-1 on 1e678963a4, 2026-11-13 09:06).

    Storage was frozen: the quiesce landed while the call's live grant read was
    blocked, so that read itself refused ("Agent stopped while waiting for
    persistence") and no intent was written. The budget still observed
    not_entered; with storage stopping that write failed and marked the whole
    account incomplete, so the call that never started was stored as a
    non-retryable cancellation and the resumed Worker lost its work. A
    not_entered for an intent never written has nothing to settle.
    """
    r, task, row, _tasks, paused, entered = worker
    live_read = r.c.repo.validate

    def blocked_live_read(*args, **kwargs):
        _quiesce()  # the stop lands during the blocked read; storage is stopping too
        restore.append(lose_completions(r, monkeypatch))
        raise AbortedError('Agent stopped while waiting for persistence')
    restore = []
    monkeypatch.setattr(r.c.repo, 'validate', blocked_live_read)
    round_entry = {}

    with pytest.raises(AbortedError) as refused:
        _call(task, round_entry, round_num=7)

    assert entered == [] and paused == [task['id']]
    assert task.get('_executionActualsComplete') is not False
    stored = tool_exception_result(task, 'run_command', round_entry, refused.value)
    error = tool_result_error(stored)
    assert error.code == 'tool_execution_interrupted' and error.retryable is True
    assert 'never started' in error.message and _continuation_repeats(stored)

    restore[0]()  # storage is back for the next generation
    monkeypatch.setattr(r.c.repo, 'validate', live_read)
    assert ledger(r, row) == []
    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    resumed['_toolContractDocumentsByName'] = DOCUMENTS
    assert resumed['_interruptedToolEffects'] == []
    assert _call(resumed, {}, round_num=8) == ('toolu_run', 'ok', False)
    assert entered == [('resumed-attempt', 'run_command', 'toolu_run')]
    assert ledger(r, row) == [('run_command', 1, 'executor_returned')]


def test_a_restart_cut_tool_reservation_leaves_the_call_unstarted_and_repeatable(
        reserving_worker, monkeypatch):
    """Drill admission-cut, window R (RESTART-DRILL-1 on 240ca7ccf3 and 7f63bc795b, 2026-11-13).

    The quiesce landed while the call's tool reservation write waited for storage,
    before its intent: the write refused ("Agent stopped while waiting for
    persistence") and the budget marked the account's actuals incomplete, so the
    call that never started was stored as a non-retryable cancellation and the
    resumed Worker lost its work (0 starts, no ledger row).
    """
    r, task, row, _tasks, paused, entered = reserving_worker
    wait = admission.wait_for_dispatch

    def cut_reservation(task_, operation, *, label, **kwargs):
        if label == 'project dispatch tool reservation':
            _quiesce()  # the stop lands while the reservation write waits for storage
            raise AbortedError('Agent stopped while waiting for persistence')
        return wait(task_, operation, label=label, **kwargs)
    monkeypatch.setattr(admission, 'wait_for_dispatch', cut_reservation)

    stored = _stored_call(task, {}, round_num=7)

    assert entered == [] and paused == [task['id']]
    error = tool_result_error(stored)
    assert error.code == 'tool_execution_interrupted' and error.retryable is True
    assert 'never started' in error.message and 'Agent stopped' not in stored
    assert ledger(r, row) == []
    monkeypatch.setattr(admission, 'wait_for_dispatch', wait)
    resumed = _restart_continuation(r, task, row)
    admission.bind_account(resumed)
    resumed['_toolContractDocumentsByName'] = DOCUMENTS
    assert resumed['_interruptedToolEffects'] == []
    assert _call(resumed, {}, round_num=8) == ('toolu_run', 'ok', False)
    assert entered == [('resumed-attempt', 'run_command', 'toolu_run')]
    assert ledger(r, row) == [('run_command', 1, 'executor_returned')]


def test_an_owner_stop_during_the_tool_reservation_keeps_its_cancellation(
        reserving_worker, monkeypatch):
    r, task, row, tasks, paused, entered = reserving_worker
    wait, stop = admission.wait_for_dispatch, _owner_stop(tasks, task)

    def stopped_reservation(task_, operation, *, label, **kwargs):
        if label == 'project dispatch tool reservation':
            stop()
            raise AbortedError('Agent stopped while waiting for persistence')
        return wait(task_, operation, label=label, **kwargs)
    monkeypatch.setattr(admission, 'wait_for_dispatch', stopped_reservation)

    stored = _stored_call(task, {}, round_num=7)

    assert entered == [] and paused == [] and not task.get('_abort_reason')
    error = tool_result_error(stored)
    assert error.code == 'tool_execution_cancelled' and error.retryable is False
    assert not _continuation_repeats(stored) and ledger(r, row) == []


def test_the_d8e7021b_restart_cut_never_reaches_the_model_as_lost_authority(worker, monkeypatch):
    """2026-11-13 12:31, worker-d8e7021b (TOFUDB-REOPEN-1): the R3 restart's quiesce landed
    while its run_command's live grant validation read storage. The stopping process ran
    pre-R3 code (01f24726f205), whose _validated_binding folded task['aborted'] into one
    identity check: the result harvested after the collector gap read "Project dispatch
    task identity is no longer authorized", and the resumed attempt ended its Turn ("the
    dispatch system revoked this worker's authority mid-command"). The refusal now names
    the restart, and the stored result says the call never started and may repeat.
    """
    r, task, _row, _tasks, _paused, entered = worker
    refusal, stored = _refused_in_window(r, monkeypatch, task, 'pre_intent', _quiesce)

    assert isinstance(refusal, admission.DispatchTaskInterrupted)
    assert 'no longer authorized' not in str(refusal) and 'server restart' in str(refusal)
    error = tool_result_error(stored)
    assert 'no longer authorized' not in stored
    assert error.code == 'tool_execution_interrupted' and error.retryable is True
    assert entered == []


@pytest.mark.parametrize('window', [*WINDOWS, 'covering'])
@pytest.mark.parametrize('stop,reason,named', [
    ('restart', 'server_shutdown', 'server restart'), ('owner', '', 'aborted by user')])
def test_a_stop_refusal_carries_its_cause_typed(worker, monkeypatch, window, stop, reason, named):
    r, task, _row, tasks, _paused, _entered = worker
    refusal, _stored = _refused_in_window(
        r, monkeypatch, task, window, _quiesce if stop == 'restart' else _owner_stop(tasks, task))
    assert isinstance(refusal, admission.DispatchTaskInterrupted)
    assert refusal.abort_reason == reason
    assert named in str(refusal) and 'identity' not in str(refusal)


def _change_principal(task):
    task['_principalContext'] = {**task['_principalContext'],
                                 'subject_id': 'project-dispatch:another-assignment'}


@pytest.mark.parametrize('mismatch,change,named', [
    ('principal', _change_principal, 'principal'),
    ('attended', lambda task: task.update(_attended=True), 'attended'),
    ('recipient', lambda task: task.update(convId='worker-elsewhere'), "'worker-elsewhere'"),
    ('parent_task', lambda task: task.update(_parentTaskId='parent-task'), 'child task'),
])
def test_each_identity_fact_refuses_with_its_own_typed_message(runtime, mismatch, change, named):
    task, _ = _worker_task(runtime)
    change(task)
    with pytest.raises(admission.DispatchIdentityMismatch) as refused:
        admission.validate_task(task)
    # Identity loss is still an authority stop, with its own cause.
    assert task['aborted'] is True and task['_abort_reason'] == 'dispatch_authority_lost'
    assert refused.value.mismatch == mismatch
    message = str(refused.value)
    assert message.startswith('Project dispatch task identity is no longer authorized: ')
    assert named in message


def test_a_stopped_sub_agent_carrier_reports_its_stop_not_lost_sharing(runtime):
    task, _ = _worker_task(runtime)
    carrier = {'id': 'child', 'convId': task['convId'], 'config': task['config'],
               '_principalContext': task['_principalContext'],
               '_executionBudget': task['_executionBudget'], 'status': 'running',
               'aborted': True, '_abort_reason': 'server_shutdown'}
    admission.share_dispatch_root(task, carrier)
    with pytest.raises(admission.DispatchTaskInterrupted) as refused:
        admission.dispatch_root(carrier)
    assert refused.value.abort_reason == 'server_shutdown'
    assert 'server restart' in str(refused.value)
