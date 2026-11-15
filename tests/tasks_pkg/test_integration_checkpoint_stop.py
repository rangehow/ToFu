"""A Stop during integration_checkpoint retires its capture instead of waiting it out.

2026-11-12: once the tool pool's 300 s deadline was removed from
integration_checkpoint and integration_submit, a Stop under load waited 515-560 s
for ``checkpoint_workspace``: its private-index capture (``git add -A`` across the
whole checkout, supervised only by the stall window) never saw the call's
cancellation. A fake ``git`` blocks the capture's ``add`` until something retires it.
"""
import os
import signal
import threading
import time

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from tests.storage.test_integration_delivery import automatic_workspace, repo as repo
from tests.storage.test_integration_snapshot_liveness import assert_retired, fake_git
from tests.tasks_pkg.test_integration_workspace_identity import call_tool, tool_task

pytestmark = pytest.mark.unit

# The stall window alone would end the blocked capture only after STALL seconds;
# within STOP_BOUND only the Stop can.
STALL = 600.0
STOP_BOUND = 30.0


@pytest.mark.parametrize('operation, args', [
    ('integration_checkpoint', {'note': 'stopping'}),
    ('integration_submit', {'summary': 'stopping'}),
])
def test_stop_retires_a_blocked_checkpoint_capture(repo, monkeypatch, tmp_path, operation, args):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('owned task\n')
    before = state.get_workspace(str(root), 'automatic', user_id=owner)
    monkeypatch.setattr(ic, 'GIT_STALL_SECONDS', STALL)
    real_git = ic.git_executable
    marker = fake_git(tmp_path, monkeypatch, 'hung', 'add')
    task = tool_task(repo, work)
    results = []
    call = threading.Thread(target=lambda: results.append(
        call_tool(repo, monkeypatch, work, operation, args, task=task)[0]), daemon=True)
    call.start()
    deadline = time.monotonic() + STOP_BOUND
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), 'the checkpoint capture never reached git add'

    stopped = time.monotonic()
    task['aborted'] = True
    call.join(STOP_BOUND)
    if call.is_alive():
        os.kill(int(marker.read_text()), signal.SIGKILL)  # release the blocked call
        call.join(STOP_BOUND)
        pytest.fail(f'Stop did not end the checkpoint capture within {STOP_BOUND:.0f} s')
    assert time.monotonic() - stopped < STOP_BOUND
    [result] = results
    assert result.startswith('Error:') and 'cancelled' in result, result
    assert_retired(marker)
    after = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert (after['checkpoint_sha'], after['state']) == (before['checkpoint_sha'], before['state'])

    # The Stop left nothing held: the next call checkpoints normally.
    monkeypatch.setattr(ic, 'git_executable', real_git)
    result, _ = call_tool(repo, monkeypatch, work, 'integration_checkpoint', {})
    assert result.startswith('Checkpoint saved for automatic:'), result
