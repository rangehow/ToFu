"""The restart-storm guard of automatic resume, on a real two-process instance.

Contract (lib/shutdown_marker.py ``is_restart_storm``; docs/RUN_CONTINUITY.md,
"Restart-paused conversations resume by themselves"): automatic resume stands
down only while the server is crash-looping, so it never re-fires paid Turns
into a loop that kills them again. Deliberate restarts (the owner's, the
Router's, a redeploy) are not a crash loop, however many arrive in a row. When
the guard does stand automatic resume down, the paused Turn says why and
offers Continue, and Continue completes it.

Each test owns its instance: restarts count toward the guard, so they must not
share boot history with other specs.
"""
from __future__ import annotations

import time

import pytest

from tests.chaos import fake_provider as fp
from tests.chaos.client import CHAT_CONFIG, MODEL_ID
from tests.chaos.test_chaos_faults import SETTLE_DEADLINE_S, _held_turn, _running_chaos, _scripted

pytestmark = [pytest.mark.api, pytest.mark.timeout(900)]


@pytest.fixture
def chaos(tmp_path_factory):
    with _running_chaos(tmp_path_factory.mktemp('chaos-guard')) as harness:
        yield harness


def _assistant(chaos, conversation: str) -> dict:
    return [t for t in chaos.client.turns(conversation) if t.get('actor') == 'assistant'][-1]


def _wait_assistant(chaos, conversation: str, predicate, what: str) -> dict:
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    turn = None
    while time.monotonic() < deadline:
        try:
            turn = _assistant(chaos, conversation)
        except (OSError, RuntimeError, IndexError):
            time.sleep(0.2)
            continue
        if predicate(turn):
            return turn
        time.sleep(0.2)
    raise AssertionError(f'{what}: {turn and (turn.get("status"), turn.get("settlement"))}')


def _tool_turn(chaos, final: str):
    conversation, nonce, user_text, _ = _scripted(chaos, lambda n: [
        fp.tool('run_command', {'command': chaos.effect_command(n, chaos.hang_command(n))}),
        fp.text(final)])
    chaos.wait_tool_process(nonce)
    chaos.wait_effect(nonce)
    return conversation, nonce, user_text


def test_deliberate_restarts_in_a_row_never_pause_automatic_resume(chaos):
    """Six deliberate restarts within the guard's window (the first boot plus
    five joined restarts), the last one cutting off a running tool: the Turn
    still continues by itself, once, because none of them crashed."""
    for _ in range(4):
        chaos.instance.restart_execution_host()
    conversation, nonce, user_text = _tool_turn(chaos, 'Continued after many restarts.')
    chaos.instance.restart_execution_host()
    turn = _wait_assistant(chaos, conversation, lambda t: t.get('status') == 'completed'
                           or (t.get('status') == 'interrupted' and _paused(t)),
                           'the Turn neither completed nor paused')
    assert turn['status'] == 'completed', turn.get('settlement')
    chaos.assert_contract(conversation, user_text, outcomes={'completed'},
                          final_text='Continued after many restarts.', effects={nonce: 1})


def _paused(turn: dict) -> bool:
    error = (turn.get('settlement') or {}).get('error') or {}
    return bool(error.get('automaticResume'))


def test_a_crash_loop_pauses_automatic_resume_visibly_and_continue_recovers(chaos):
    """Five crashes in a row (SIGKILL of the execution host, recovered by its
    owner), the last one cutting off a streaming answer: automatic resume
    stands down, the interrupted Turn's settlement says why and offers
    Continue, and the owner's Continue completes it."""
    for _ in range(4):
        chaos.instance.kill_execution_host()
        chaos.instance.recover_execution_host()
    conversation, _nonce, user_text, _, release = _held_turn(chaos, 'Continued by the owner.')
    try:
        chaos.instance.kill_execution_host()
    finally:
        release.set()
    chaos.instance.recover_execution_host()
    turn = _wait_assistant(chaos, conversation, lambda t: t.get('status') == 'interrupted' and _paused(t),
                           'the paused Turn never explained its pause')
    settlement = turn['settlement']
    error = settlement['error']
    assert 'crash loop' in error['message'], error
    assert error['automaticResume']['state'] == 'paused', error
    assert error['automaticResume']['guard'] == 'restart_storm', error
    continue_option = next(option for option in settlement['resumeOptions']
                           if option['operation'] in ('continue', 'checkpoint_resume'))
    chaos.client.request(
        'POST', f'/api/v4/conversations/{conversation}/turns/{turn["turnId"]}/attempts',
        {'commandId': f'owner-continue-{turn["currentAttemptId"]}',
         'config': {**CHAT_CONFIG, 'model': MODEL_ID, 'projectPath': str(chaos.workspace)},
         'operation': continue_option['operation'], 'resumeAnchor': continue_option['anchor'],
         'expectedProjectionRevision': turn['projectionRevision']})
    chaos.assert_contract(conversation, user_text, outcomes={'completed'},
                          final_text='Continued by the owner.')
