"""Executable specification of scripts/blackbox_run.py's pytest passthrough.

The runner advertises ``[pytest args...]`` after ``--``. ``parse_known_args``
keeps the separator in the extras, so without stripping it pytest receives a
stray ``--`` and takes the first option for a path: observed as
``ERROR: file or directory not found: -q`` with 0 tests collected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / 'scripts'))

import blackbox_run  # noqa: E402

pytestmark = pytest.mark.unit

SERVER = 'http://127.0.0.1:9'


class _Completed:
    returncode = 0


def _captured(monkeypatch) -> dict:
    seen: dict = {}

    def run(command, **kwargs):
        seen['command'] = command
        seen['environment'] = kwargs.get('env')
        return _Completed()

    monkeypatch.setattr(blackbox_run.subprocess, 'run', run)
    return seen


def test_pytest_options_after_the_separator_reach_pytest_unchanged(monkeypatch):
    seen = _captured(monkeypatch)
    assert blackbox_run.main(['--server-url', SERVER, '--', '-q', '-k', 'push']) == 0
    command = seen['command']
    assert command[-3:] == ['-q', '-k', 'push']
    assert '--' not in command[3:]
    assert command[3] == blackbox_run.HARNESS


def test_the_separator_may_be_omitted(monkeypatch):
    seen = _captured(monkeypatch)
    assert blackbox_run.main(['--server-url', SERVER, '-x']) == 0
    assert seen['command'][-1] == '-x'


def test_a_lone_separator_runs_the_whole_harness(monkeypatch):
    seen = _captured(monkeypatch)
    assert blackbox_run.main(['--server-url', SERVER, '--']) == 0
    assert seen['command'][3:] == [blackbox_run.HARNESS]


def test_the_harness_target_is_the_exported_server_url(monkeypatch):
    seen = _captured(monkeypatch)
    blackbox_run.main(['--server-url', SERVER + '/', '--reference-url', SERVER + '//'])
    assert seen['environment']['TOFU_BLACKBOX_URL'] == SERVER
    assert seen['environment']['TOFU_BLACKBOX_REFERENCE_URL'] == SERVER
