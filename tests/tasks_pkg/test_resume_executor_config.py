"""Executor resume authorities derived from a settled projection.

Every resume replays the retained checkpoint rounds (retention keeps pre-gap
display carriers, replay stays the causal prefix) and regenerates from the
last complete message. No operation carries the interrupted prose or
reasoning as a prefill or display seed: no provider continues a bare trailing
assistant message, so a seed would show text the regenerated reply repeats.
"""

from __future__ import annotations

import pytest

from lib.conversation_sync.command_service import (
    _journal_resume_file_fields,
    _resume_executor_config,
)
from lib.tool_results.tool_round_replay import scan_replayable_tool_round_prefix

pytestmark = pytest.mark.unit
_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/foo.py'}


@pytest.mark.parametrize('operation', ['continue', 'checkpoint_resume', 'answer_guidance'])
def test_resume_preserves_unknown_usage_independently_of_paid_counters(operation):
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state
    projection = {'usage': {'total_tokens': 5}, 'usageComplete': False}
    if operation == 'answer_guidance':
        projection['toolRounds'] = [_awaiting_human('call-ask', 'hg-1')]
    config = _resume_executor_config(operation, projection, {
        'guidanceId': 'hg-1', 'toolCallId': 'call-ask', 'response': 'proceed'})
    prepared = prepare_resume_state(config)
    assert prepared.checkpoint_usage == {'total_tokens': 5}
    assert prepared.checkpoint_usage_complete is False


@pytest.mark.parametrize('value', [0, 1, 'false', [], {}])
def test_resume_rejects_non_boolean_usage_completeness(value):
    from lib.tasks_pkg.orchestrator._resume_state import (
        ContinueResumeStateProtocolError, prepare_resume_state)
    with pytest.raises(ContinueResumeStateProtocolError, match='checkpointUsageComplete'):
        prepare_resume_state({'checkpointUsageComplete': value})


def test_resume_complete_claim_never_clears_existing_unknown_usage():
    import threading
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
    task = {'id': 'task-1', 'convId': 'conv-1', 'content': '', 'thinking': '',
            'content_lock': threading.Lock(), '_executionActualsComplete': False}
    apply_resume_state(task=task, cfg={'checkpointUsageComplete': True},
                       messages=[], tid='task-1')
    assert task['_executionActualsComplete'] is False


def _completed(call_id: str) -> dict:
    return {
        'toolCallId': call_id,
        'toolName': 'run_command',
        'toolArgs': {'command': 'pwd'},
        'toolContent': '/repo',
        'status': 'done',
    }


_DISPLAY_ROW = {
    # Program-shell/progress carrier: no toolCallId, never dispatched.
    'toolName': 'execute_tools',
    'toolArgs': {'calls': []},
    'status': 'done',
}

_IN_FLIGHT = {
    # Interrupted mid-dispatch: identity assigned, result never arrived.
    'toolCallId': 'call-tail',
    'toolName': 'run_command',
    'toolArgs': {'command': 'sleep 60'},
    'toolContent': None,
    'status': 'running',
}


def test_continue_replays_completed_rounds_and_display_carriers() -> None:
    config = _resume_executor_config('continue', {
        'content': 'partial answer',
        'images': [],
        'toolRounds': [dict(_DISPLAY_ROW), _completed('call-1'),
                       dict(_IN_FLIGHT)],
    })

    assert not _SEED_KEYS & set(config)
    retained = config['checkpointToolRounds']
    # The result-less in-flight tail is amputated; the display carrier and
    # the completed execution receipt survive.
    assert [item.get('toolCallId') for item in retained] == [None, 'call-1']


def test_continue_without_tool_rounds_omits_checkpoint() -> None:
    config = _resume_executor_config('continue', {'content': 'tail'})

    assert 'checkpointToolRounds' not in config
    assert not _SEED_KEYS & set(config)
    assert config['checkpointImages'] == []


_SEED_KEYS = {'resumePrefill', 'contentPrefix', 'thinkingPrefix'}


@pytest.mark.parametrize('operation', ['continue', 'checkpoint_resume', 'answer_guidance'])
def test_resume_never_carries_the_interrupted_tail(operation) -> None:
    config = _resume_executor_config(operation, {
        'content': 'partial answer',
        'thinking': 'interrupted reasoning',
        'toolRounds': [_completed('call-1'), _awaiting_human('call-ask', 'hg-1')],
    }, {'guidanceId': 'hg-1', 'toolCallId': 'call-ask', 'response': 'go'})

    assert not _SEED_KEYS & set(config)


