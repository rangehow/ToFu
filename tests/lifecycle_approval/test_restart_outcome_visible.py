"""A restart that fails outside the server is visible in Settings (check: no silent failure).

``serverctl restart`` can stop before the manager is reached (engine
certification, frontend build); the owner saw nothing in Settings on
2026-11-17. The outcome is recorded on the restart's operator-pass record and
``/update/check`` projects the newest failure since this server started.
"""
from __future__ import annotations

import time

import pytest

import lib.lifecycle_approval as la

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def approvals(tmp_path, monkeypatch):
    monkeypatch.setattr(la, '_APPROVALS_FILE', str(tmp_path / 'approvals.json'))


def _terminal_pass() -> str:
    return la.record_operator_pass('restart', {'kind': 'interactive-terminal', 'pid': 1, 'tty': '/dev/pts/1'})['id']


def test_a_failed_terminal_restart_names_its_stage_and_reason():
    started = time.time() - 1
    approval = _terminal_pass()
    la.record_restart_outcome(approval, {
        'ok': False, 'stage': 'storage engine certification',
        'reason': "Cannot certify HEAD's storage engine: the engine gate exited 1"})
    failure = la.latest_restart_failure(since=started)
    assert failure['stage'] == 'storage engine certification'
    assert 'engine gate exited 1' in failure['reason']
    assert failure['via'] == 'interactive-terminal' and failure['approvalId'] == approval


def test_a_later_attempt_supersedes_an_earlier_failure():
    started = time.time() - 1
    la.record_restart_outcome(_terminal_pass(), {'ok': False, 'stage': 'frontend build', 'reason': 'vite'})
    time.sleep(0.01)
    la.record_restart_outcome(_terminal_pass(), {'ok': True, 'stage': 'ready', 'reason': ''})
    assert la.latest_restart_failure(since=started) is None


def test_failures_before_this_server_started_are_not_shown():
    la.record_restart_outcome(_terminal_pass(), {'ok': False, 'stage': 'frontend build', 'reason': 'vite'})
    assert la.latest_restart_failure(since=time.time() + 1) is None


def test_update_check_projects_the_failure(monkeypatch):
    from routes.api_v1 import update
    from lib import boot_identity
    monkeypatch.setattr(boot_identity, 'BOOT_TS', time.time() - 1)
    la.record_restart_outcome(_terminal_pass(), {'ok': False, 'stage': 'manager', 'reason': 'stubbed'})
    payload = update._enrich_with_restart_failure({})
    assert payload['last_restart_failure']['stage'] == 'manager'
