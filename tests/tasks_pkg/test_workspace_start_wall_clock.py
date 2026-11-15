"""A Worker start never fails on a slow Git step that is still executing (SCALE-REPO-2).

Every start read its repository's delivery-commit decision
(``linear_git_checkpoint.is_enabled``: ``rev-parse --show-toplevel`` and
``config --get tofu.linearCheckpoint``) under the fixed 30 s wall-clock
default, and a start that captures its input also ran ``commit-tree`` and
``update-ref`` under it (WORKSPACE-ADMISSION's lead, 2026-11-13). On a
contended volume a step that is still executing outlives such a deadline. They
now run under the shared ``GIT_STALL_SECONDS`` window, as the identity reads
have since 2026-11-03 ("Supervise workspace admission Git by activity instead
of a 10 s deadline"). A fake ``git`` stands in for the contended volume, with
deadlines dilated as in ``test_workspace_start_liveness.py``.
"""
import time

import pytest

from lib import integration_control as ic
from lib import linear_git_checkpoint as linear
from tests.storage.test_local_task_checkouts import OWNER, create, git, layout as layout  # noqa: F401
from tests.tasks_pkg.test_workspace_start_liveness import (
    DILATION, OLD_DEADLINE, WINDOW, dilated as dilated, fake_git, repo as repo)

pytestmark = pytest.mark.unit


@pytest.fixture
def dilated_decision(monkeypatch, dilated):
    """The delivery-commit decision reads through the same dilated volume."""
    real = linear.subprocess

    class Dilated:
        def __getattr__(self, name):
            return getattr(real, name)

        @staticmethod
        def run(*args, timeout=None, **kwargs):
            return real.run(*args, timeout=None if timeout is None else timeout / DILATION,
                            **kwargs)

    monkeypatch.setattr(linear, 'subprocess', Dilated())
    monkeypatch.setattr(linear, 'GIT_STALL_SECONDS', WINDOW)
    monkeypatch.delenv('TOFU_LINEAR_GIT_CHECKPOINT', raising=False)


def through_fake_git(monkeypatch, tmp_path, mode, needle, seconds=0.0):
    marker = fake_git(tmp_path, monkeypatch, mode, needle, seconds=seconds)
    monkeypatch.setattr(linear, 'git_executable', ic.git_executable)
    return marker


@pytest.mark.parametrize('needle', ['--show-toplevel', 'tofu.linearCheckpoint'])
def test_a_slow_delivery_commit_decision_outlives_the_old_wall_clock_deadline(
        repo, tmp_path, monkeypatch, dilated_decision, needle):
    git(repo, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    marker = through_fake_git(monkeypatch, tmp_path, 'slow', needle, seconds=1.5 * OLD_DEADLINE)
    started = time.monotonic()

    assert linear.is_enabled(repo) is True

    assert time.monotonic() - started > 1.5 * OLD_DEADLINE
    assert marker.exists()


def test_an_unresponsive_delivery_commit_decision_still_ends(
        repo, tmp_path, monkeypatch, dilated_decision):
    through_fake_git(monkeypatch, tmp_path, 'hung', 'tofu.linearCheckpoint')
    started = time.monotonic()

    with pytest.raises(linear.LinearCheckpointError, match='no progress|stalled|config'):
        linear.is_enabled(repo)

    # One window, plus sampling and process-group retirement; never forever.
    assert WINDOW <= time.monotonic() - started < WINDOW + 30


@pytest.mark.parametrize('needle', ['commit-tree', 'update-ref'])
def test_a_slow_input_capture_outlives_the_old_wall_clock_deadline(
        layout, tmp_path, monkeypatch, dilated_decision, needle):
    root = layout['root']
    # A repository that publishes into its working tree captures each start's input.
    (root / 'base.txt').write_text('an earlier delivery, uncommitted\n')
    marker = through_fake_git(monkeypatch, tmp_path, 'slow', needle, seconds=1.5 * OLD_DEADLINE)
    started = time.monotonic()

    checkout = create(layout, 'worker')

    assert time.monotonic() - started > 1.5 * OLD_DEADLINE
    assert marker.exists()
    assert (checkout / 'base.txt').read_text() == 'an earlier delivery, uncommitted\n'
    assert git(root, 'rev-parse', ic._input_ref(OWNER, 'worker'))