def test_resumed_wire_ends_at_the_replayed_checkpoint() -> None:
    """The resumed request is the interrupted request's complete prefix: the
    replayed rounds end on a tool result, with no trailing assistant turn and
    no reasoning seed for a regenerated tail to repeat."""
    import threading

    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state

    config = _resume_executor_config('continue', {
        'content': '',
        'thinking': 'interrupted reasoning',
        'toolRounds': [_completed('call-1'), dict(_IN_FLIGHT)],
    })
    task = {
        'id': 'task-1', 'convId': 'conv-1',
        'content': '', 'thinking': '',
        'content_lock': threading.Lock(),
    }
    base = [{'role': 'user', 'content': 'inspect the repo'}]
    messages = [dict(message) for message in base]
    apply_resume_state(task=task, cfg=config, messages=messages, tid='task-1')

    assert messages[:len(base)] == base
    assert [message['role'] for message in messages[len(base):]] == [
        'assistant', 'tool']
    assert messages[-1]['tool_call_id'] == 'call-1'
    assert task['content'] == ''
    assert task['thinking'] == ''


def test_continue_with_only_an_in_flight_round_omits_checkpoint() -> None:
    config = _resume_executor_config('continue', {
        'content': '',
        'toolRounds': [dict(_IN_FLIGHT)],
    })

    assert 'checkpointToolRounds' not in config


def test_checkpoint_resume_passes_retained_projection_verbatim() -> None:
    rounds = [dict(_DISPLAY_ROW), _completed('call-1')]
    config = _resume_executor_config('checkpoint_resume', {
        'content': 'partial',
        'toolRounds': rounds,
    })

    assert config['checkpointToolRounds'] == rounds
    assert not _SEED_KEYS & set(config)


def _awaiting_human(call_id: str, guidance_id: str) -> dict:
    return {
        'toolCallId': call_id,
        'toolName': 'ask_human',
        'toolArgs': {'question': 'Which scope?'},
        'toolContent': None,
        'status': 'awaiting_human',
        'guidanceId': guidance_id,
        'guidanceQuestion': 'Which scope?',
        'guidanceType': 'free_text',
    }


def test_answer_guidance_completes_the_interrupted_question_round() -> None:
    config = _resume_executor_config('answer_guidance', {
        'content': 'partial answer',
        'images': [],
        'toolRounds': [dict(_DISPLAY_ROW), _completed('call-1'),
                       _awaiting_human('call-ask', 'hg-1')],
    }, {
        'guidanceId': 'hg-1',
        'toolCallId': 'call-ask',
        'response': '仅服务商侧改造',
    })

    assert not _SEED_KEYS & set(config)
    rounds = config['checkpointToolRounds']
    assert [item.get('toolCallId') for item in rounds] == [
        None, 'call-1', 'call-ask',
    ]
    answered = rounds[-1]
    assert answered['status'] == 'done'
    assert answered['toolContent'] == 'Human response: 仅服务商侧改造'
    meta = answered['results'][0]
    assert meta['source'] == 'HumanGuidance'
    assert meta['badge'] == 'answered'
    assert meta['userResponse'] == '仅服务商侧改造'
    assert meta['guidanceId'] == 'hg-1'
    assert meta['responseType'] == 'free_text'
    # prepare_resume_state rejects any causal gap in checkpointToolRounds:
    # the synthesized list must scan clean end to end.
    prefix = scan_replayable_tool_round_prefix(rounds)
    assert prefix.blocked_position is None
    assert len(prefix.rounds) == 2


def test_answer_guidance_rejects_mismatched_or_missing_answer() -> None:
    projection = {'toolRounds': [_awaiting_human('call-ask', 'hg-1')]}
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', projection, None)
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', projection, {
            'guidanceId': 'hg-other', 'response': 'x',
        })
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', projection, {
            'guidanceId': 'hg-1', 'response': '',
        })
    # No interrupted question: a completed projection cannot be answered.
    with pytest.raises(ValueError):
        _resume_executor_config('answer_guidance', {
            'toolRounds': [_completed('call-1')],
        }, {'guidanceId': 'hg-1', 'response': 'x'})


def test_resume_ops_carry_the_settled_modified_file_ledger() -> None:
    files = [
        {'path': 'JOURNAL.md', 'action': 'patched'},
        {'path': 'lib/tool_results/tool_round_replay.py', 'action': 'written', 'root': 'chatui'},
    ]
    projection = {
        'content': 'partial',
        'toolRounds': [_completed('call-1')],
        'modifiedFiles': 2,
        'modifiedFileList': files,
    }
    for operation in ('continue', 'checkpoint_resume'):
        config = _resume_executor_config(operation, projection)
        assert config['checkpointModifiedFileList'] == files
        assert config['checkpointModifiedFiles'] == 2

    answered = _resume_executor_config('answer_guidance', {
        'content': 'partial',
        'toolRounds': [_awaiting_human('call-ask', 'hg-1')],
        'modifiedFiles': 2,
        'modifiedFileList': files,
    }, {'guidanceId': 'hg-1', 'response': '继续'})
    assert answered['checkpointModifiedFileList'] == files
    assert answered['checkpointModifiedFiles'] == 2


