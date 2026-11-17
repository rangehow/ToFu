"""Restart continuity on a real instance (audit 2026-11-18 #10, findings P3 and P6).

Each restart in the audit window cancelled the swarm sub-agents of the dispatched
Workers it interrupted ("Project dispatch authority is no longer available: File
execution requires its registered owned isolated worktree", 4 sub-agents at
11-17 23:48 and 4 at 11-18 02:16), and a lead's Turn accepted just before a
shutdown sat unstarted, then settled 'failed, external outcome unknown' once its
owner stopped it. These specs drive the real two-process runtime (storage host,
execution host, API host; tests/chaos/instance.py) with the scripted provider:

- a dispatched Worker's sub-agent resumes after an explicit restart and finishes
  without repeating its side effect;
- a Turn its execution host entered before dying starts again after the boot and
  completes without the owner.

A Stop reaching a dead execution is specified against real killed processes in
tests/conversation/test_worker_exit_recovery.py: the API host refuses commands
while its execution owner is down, so this harness cannot order a Stop there.

Run with ``make test-chaos`` (or pytest on this file); each spec owns its instance.
"""
from __future__ import annotations

import time

import pytest

from tests.chaos import fake_provider as fp
from tests.chaos.test_chaos_faults import (
    SETTLE_DEADLINE_S, _bind_coordinator, _held_turn, _running_chaos)

pytestmark = [pytest.mark.api, pytest.mark.timeout(900)]


@pytest.fixture
def chaos(tmp_path_factory):
    with _running_chaos(tmp_path_factory.mktemp('chaos-continuity')) as harness:
        yield harness


def _wait(predicate, what):
    deadline = time.monotonic() + SETTLE_DEADLINE_S
    while not predicate():
        assert time.monotonic() < deadline, what()
        time.sleep(0.1)


def _worker_effects(chaos, nonce: str) -> int:
    """Side effects of ``nonce`` in the Workers' own isolated checkouts."""
    return sum(log.read_text().split().count(nonce)
               for log in (chaos.instance.root / 'data' / 'integration').rglob('effects.log'))


def _log_text(chaos) -> str:
    return '\n'.join(path.read_text(errors='replace')
                     for path in sorted(chaos.instance.root.rglob('*.log')) if path.is_file())


def test_a_dispatched_workers_sub_agent_resumes_after_a_restart(chaos):
    coordinator = _bind_coordinator(chaos)
    worker_nonce = chaos.provider.script([])
    sub_nonce = chaos.provider.script([])
    chaos.provider._scripts[sub_nonce] = [
        fp.tool('run_command', {'command': chaos.effect_command(
            sub_nonce, chaos.hang_command(sub_nonce))}),
        fp.text('Sub-agent finished after the restart.')]
    chaos.provider._scripts[worker_nonce] = [
        fp.tool('spawn_agents', {'agents': [{
            'id': 'restart-sub', 'role': 'coder',
            'objective': f'Run the chaos step. [chaos turn={sub_nonce}]'}]}),
        fp.tool('run_command', {'command': chaos.hang_command(worker_nonce)}),
        fp.text('Worker finished. [END_TURN: done]')]
    coordinator_nonce = chaos.provider.script([
        fp.tool('delegate_project_work', {
            'title': 'Run a sub-agent across a restart',
            'brief': f'Spawn the sub-agent, then wait. [chaos turn={worker_nonce}]',
            'idempotency_key': 'chaos-restart-sub'}),
        fp.text('Delegated. [END_TURN: done]')])
    chaos.client.create_turn(coordinator, f'Delegate it. [chaos turn={coordinator_nonce}]',
                             workspace=str(chaos.workspace), first=False)
    # Both the Worker and its sub-agent are inside a running tool when the restart lands.
    chaos.wait_tool_process(sub_nonce)
    _wait(lambda: _worker_effects(chaos, sub_nonce) == 1, lambda: 'the sub-agent never ran its step')
    chaos.wait_tool_process(worker_nonce)
    assert len(chaos.provider.requests(sub_nonce)) == 1
    chaos.instance.restart_execution_host()
    # The resumed sub-agent asks the model again: its interrupted round goes on.
    _wait(lambda: len(chaos.provider.requests(sub_nonce)) >= 2,
          lambda: 'the sub-agent never resumed after the restart: ' + _authority_lines(chaos))
    assert 'Project dispatch authority is no longer available' not in _log_text(chaos)
    # Its side effect ran once: the resume continued the round, it did not replay it.
    assert _worker_effects(chaos, sub_nonce) == 1


def _authority_lines(chaos) -> str:
    return ' | '.join(line for line in _log_text(chaos).splitlines()
                      if 'authority' in line and 'ProjectDispatch' in line)[-1500:]


def test_a_turn_its_dead_execution_host_entered_starts_again_after_boot(chaos):
    """P3 (attempt 5234bf17): a Turn entered just before its execution host went
    away had produced nothing when the next generation took over. It continues
    by itself, once, and completes; it is not left waiting for the owner."""
    conversation, nonce, user_text, _response, release = _held_turn(chaos, 'Started after the boot.')
    try:
        chaos.instance.kill_execution_host()
    finally:
        release.set()
    chaos.instance.recover_execution_host()
    chaos.assert_contract(conversation, user_text, outcomes={'completed'},
                          final_text='Started after the boot.')
    assert len(chaos.provider.requests(nonce)) >= 2
