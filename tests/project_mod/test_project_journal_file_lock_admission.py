"""A file's journal lock is bounded, leaves no residue and names its holder.

Project-wide admission order was removed (owner decision 2026-11-10); only
writers of the same file wait for each other.
"""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lib.project_mod import journal_locks as locks
from lib.project_mod.tool_admission import tool_admission_scope

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_waiter_that_gives_up_leaves_nothing_behind(tmp_path, monkeypatch):
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.2)

    def enter(path='file.txt'):
        with locks.path_locks(session, [path]):
            return 'admitted'

    with ThreadPoolExecutor(max_workers=1) as pool:
        with locks.path_locks(session, ['file.txt']):
            with pytest.raises(locks.JournalLockBusy):
                pool.submit(enter).result(timeout=5)
            # Another file of the same journal is never held up.
            assert pool.submit(enter, 'other.txt').result(timeout=5) == 'admitted'
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0)
    assert enter() == 'admitted'
    assert not locks._active_descriptors and not locks._locals


def test_failed_acquisition_leaves_nothing_behind(tmp_path, monkeypatch):
    session = str(tmp_path)
    flock = fcntl.flock

    def broken(descriptor, operation):
        raise OSError('lock storage failed')

    monkeypatch.setattr(fcntl, 'flock', broken)
    with pytest.raises(OSError, match='lock storage failed'):
        with locks.path_locks(session, ['file.txt']):
            pass
    monkeypatch.setattr(fcntl, 'flock', flock)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0)
    with locks.path_locks(session, ['file.txt']):
        pass
    assert not locks._active_descriptors and not locks._locals


@pytest.mark.parametrize('same_task', [True, False])
def test_busy_names_the_file_the_holding_tool_call_and_how_long_it_held(
        tmp_path, monkeypatch, same_task):
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.3)
    held, release = threading.Event(), threading.Event()
    holder_task = {'id': 'task-a'}

    def hold():
        with tool_admission_scope(holder_task, tool_name='run_command'):
            with locks.path_locks(session, ['src/app.py']):
                held.set()
                assert release.wait(10)

    holder = threading.Thread(target=hold, name='tool-parallel_0')
    holder.start()
    try:
        assert held.wait(5)
        with tool_admission_scope(holder_task if same_task else {'id': 'task-b'},
                                  tool_name='edit_file'):
            with pytest.raises(locks.JournalLockBusy) as caught:
                with locks.path_locks(session, ['src/app.py']):
                    pass
    finally:
        release.set()
        holder.join(10)
    message = str(caught.value)
    relation = 'this task' if same_task else 'another task'
    assert 'Journal lock for file src/app.py is busy' in message, message
    assert f'Held by run_command in {relation} (thread tool-parallel_0, pid {os.getpid()}) for ' in message
    held_for = float(message.split(') for ', 1)[1].split('s', 1)[0])
    assert held_for + 0.01 >= caught.value.waited_seconds > 0
    assert 'no file mutation was performed' in message and 're-read' in message


def test_busy_names_a_holder_in_another_process(tmp_path, monkeypatch):
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.3)
    program = (
        'import sys\n'
        'from lib.project_mod import journal_locks as locks\n'
        'with locks.path_locks(sys.argv[1], ["file.txt"]):\n'
        '    print("held", flush=True)\n'
        '    sys.stdin.read()\n')
    child = subprocess.Popen([sys.executable, '-c', program, session], cwd=str(ROOT),
                             env={**os.environ, 'PYTHONPATH': str(ROOT)},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline() == 'held\n'
        with pytest.raises(locks.JournalLockBusy) as caught:
            with locks.path_locks(session, ['file.txt']):
                pass
        assert f'thread MainThread, pid {child.pid} outside a project tool call for ' in str(caught.value)
        with locks.try_path_lock(session, 'file.txt') as free:
            assert free is False
        with locks.path_locks(session, ['other.txt']):
            pass
    finally:
        child.stdin.close()
        assert child.wait(30) == 0
    with locks.try_path_lock(session, 'file.txt') as free:
        assert free is True


def test_multi_file_holders_lock_in_one_order_without_deadlock(tmp_path, monkeypatch):
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 10)
    barrier = threading.Barrier(2)

    def take(paths):
        for _ in range(50):
            with locks.path_locks(session, paths):
                pass
        barrier.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        forward = pool.submit(take, ['a.txt', 'b.txt', 'c.txt'])
        backward = pool.submit(take, ['c.txt', 'b.txt', 'a.txt'])
        forward.result(timeout=30)
        backward.result(timeout=30)
    assert not locks._locals
