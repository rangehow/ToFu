"""A pending restart takes precedence over new protected steps (lib/runtime/protected_steps.py).

Real processes and real ``flock`` waits: a landing that starts while a restart
is pending waits for it and then runs; a restart waits for the landings already
running; a restart that hands over keeps precedence until its process exits.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

_CHILD = r'''
import sys, time
from lib.runtime import protected_steps
protected_steps.protected_steps_root = lambda: sys.argv[2]
role, root, journal = sys.argv[1], sys.argv[2], sys.argv[3]
def note(event):
    with open(journal, 'a') as handle:
        handle.write(f'{event} {time.monotonic():.6f}\n')
def waiting(message):
    note(f'{role}-waits')
if role == 'restart':
    with protected_steps.restart_precedence('test restart', on_wait=waiting) as held:
        note('restart-holds')
        sys.stdin.readline()
        if len(sys.argv) > 4:
            held.retain_until_exit()
            note('restart-retained')
            sys.stdin.readline()
    note('restart-released')
    sys.stdin.readline()
else:
    with protected_steps.protected_step('git_land', detail=role, on_wait=waiting):
        note(f'{role}-runs')
        sys.stdin.readline()
    note(f'{role}-done')
'''


class _Child:
    def __init__(self, tmp_path: Path, role: str, *extra: str):
        self.journal = tmp_path / 'journal'
        environment = {**os.environ, 'PYTHONPATH': str(ROOT)}
        self.process = subprocess.Popen(
            [sys.executable, '-c', _CHILD, role, str(tmp_path / 'steps'), str(self.journal), *extra],
            stdin=subprocess.PIPE, text=True, cwd=ROOT, env=environment)

    def step(self) -> None:
        self.process.stdin.write('\n')
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


def _events(journal: Path) -> list[str]:
    try:
        return [line.split()[0] for line in journal.read_text().splitlines()]
    except FileNotFoundError:
        return []


def _until(journal: Path, event: str, deadline: float = 30.0) -> None:
    stop = time.monotonic() + deadline
    while event not in _events(journal):
        assert time.monotonic() < stop, f'{event} never happened: {_events(journal)}'
        time.sleep(0.02)


@pytest.fixture
def children(tmp_path):
    started: list[_Child] = []

    def start(role: str, *extra: str) -> _Child:
        child = _Child(tmp_path, role, *extra)
        started.append(child)
        return child
    yield start
    for child in started:
        child.close()


def test_a_landing_waits_for_a_pending_restart_then_runs(tmp_path, children):
    journal = tmp_path / 'journal'
    restart = children('restart')
    _until(journal, 'restart-holds')
    landing = children('landing')
    _until(journal, 'landing-waits')
    time.sleep(0.5)
    assert 'landing-runs' not in _events(journal)
    from lib.runtime import protected_steps
    original = protected_steps.protected_steps_root
    protected_steps.protected_steps_root = lambda: str(tmp_path / 'steps')
    try:
        assert protected_steps.pending_restart()['detail'] == 'test restart'
        restart.step()
        _until(journal, 'landing-runs')
        assert protected_steps.pending_restart() is None
    finally:
        protected_steps.protected_steps_root = original
    events = _events(journal)
    assert events.index('restart-released') < events.index('landing-runs')
    landing.step()
    _until(journal, 'landing-done')


def test_a_restart_waits_for_running_landings_and_holds_back_new_ones(tmp_path, children):
    journal = tmp_path / 'journal'
    first = children('first')
    _until(journal, 'first-runs')
    restart = children('restart')
    _until(journal, 'restart-waits')
    time.sleep(0.5)
    assert 'restart-holds' not in _events(journal)
    second = children('second')
    _until(journal, 'second-waits')
    first.step()
    _until(journal, 'restart-holds')
    time.sleep(0.5)
    assert 'second-runs' not in _events(journal)
    restart.step()
    _until(journal, 'second-runs')
    second.step()
    _until(journal, 'second-done')


def test_a_handed_over_restart_keeps_precedence_until_its_process_exits(tmp_path, children):
    journal = tmp_path / 'journal'
    restart = children('restart', 'retain')
    _until(journal, 'restart-holds')
    restart.step()
    _until(journal, 'restart-retained')
    restart.step()
    _until(journal, 'restart-released')
    landing = children('landing')
    _until(journal, 'landing-waits')
    time.sleep(0.5)
    assert 'landing-runs' not in _events(journal)
    restart.process.kill()
    restart.process.wait()
    _until(journal, 'landing-runs')
    landing.step()
