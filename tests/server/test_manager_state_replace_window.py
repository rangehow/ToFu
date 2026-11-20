"""The manager's state file is read consistently while the manager replaces it.

``server_manager`` and ``oob_alert`` are standard-library only, so they share
the store's ``<path>.lock`` file lock directly: the manager publishes under it
and a reader that sees the file missing re-reads under it.
``tests.support.replace_window`` reproduces the FUSE ENOENT window.
"""

import json
import os
import time
from pathlib import Path

import pytest

import server_manager as sm
from serverctl_pkg import oob_alert
from tests.support.replace_window import ReplaceWindow

pytestmark = pytest.mark.unit


def _publish(path: Path, value: dict) -> None:
    staging = path.with_name(path.name + '.staging')
    staging.write_text(json.dumps(value), encoding='utf-8')
    os.replace(staging, path)


def _state_root(tmp_path: Path, value: dict) -> tuple[Path, Path]:
    root = tmp_path / 'tofu'
    path = root / 'data' / sm.MANAGER_STATE_FILE
    path.parent.mkdir(parents=True)
    sm._atomic_json(path, value)
    return root, path


def _planned(now: float) -> dict:
    return {'lastWorkerExitKind': 'planned', 'lastWorkerExitAt': now - 30,
            'lastRestart': {'source': 'serverctl', 'outcome': 'started', 'startedAt': now - 90}}


def test_planned_restart_read_during_replace_sees_the_restart(tmp_path, monkeypatch):
    now = time.time()
    root, path = _state_root(tmp_path, _planned(now))
    window = ReplaceWindow(monkeypatch, str(path))
    window.run_writer(lambda: _publish(path, _planned(now)))

    seen = window.read_during_gap(lambda: oob_alert.planned_restart(root))

    assert seen.get('outcome') == 'started'


def test_restart_outcome_read_during_replace_sees_the_outcome(tmp_path, monkeypatch):
    outcome = {'lastRestart': {'outcome': 'spawned'}}
    root, path = _state_root(tmp_path, outcome)
    window = ReplaceWindow(monkeypatch, str(path))
    window.run_writer(lambda: _publish(path, outcome))

    assert window.read_during_gap(lambda: sm.read_restart_outcome(str(root))) == {'outcome': 'spawned'}


def test_the_manager_publishes_its_state_under_the_store_lock(tmp_path, monkeypatch):
    _root, path = _state_root(tmp_path, {'version': 'old'})
    window = ReplaceWindow(monkeypatch, str(path))
    window.run_writer(lambda: _publish(path, {'version': 'other writer'}))

    window.read_during_gap(lambda: sm._atomic_json(path, {'version': 'manager'}))

    assert json.loads(path.read_text(encoding='utf-8')) == {'version': 'manager'}


def test_absent_state_reads_create_no_lock_file(tmp_path):
    root = tmp_path / 'tofu'
    (root / 'data').mkdir(parents=True)

    assert oob_alert.planned_restart(root) == {}
    assert sm.read_restart_outcome(str(root)) == {}
    assert os.listdir(root / 'data') == []
