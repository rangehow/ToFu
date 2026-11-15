"""A Worker's workspace admission is its liveness while it advances.

2026-11-11: 8 of 19 Worker admissions were force-failed by the stuck-task
reaper ("WEDGED - no event/dispatch progress for 1864s", surfacing as "Task
cancelled during workspace admission"). They were queued for repository
admission and snapshotting or checking out on a contended network volume.
Worker entry resets both liveness clocks, but nothing in the admission
refreshed them, so any admission slower than the stall threshold was reaped,
however steadily it advanced.

Admission now reports each observed advance: every poll while it is queued
behind a live lock holder, and every observed execution of its snapshot and
checkout Git. A command blocked on the volume reports nothing. Its silence is
still judged by the same threshold, and its own stall window still retires it.

Each test samples the reaper's own judge (``liveness.judge``) while one
admission phase runs. The clocks are reset when the phase starts, as Worker
entry sets them, and the threshold is far shorter than the phase.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from lib import integration_control as ic
from lib.tasks_pkg import liveness
from lib.tasks_pkg import workspace_delivery
from tests.storage.test_integration_snapshot_liveness import fake_git

pytestmark = pytest.mark.unit

# Several activity samples (one per second) fit in the threshold; the phase
# lasts well past it, so silence alone would be judged wedged.
THRESHOLD = 3.0
PHASE = 2.5 * THRESHOLD
TASK_ID = 'admitting-worker'


def git(root, *args):
    cp = subprocess.run(['git', '-C', str(root), *args], text=True,
                        capture_output=True, timeout=20)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **k: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase', lambda *a, **k: None)
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', '')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    root = tmp_path / 'canonical'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Admission liveness')
    git(root, 'config', 'user.email', 'admission@example.invalid')
    git(root, 'config', '--local', 'tofu.workspaceMode', 'isolated')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'base')
    task = {'id': TASK_ID, 'convId': 'conversation', '_userId': 1, 'status': 'running',
            'config': {'projectPath': str(root)}}
    try:
        yield root.resolve(), task
    finally:
        install_runtime_for_test(None)
        from lib import project_mod
        project_mod.config.clear_conv_state(TASK_ID)


class ReaperView:
    """The reaper's verdicts on ``task`` from ``started()`` until ``finished()``."""

    def __init__(self, task, threshold, *, started, finished):
        self.samples: list[tuple[float, str, float]] = []
        self.began = self.ended = 0.0
        self._task, self._threshold = task, threshold
        self._started, self._finished = started, finished
        self._thread = threading.Thread(target=self._sample, name='reaper-view', daemon=True)
        self._thread.start()

    def _sample(self):
        deadline = time.monotonic() + 60
        while not self._started() and time.monotonic() < deadline:
            time.sleep(0.01)
        entered = time.time()
        self._task['created_at'] = entered
        self._task['_t_last_event'] = self._task['_dispatch_heartbeat'] = entered
        self.began = time.monotonic()
        while not self._finished() and time.monotonic() < deadline:
            verdict = liveness.judge(self._task, time.time(), self._threshold)
            self.samples.append((round(time.monotonic() - self.began, 2), verdict.state,
                                 round(verdict.silent_seconds, 2)))
            time.sleep(0.05)
        self.ended = time.monotonic()

    @property
    def verdicts(self) -> list[str]:
        return [state for _at, state, _silent in self.samples]

    def join(self):
        self._thread.join(60)
        assert not self._thread.is_alive()
        return self.ended - self.began


def process_ended(marker: Path):
    def ended():
        try:
            os.kill(int(marker.read_text()), 0)
        except (FileNotFoundError, ValueError):
            return False  # Not started yet.
        except ProcessLookupError:
            return True
        return False
    return ended


def queued_behind_a_holder(root: Path, monkeypatch):
    """Hold the admission's destination, as a live discard or same-task retry
    would, for one phase after the admission (on this test's thread) starts
    waiting for it. Admission takes no repository admission."""
    destination = ic._task_destination(root, 1, TASK_ID)
    key = 'destination:' + str(destination.resolve())
    held, queued, released = threading.Event(), threading.Event(), threading.Event()
    lock = ic._file_lock

    def observed(name, **kwargs):
        if name == key and threading.current_thread() is threading.main_thread():
            queued.set()
        return lock(name, **kwargs)

    monkeypatch.setattr(ic, '_file_lock', observed)

    def hold():
        with ic._destination_lock(destination):
            held.set()
            queued.wait(60)
            time.sleep(PHASE)
        released.set()

    threading.Thread(target=hold, name='admission-holder', daemon=True).start()
    assert held.wait(30)
    return queued.is_set, released.is_set


@pytest.mark.parametrize('phase', ['queued', 'snapshot', 'checkout'])
def test_an_admission_that_keeps_advancing_is_never_judged_wedged(
        worker, tmp_path, monkeypatch, phase):
    root, task = worker
    if phase == 'queued':
        started, finished = queued_behind_a_holder(root, monkeypatch)
    else:
        subcommand = {'snapshot': 'add', 'checkout': 'checkout'}[phase]
        marker = fake_git(tmp_path, monkeypatch, 'slow', subcommand, seconds=PHASE)
        started, finished = marker.exists, process_ended(marker)
    view = ReaperView(task, THRESHOLD, started=started, finished=finished)

    workspace_delivery.bind_worker_workspace(task)

    lasted = view.join()
    assert lasted >= 2 * THRESHOLD, 'the phase must outlast the threshold to mean anything'
    assert view.verdicts and 'wedged' not in view.verdicts, view.samples
    workspace = Path(task['config']['projectPath'])
    assert workspace != root and (workspace / 'base.txt').read_text() == 'base\n'


def test_an_admission_blocked_on_the_volume_is_still_judged_wedged(
        worker, tmp_path, monkeypatch):
    root, task = worker
    blocked_threshold = 1.0
    # The blocked command's own stall window retires it after the reaper's
    # threshold has passed; production uses 90 s against 1,800 s.
    monkeypatch.setattr(ic, 'GIT_STALL_SECONDS', 4 * blocked_threshold)
    marker = fake_git(tmp_path, monkeypatch, 'hung', 'add')
    view = ReaperView(task, blocked_threshold, started=marker.exists,
                      finished=process_ended(marker))

    with pytest.raises(ic.IntegrationError, match='made no progress'):
        workspace_delivery.bind_worker_workspace(task)

    view.join()
    assert 'wedged' in view.verdicts, view.samples
    assert Path(task['config']['projectPath']).resolve() == root
