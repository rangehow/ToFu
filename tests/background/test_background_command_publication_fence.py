"""A task's publication fence follows its own commands, not a failed record sweep.

A launch mints its record directory and then publishes ``record.json`` before
it starts any process. A directory left with no ``record.json`` (only the
writer's lock file) therefore never started a command: an orphan of a launch
interrupted between those two steps. Recovery used to count that orphan as an
unfinished scan for the whole process, and the delivery gate
(``task_has_running_commands``) then reported every task as having a running
background command for good. The two settled attempts in the 2026-11-19 audit
(assignments fdaed9da and a1fefd73) held publication for that reason while
their Workers reported no running command.

These specs use the durable records and the process registry directly, with no
real background command: a completed record settles, an orphan directory is not
a command, and a registry entry whose ``done`` event is unset remains the one
authority that still holds publication.
"""
from __future__ import annotations

import os
import threading

import pytest

from lib.tasks_pkg import background_commands

pytestmark = pytest.mark.unit

_UID = 424242
_SETTLED_TASK = 'conversation-attempt:settled'


@pytest.fixture(autouse=True)
def records(monkeypatch, tmp_path):
    root = tmp_path / 'background_commands'
    root.mkdir()
    monkeypatch.setattr(background_commands, '_records_root', lambda: str(root))
    with background_commands._lock:
        background_commands._registry.clear()
        background_commands._recovery_in_progress = False
        background_commands._recovery_scan_failed = False
        background_commands._recovery_incomplete_conversations.clear()
    yield root
    with background_commands._lock:
        background_commands._registry.clear()
        background_commands._recovery_in_progress = False
        background_commands._recovery_scan_failed = False
        background_commands._recovery_incomplete_conversations.clear()


def _record(root, command_id, *, task_id=_SETTLED_TASK, exit_code=None):
    paths = background_commands._record_paths(os.path.join(str(root), command_id))
    os.makedirs(paths['dir'])
    background_commands._write_record(paths, {
        'command_id': command_id, 'command': 'printf hi',
        'task_id': task_id, 'conv_id': '', 'origin_conv_id': '',
        'user_id': _UID, 'started_at': 0.0, 'credentials': {},
        'pid': None, 'pgid': None, 'pid_starttime': None,
    })
    if exit_code is not None:
        with open(paths['exit'], 'w', encoding='ascii') as stream:
            stream.write(str(exit_code))
    return paths


def _orphan(root, command_id):
    """A launch that minted its directory and died before publishing metadata."""
    directory = os.path.join(str(root), command_id)
    os.makedirs(directory)
    with open(os.path.join(directory, 'record.json.lock'), 'w', encoding='ascii'):
        pass


def _running_entry(command_id, *, task_id=_SETTLED_TASK, done=None):
    done = done or threading.Event()
    with background_commands._lock:
        background_commands._registry[command_id] = {
            'done': done,
            'task': {'id': task_id},
            'launch_task_id': task_id,
            'user_id': _UID,
            'conv_id': '',
            'origin_conv_id': '',
            'state': {'command': 'sleep 300', 'finished': False,
                      'delivery_status': 'not_applicable'},
        }
    return done


def test_an_orphaned_record_does_not_hold_a_settled_tasks_publication(records):
    _orphan(records, 'bg_00000000dead')
    _record(records, 'bg_00000000beef', exit_code=0)

    settled = background_commands.recover_background_commands_on_startup()

    assert settled == 1
    assert background_commands.task_has_running_commands(
        task_id=_SETTLED_TASK, user_id=_UID) is False
    assert background_commands.recovery_needs_retry() is False
    assert not os.path.exists(os.path.join(str(records), 'bg_00000000beef'))


def test_a_genuinely_running_command_still_holds_publication(records):
    done = _running_entry('bg_00000000cafe')

    assert background_commands.task_has_running_commands(
        task_id=_SETTLED_TASK, user_id=_UID) is True

    done.set()
    assert background_commands.task_has_running_commands(
        task_id=_SETTLED_TASK, user_id=_UID) is False