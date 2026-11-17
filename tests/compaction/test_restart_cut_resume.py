"""A restart-cut batch the model simply issues again (docs/RUN_CONTINUITY.md).

When a restart (or a storage fence) cuts a Turn's last tool batch and every call
in it is harmless to run twice, the resume drops that batch from both the
suspended working view and the replayed rounds. The resumed model's first
request then repeats the interrupted one exactly: it reads no restart wording
and the provider cache still holds the whole prefix.
"""
from copy import deepcopy

import pytest

from tests.compaction.test_dispatch_context_recovery import _round, _task
from tests.compaction.test_suspended_working_view import _carrier, _interrupted, archives  # noqa: F401

pytestmark = pytest.mark.unit


def _cut_poll(index):
    return {'toolName': 'poll', 'toolCallId': f'c{index}', 'llmRound': index,
            'toolArgs': {'ids': ['bg_1']}, 'status': 'aborted', '_restartCut': True,
            'toolContent': 'Wait interrupted by a server restart: this wait did not finish.'}


def _restart_cut_task(abort_reason='server_shutdown'):
    from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
    settled, cut = [_round(0), _round(1)], _cut_poll(2)
    boundary = ([{'role': 'user', 'content': 'work'}, _carrier('memories')]
                + _reconstruct_tool_call_messages(settled))
    task = _interrupted(_task(), settled + [cut],
                        boundary + _reconstruct_tool_call_messages([cut]))
    task['_abort_reason'] = abort_reason
    return task, settled, cut, boundary


def test_the_saved_view_ends_at_the_request_that_issued_the_cut_batch(archives):  # noqa: F811
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    task, settled, _cut, boundary = _restart_cut_task()
    preserve_suspended_working_view(task)
    row = archives.rows['archive-1']
    assert row['post'] == boundary
    assert row['receipt']['resumeWitness']['toolRoundCount'] == len(settled)


def test_the_resume_restores_exactly_that_view_without_the_cut_receipts(archives):  # noqa: F811
    from lib.conversation_sync.command_service import (
        RESTART_REPEATED_CALLS_KEY, _without_repeatable_restart_cut)
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_working_view, restore_compacted_resume, resume_background_digest,
        resume_execution_digest)
    task, settled, cut, boundary = _restart_cut_task()
    preserve_suspended_working_view(task)
    config = deepcopy(task['config'])
    turn = {'turnId': 'turn', 'settlement': {'cause': 'server_restart'}}
    projection = _without_repeatable_restart_cut(
        'continue', turn, {'toolRounds': settled + [cut]}, config)
    assert projection['toolRounds'] == settled
    assert config[RESTART_REPEATED_CALLS_KEY] == [cut['toolCallId']]
    config['_resumeBackgroundDigest'] = resume_background_digest([])
    config['_resumeExecutionDigest'] = resume_execution_digest('', '')
    resumed = _task()
    resumed['_attemptId'] = 'continue-attempt'
    restored = restore_compacted_resume(resumed, config, projection['toolRounds'],
                                        fresh_messages=[])
    assert restored == boundary
    assert 'server restart' not in repr(restored)


def test_a_persons_stop_keeps_the_batch_and_its_receipts(archives):  # noqa: F811
    from lib.conversation_sync.command_service import _without_repeatable_restart_cut
    from lib.tasks_pkg.compaction._resume_projection import preserve_suspended_working_view
    task, settled, cut, boundary = _restart_cut_task('user_abort')
    preserve_suspended_working_view(task)
    assert archives.rows['archive-1']['post'] == task['messages']
    projection = {'toolRounds': settled + [cut]}
    stopped = {'turnId': 'turn', 'settlement': {'cause': 'user_abort'}}
    assert _without_repeatable_restart_cut('continue', stopped, projection, {}) is projection
    restarted = {'turnId': 'turn', 'settlement': {'cause': 'server_restart'}}
    assert _without_repeatable_restart_cut('regenerate', restarted, projection, {}) is projection


def test_an_unrepeatable_cut_batch_keeps_its_receipts(archives):  # noqa: F811
    from lib.conversation_sync.command_service import _without_repeatable_restart_cut
    write = {'toolName': 'write_file', 'toolCallId': 'w', 'llmRound': 2, 'toolArgs': {'path': 'a'},
             'toolContent': 'interrupted', '_restartCut': True,
             'executionEffect': {'state': 'entered', 'recovery': 'reconcile', 'operationId': 'op'}}
    projection = {'toolRounds': [_round(0), _cut_poll(2), write]}
    turn = {'turnId': 'turn', 'settlement': {'cause': 'server_restart'}}
    assert _without_repeatable_restart_cut('continue', turn, projection, {}) is projection


def test_the_view_binds_the_turn_text_the_resume_reads(archives):  # noqa: F811
    """A lost event delivery leaves the Turn text behind the live task's; the view stays usable."""
    from lib.tasks_pkg.compaction._resume_projection import (
        preserve_suspended_working_view, resume_execution_digest)
    task = _interrupted(_task(), [_round(0)], None)
    task['content'] = 'live text the Turn never recorded'
    preserve_suspended_working_view(task, turn_text=lambda _task: ('recorded', ''))
    witness = archives.rows['archive-1']['receipt']['resumeWitness']
    assert witness['executionDigest'] == resume_execution_digest('recorded', '')


def test_the_interrupted_effect_note_skips_calls_the_model_issues_again():
    from lib.tasks_pkg.orchestrator._resume_state import append_interrupted_effect_note
    call = {'tool_name': 'poll', 'tool_ordinal': 3, 'tool_call_id': 'c2', 'operation_id': 'op'}
    task = {'_interruptedToolEffects': [call], 'config': {'_restartRepeatedCallIds': ['c2']}}
    messages = [{'role': 'user', 'content': 'work'}]
    append_interrupted_effect_note(task, messages)
    assert messages == [{'role': 'user', 'content': 'work'}]


@pytest.mark.parametrize('abort_reason, marked', [
    ('server_shutdown', True), ('worker_lease_lost', True), ('user_abort', False), (None, False)])
def test_a_result_settled_after_a_restart_or_fence_stop_is_marked_cut(monkeypatch, abort_reason, marked):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    from lib.tools.restart_effect_class import RESTART_CUT_FIELD
    monkeypatch.setattr(pipeline, 'append_event', lambda *a, **kw: None)
    row = {'toolCallId': 'c1', 'toolName': 'poll', 'toolArgs': '{}', 'llmRound': 1, 'roundNum': 1,
           'status': 'executing', 'taskId': 't'}
    task = {'id': 't', '_userId': 1, 'model': 'kimi-k2.5', 'config': {}, 'toolRounds': [row]}
    if abort_reason:
        task.update(aborted=True, _abort_reason=abort_reason)
    pipeline._settle_tool_result(task, 'poll', 'c1', {}, 1, row, 'wait ended', idempotent_tools=frozenset(),
                                 cache={}, tid='t', round_num=1, settled_results={})
    assert (row.get(RESTART_CUT_FIELD) is True) is marked
