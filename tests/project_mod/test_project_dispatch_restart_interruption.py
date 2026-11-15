"""A dispatch call cut off by a restart names the restart; recovery's successor keeps the grant.

Incident 2026-11-13, 04:27 restart: the drain (``quiesce_running_tasks(reason='server_shutdown')``)
stopped 34 running Workers. The file check inside an in-flight ``edit_file`` or ``run_command``
(``FileScope.check`` -> ``validate_task``) then refused the stopped task with "Project dispatch task
identity is no longer authorized", wrapped in "Respect the cancellation; do not resume this operation
automatically". Project-dispatch recovery continued each interrupted Turn in a successor attempt with
its grant intact, but four Workers read that result as a revoked grant and ended their Turns without
another call. A call of a task that something already stopped names who stopped it; only a real
authority failure says the grant is gone.
"""
import threading

import pytest

from lib.llm.llm_errors import AbortedError
from lib.project_dispatch import admission
from lib.tasks_pkg.executor._effects import tool_exception_result
from lib.tools.result_envelope import tool_result_error
from tests.project_mod.test_project_dispatch_repository import context as context
from tests.project_mod.test_project_dispatch_runtime import runtime as runtime

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('context', 'runtime')

pytestmark = pytest.mark.unit


def _attempt_task(trusted, task_id, attempt_id):
    config = trusted.capture({}, 7, trusted.principal, 'recipient')
    config.update(_turnOwnerUserId=7, _turnId='worker-turn', _attemptId=attempt_id)
    return {'id': task_id, 'convId': 'recipient', 'config': config, 'status': 'running',
            '_userId': 7, '_principalContext': trusted.principal.to_payload(),
            '_turnId': 'worker-turn', '_attemptId': attempt_id,
            '_attended': False, 'abort_event': threading.Event(), 'usage': {}}


def _stop(task, reason=None):
    if reason is not None:
        task['_abort_reason'] = reason
    task['aborted'] = True
    task['abort_event'].set()


def _in_flight_refusal(task, call):
    """What a gate nested in the executing call raises once ``task`` is stopped, and the result the model reads.

    ``call`` is the call's own admission (``admit_dispatch`` at call start). Its nested gates (the
    effect intent, a file scope) recheck the live task from it without a persistence wait.
    """
    with admission.admitted_call(call):
        with pytest.raises(AbortedError) as refused:
            admission.validate_task(task, admission=admission.call_admission(task))
    content = tool_exception_result(task, 'edit_file', {}, refused.value)
    return str(refused.value), tool_result_error(content)


def test_a_restart_cut_call_names_the_restart_and_the_successor_keeps_the_grant(runtime):
    r = runtime
    task = r.task()
    admission.bind_account(task)
    call = admission.admit_dispatch(task)
    _stop(task, 'server_shutdown')

    refusal, result = _in_flight_refusal(task, call)
    assert task['_abort_reason'] == 'server_shutdown'
    assert 'no longer authorized' not in refusal
    assert 'server restart' in refusal and 'grant is unchanged' in refusal
    # The call never entered its handler, so the restart left it repeatable.
    assert result.code == 'tool_execution_interrupted' and result.retryable is True
    assert 'server restart' in result.message
    assert 'do not resume' not in result.next_action.lower()
    assert 'not a revoked authorization' in result.next_action

    # Project-dispatch recovery continues the same Turn in a successor attempt.
    attempts = {
        'worker-attempt': {'attemptId': 'worker-attempt', 'turnId': 'worker-turn',
                           'conversationId': 'recipient', 'taskId': 'task-a', 'status': 'interrupted'},
        'successor-attempt': {'attemptId': 'successor-attempt', 'turnId': 'worker-turn',
                              'conversationId': 'recipient',
                              'taskId': 'conversation-attempt:successor-attempt', 'status': 'running'},
    }
    r.c.repo.attempt_get = lambda attempt_id, **kw: dict(attempts[attempt_id])
    # The restarted process validates the grant afresh; nothing from before the restart is cached.
    recovered = admission.TrustedDispatch(r.c.repo.admission(r.assignment['id']))
    recovered.continue_execution('worker-turn', 'successor-attempt')
    resumed = _attempt_task(recovered, 'conversation-attempt:successor-attempt', 'successor-attempt')
    admission.bind_account(resumed)
    binding = admission.validate_task(resumed)
    assert binding['assignmentId'] == r.assignment['id'] and not resumed.get('aborted')

    # The predecessor the successor replaced stays refused, also as a live handle.
    replaced = _attempt_task(recovered, 'task-a', 'worker-attempt')
    with pytest.raises(AbortedError, match='immutable birth association'):
        admission.validate_task(replaced)


def test_a_person_stop_keeps_its_cancellation(runtime):
    task = runtime.task()
    admission.bind_account(task)
    call = admission.admit_dispatch(task)
    _stop(task)

    refusal, result = _in_flight_refusal(task, call)
    assert 'aborted by user' in refusal and 'unchanged' not in refusal
    assert 'Respect the cancellation' in result.next_action
    assert '_abort_reason' not in task


def test_a_revoked_grant_still_stops_the_worker_and_says_so(runtime):
    r = runtime
    task = r.task()
    admission.bind_account(task)
    call = admission.admit_dispatch(task)
    r.c.repo.revoke(r.assignment['id'], 1)
    with pytest.raises(AbortedError):
        admission.validate_task(task)
    assert task['aborted'] is True and task['_abort_reason'] == 'dispatch_grant_revoked'

    refusal, result = _in_flight_refusal(task, call)
    assert 'project dispatch authority (dispatch_grant_revoked)' in refusal
    assert 'unchanged' not in refusal
    assert 'Respect the cancellation' in result.next_action