def test_resume_file_ledger_count_defaults_to_list_length() -> None:
    config = _resume_executor_config('continue', {
        'content': 'x',
        'modifiedFileList': [{'path': 'a.py', 'action': 'patched'}],
    })

    assert config['checkpointModifiedFiles'] == 1


def test_resume_without_file_ledger_omits_checkpoint_keys() -> None:
    for projection in ({'content': 'x'},
                       {'content': 'x', 'modifiedFileList': []},
                       {'content': 'x',
                        'modifiedFileList': [{}, '', None]}):
        config = _resume_executor_config('continue', projection)
        assert 'checkpointModifiedFileList' not in config
        assert 'checkpointModifiedFiles' not in config


def test_carried_file_ledger_passes_resume_state_validation() -> None:
    from lib.tasks_pkg.orchestrator._resume_state import (
        prepare_resume_state)

    files = [{'path': 'JOURNAL.md', 'action': 'patched'}]
    config = _resume_executor_config('continue', {
        'content': 'partial',
        'modifiedFiles': 1,
        'modifiedFileList': files,
    })

    prepared = prepare_resume_state(config)
    assert list(prepared.checkpoint_modified_file_list) == files
    assert prepared.checkpoint_modified_files == 1


def test_incomplete_file_ledger_survives_continue_without_restoring_undo():
    from lib.tasks_pkg.orchestrator._resume_state import prepare_resume_state
    files = [{'path': 'main.tex', 'action': 'written', 'rootPath': 'remote:device:paper'}]
    config = _resume_executor_config('continue', {
        'modifiedFileList': files, 'modifiedFiles': 1, 'modifiedFilesTruncated': True,
    })
    assert config['checkpointModifiedFilesTruncated'] is True
    prepared = prepare_resume_state(config)
    assert prepared.checkpoint_modified_files_truncated is True
    assert list(prepared.checkpoint_modified_file_list) == files


def test_journal_fallback_rebuilds_the_ledger_after_restart(
        monkeypatch) -> None:
    import lib.tasks_pkg.commit_round._derive as derive_mod
    captured = {}

    def fake_derive(task, project_path, project_paths, **kwargs):
        captured['recovery'] = kwargs.get('recover_conversation_journal')
        captured['task'] = task
        captured['project_path'] = project_path
        captured['project_paths'] = project_paths
        return ([{'path': 'lib/foo.py', 'action': 'patched'}], 1, True)

    monkeypatch.setattr(
        derive_mod, 'derive_round_modified_files', fake_derive)
    turn = {'createdAt': 1788137746471, 'conversationId': 'conv-1'}
    config = {'projectPath': '/repo', 'projectPaths': ['/repo', '/extra']}

    out = _journal_resume_file_fields(turn, config)

    assert out == {
        'checkpointModifiedFileList': [
            {'path': 'lib/foo.py', 'action': 'patched'}],
        'checkpointModifiedFiles': 1,
    }
    # Empty task id: no taskId-stamped row matches, so the conv-scoped
    # timestamp scan covers every attempt of the turn in one pass.
    assert captured['task']['id'] == ''

    assert captured['recovery'] is True
    assert captured['task']['convId'] == 'conv-1'
    # Turn timestamps are epoch ms; journal rows are time.time() seconds.
    assert captured['task']['created_at'] == pytest.approx(1788137746.471)
    assert captured['project_path'] == '/repo'
    assert captured['project_paths'] == ['/repo', '/extra']


def test_journal_fallback_is_silent_without_floor_rows_or_project(
        monkeypatch) -> None:
    import lib.tasks_pkg.commit_round._derive as derive_mod
    monkeypatch.setattr(
        derive_mod, 'derive_round_modified_files',
        lambda task, project_path, project_paths, **kwargs: ([], 0, False))
    turn = {'createdAt': 1788137746471, 'conversationId': 'conv-1'}

    assert _journal_resume_file_fields(turn, {'projectPath': '/repo'}) == {}
    assert _journal_resume_file_fields(
        {'conversationId': 'conv-1'}, {'projectPath': '/repo'}) == {}
    assert _journal_resume_file_fields(turn, {}) == {}

def test_other_operations_get_no_resume_authorities() -> None:
    assert _resume_executor_config('regenerate', {
        'content': 'x',
        'toolRounds': [_completed('call-1')],
    }) == {}


# WORKSPACE-ADMISSION-R2 (2026-11-11): batch R178 announced edit_file and poll
# in one provider response. Its native content rides on the first row only;
# the poll's row stayed result-less (storage outage, then restart) and every
# resume amputated it as the causal gap, so the record kept a tool_use whose
# result no resume could ever send (HTTP 400 on every option).
_NATIVE_EDIT_AND_POLL = [
    {'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'},
    {'type': 'tool_use', 'id': 'toolu_edit', 'name': 'edit_file',
     'input': {'path': 'a.py', 'edits': []}},
    {'type': 'tool_use', 'id': 'toolu_poll', 'name': 'poll',
     'input': {'id': 'bg_1', 'wait_s': 900}},
]


