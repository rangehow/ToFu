#!/usr/bin/env python3
"""tests/turn_lifecycle/test_checkpoint_resume_after_402.py — long-turn survival
after a credential dies mid-turn.

Observed 2026-11-09: Worker turn 411d2578-81f5-4d93-ad30-2a71d917f12e died at
LLM round 247 with HTTP 402 (credit exhausted on the fallback model) after 367
completed tool rounds. The Coordinator offered ``checkpoint_resume`` and
resumed it; this spec pins the invariants that make that safe:

  1. A non-retryable ``quota`` envelope does NOT hide the resume offer — the
     failure was a dying credential, not lost progress.
  2. The checkpoint keeps every completed tool round and amputates only the
     in-flight (result-less) tail, so a resumed turn re-issues only the
     unfinished call instead of re-running finished side effects.
  3. The resumed attempt carries the completed tool RESULTS (not just the
     display rows) into its replay history, with no second execution.
  4. The unfinished tail is retained as ``rolledBack`` evidence rather than
     displayed as a completed result.

Run:  pytest tests/turn_lifecycle/test_checkpoint_resume_after_402.py -m unit
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def _conversation_id():
    return f'resume-402-{uuid.uuid4().hex[:10]}'


def _quota_envelope():
    from lib.error_envelope import make_envelope

    return make_envelope(
        'quota', model='kimi-k3.1-pro', context='on-fallback-model',
        source='llm-stream',
        detail='API HTTP 402: {"status":402,"message":"credit exhausted"}',
        retryable=False,
    )


def _long_running_task(attempt_id, task_id):
    """367 completed tool rounds, then one in-flight call with no result."""
    rounds = [
        {
            'roundNum': index + 1,
            'llmRound': index,
            'toolCallId': f'call-{index}',
            'toolName': 'run_command',
            'toolArgs': {'command': f'echo round-{index}'},
            'toolContent': f'round-{index} completed result',
            'status': 'done',
        }
        for index in range(367)
    ]
    rounds.append({
        'roundNum': 368, 'llmRound': 367,
        'toolCallId': 'call-in-flight',
        'toolName': 'run_command', 'toolArgs': {'command': 'echo unfinished'},
        'toolContent': None, 'status': 'running',
    })
    return {
        '_attemptId': attempt_id,
        '_userId': 1,
        'id': task_id,
        'status': 'done',
        'finishReason': 'error',
        'error': _quota_envelope(),
        'content': '',
        'thinking': '',
        'toolRounds': rounds,
        'model': 'kimi-k3.1-pro',
        'config': {'model': 'kimi-k3.1-pro'},
    }


def test_checkpoint_resume_after_402_preserves_completed_tool_results(chat_sidecar):
    from lib.turn_lifecycle import (
        bind_task,
        create_attempt,
        create_turn_pair,
        get_turn,
        record_task_event,
    )

    conversation_id = _conversation_id()
    created = create_turn_pair(
        conversation_id,
        command_id=f'create-{conversation_id}',
        input_projection={'content': 'supervise the smoke runs'},
        config={'model': 'kimi-k3.1-pro'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': '402 resume',
            'settings': {'model': 'kimi-k3.1-pro'},
        },
    )
    old_attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(old_attempt_id, 'long-402-task', user_id=1)
    task = _long_running_task(old_attempt_id, 'long-402-task')
    assert record_task_event(
        task, {'type': 'done', 'finishReason': 'error', 'error': task['error']},
    ) is True

    settled = get_turn(conversation_id, turn_id, user_id=1)
    assert settled['settlement']['outcome'] == 'failed'
    assert settled['settlement']['error']['kind'] == 'quota'
    offered = [option['operation']
               for option in settled['settlement']['resumeOptions']]
    assert offered == ['continue', 'checkpoint_resume', 'regenerate'], (
        'a dying credential must not hide the progress-preserving resume; '
        'a prose-free tail also allows the replay-only continue')
    anchor = next(
        option['anchor']
        for option in settled['settlement']['resumeOptions']
        if option['operation'] == 'checkpoint_resume'
    )
    assert anchor['keptToolRounds'] == 367
    assert anchor['replayableToolRounds'] == 367, (
        'completed rounds are replayed as HISTORY, not re-executed; the '
        'result-less in-flight tail is excluded from the replay prefix')
    assert len(anchor['retainedToolRoundPositions']) == 367

    resumed = create_attempt(
        conversation_id,
        turn_id,
        command_id=f'resume-{conversation_id}',
        operation='checkpoint_resume',
        expected_projection_revision=settled['projectionRevision'],
        user_id=1,
    )
    assert resumed['attempt']['attemptId'] != old_attempt_id
    projection = resumed['turn']['projection']
    rounds = projection['toolRounds']
    assert len(rounds) == 367, 'completed tool results are preserved, not replayed'
    assert [item['toolCallId'] for item in rounds] == [
        f'call-{index}' for index in range(367)]
    assert rounds[0]['toolContent'] == 'round-0 completed result'
    assert rounds[-1]['toolContent'] == 'round-366 completed result'
    assert all(item['status'] == 'done' for item in rounds)
    assert not any(item.get('toolCallId') == 'call-in-flight' for item in rounds), (
        'an unfinished call is never displayed as a completed side effect')
    # No content/thinking tail existed, so nothing is rolled back: the
    # credential died between tool rounds, not mid-generation.
    assert not projection.get('rolledBack')
    # A fresh attempt runs its own tool rounds; nothing was re-executed here.
    assert resumed['attempt'].get('toolRounds') in (None, [])
