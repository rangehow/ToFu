"""A lead's hold is a pause: the held Turn resumes from its checkpoint and its model reads no Stop.

``control_conversation(action=hold)`` stops a Worker's exact Attempt with the
``lead_hold`` abort reason and keeps its dispatch grant
(docs/PROJECT_COORDINATION.md, "A lead holds a Worker without losing it").
"""
import pytest

from lib.turn_verdict import (
    PAUSE_ABORT_REASONS, aborted_wait_text, derive_task_verdict, stopped_tool_result,
)

pytestmark = pytest.mark.unit


def _held_task():
    return {'aborted': True, 'status': 'aborted', 'finishReason': 'aborted', '_abort_reason': 'lead_hold',
            'model': 'model', 'config': {'model': 'model'}}


def test_a_held_turn_settles_interrupted_and_offers_continue_even_before_any_progress():
    """Like a restart's quiesce, nobody stopped the work: Continue keeps the lane's later Turns."""
    from lib.turn_lifecycle import _settlement
    status, settlement = _settlement(_held_task(), {'type': 'aborted'}, {'content': '', 'toolRounds': []})
    assert (status, settlement['cause'], settlement['evidence']) == ('interrupted', 'lead_hold', 'system_recovery')
    assert [option['operation'] for option in settlement['resumeOptions']] == ['continue', 'regenerate']


def test_a_person_stop_that_produced_nothing_still_offers_only_regenerate():
    from lib.turn_lifecycle import _settlement
    task = {**_held_task(), '_abort_reason': ''}
    status, settlement = _settlement(task, {'type': 'aborted'}, {'content': '', 'toolRounds': []})
    assert (status, settlement['cause']) == ('interrupted', 'user_abort')
    assert [option['operation'] for option in settlement['resumeOptions']] == ['regenerate']


def test_the_resumed_model_reads_a_hold_never_a_stop_or_revocation():
    assert 'lead_hold' in PAUSE_ABORT_REASONS
    assert derive_task_verdict(_held_task()).cause == 'lead_hold'
    text = stopped_tool_result('lead_hold')
    assert 'aborted by user' not in text and 'grant is unchanged' in text and 'Continue the task' in text
    wait = aborted_wait_text('lead_hold')
    assert wait.startswith("Wait interrupted by the project lead's hold") and 'Continue the task' in wait


def test_a_call_the_hold_cut_before_it_started_is_repeatable():
    from lib.llm.llm_errors import AbortedError
    from lib.tasks_pkg.executor._effects import tool_exception_result
    task = {**_held_task(), '_executionActualsComplete': True}
    result = tool_exception_result(task, 'run_command', {}, AbortedError('stopped'))
    assert 'tool_execution_interrupted' in result and "The project lead's hold paused this work" in result
    assert 'do not resume' not in result