def _batch_row(call_id, name, *, llm_round, attempt='attempt-3', content='ok',
               status='done', native=None):
    row = {'toolCallId': call_id, 'toolName': name, 'toolArgs': '{}',
           'toolContent': content, 'status': status, 'llmRound': llm_round,
           'attemptId': attempt, 'source': 'native_direct'}
    if native is not None:
        row['_anthropicContentBlocks'] = native
    return row


def _workspace_admission_turn(*, poll_row: bool) -> list[dict]:
    """Earlier attempts, a rejected project-message call, then the lost poll."""
    rows = [_batch_row('toolu_a1', 'read_files', llm_round=0, attempt='attempt-1'),
            _batch_row('toolu_a2', 'run_command', llm_round=1, attempt='attempt-2'),
            _batch_row('toolu_send', 'send_project_message', llm_round=154),
            _batch_row('toolu_consider', 'consider_project_message', llm_round=154,
                       status='rejected', content='Invalid type at $.expected_revision'),
            _batch_row('toolu_edit', 'edit_file', llm_round=177, content='edited',
                       native=_NATIVE_EDIT_AND_POLL)]
    if poll_row:
        rows.append(_batch_row('toolu_poll', 'poll', llm_round=177,
                               content=None, status='running'))
    return rows


@pytest.mark.parametrize('operation,poll_row', [
    ('continue', True),            # the automatic continue after the restart
    ('checkpoint_resume', False),  # every later resume: the poll row is gone
])
def test_resume_records_a_typed_result_for_each_call_its_batch_announced(
        operation, poll_row) -> None:
    from lib.tool_results.tool_round_replay import (
        UNRECORDED_TOOL_RESULT_FIELD, UNRECORDED_TOOL_RESULT_TEXT)
    config = _resume_executor_config(operation, {
        'content': '', 'toolRounds': _workspace_admission_turn(poll_row=poll_row)})

    rows = config['checkpointToolRounds']
    assert [row['toolCallId'] for row in rows] == [
        'toolu_a1', 'toolu_a2', 'toolu_send', 'toolu_consider', 'toolu_edit', 'toolu_poll']
    closure = rows[-1]
    assert closure['toolContent'] == UNRECORDED_TOOL_RESULT_TEXT
    assert closure[UNRECORDED_TOOL_RESULT_FIELD] is True
    assert closure['status'] == 'aborted'
    # Same provider batch as the row that announced it.
    assert (closure['llmRound'], closure['attemptId']) == (177, 'attempt-3')
    assert scan_replayable_tool_round_prefix(rows).blocked_position is None


def test_checkpoint_resume_of_the_lost_sibling_shape_sends_a_valid_request() -> None:
    """Offline end-to-end: settled projection -> checkpoint_resume executor
    authority -> resumed messages -> Claude request. Every tool_use is answered
    and the batch's native content is replayed unchanged."""
    import threading

    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.llm.body import build_body
    from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state

    config = _resume_executor_config('checkpoint_resume', {
        'content': '', 'toolRounds': _workspace_admission_turn(poll_row=False)})
    task = {'id': 'task-4', 'convId': 'conv-1', 'content': '', 'thinking': '',
            'content_lock': threading.Lock()}
    messages = [{'role': 'user', 'content': 'carry the admission fix'}]
    apply_resume_state(task=task, cfg=config, messages=messages, tid='task-4')
    tools = [{'type': 'function', 'function': {
        'name': name, 'parameters': {'type': 'object', 'properties': {}}}}
        for name in ('read_files', 'run_command', 'send_project_message',
                     'consider_project_message', 'edit_file', 'poll')]
    body = build_body('kunlunbedrock.claude-opus-5.5-m17', messages,
                      max_tokens=1000, context_window=128000,
                      precomputed_input_tokens=100, tools=tools)
    wire = openai_body_to_anthropic(body)['messages']

    unanswered = []
    for index, message in enumerate(wire):
        if message['role'] != 'assistant' or not isinstance(message['content'], list):
            continue
        following = wire[index + 1]['content'] if index + 1 < len(wire) else []
        answered = {block.get('tool_use_id') for block in following
                    if isinstance(block, dict) and block.get('type') == 'tool_result'}
        unanswered += [block['id'] for block in message['content']
                       if block['type'] == 'tool_use' and block['id'] not in answered]
    assert unanswered == []
    assert _NATIVE_EDIT_AND_POLL in [message['content'] for message in wire]
    assert wire[-1]['role'] == 'user'
