"""Project journal without project-wide admission (owner decision 2026-11-10).

Writers of different files never wait for each other; one file's writers
serialize; a commit appends to the log, or publishes a record file while the
rewrite lock is busy; rewrites (folds, undo) lose no
concurrent record; recovery and inspection treat a held file lock as a live
writer of that file only. See "Per-file journal locks" in
docs/PROJECT_FILE_UNDO.md.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading

import pytest

from lib import file_history
from lib.project_mod import modifications as mods, write_intents
from lib.project_mod.effect_reconciliation import inspect_prepared_write, reference_for
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_LIMIT = write_intents._limit


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    base.mkdir()
    for name in ('first.txt', 'second.txt', 'third.txt'):
        (base / name).write_bytes(b'old\n')
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    limit = write_intents._limit
    monkeypatch.setattr(write_intents, '_limit',
                        lambda name: 0.3 if name == 'lockWaitSeconds' else limit(name))
    return str(base)


def write(base, name, content, task):
    target = os.path.join(base, name)
    original = Path(target).read_bytes() if os.path.exists(target) else None
    _ops._write_with_undo(target, content.encode(), base, name, original,
                          conv_id='conv', task_id=task, tracked=True,
                          attribution=(base, name))


@pytest.fixture
def stall(monkeypatch):
    """Stop the commit of one file (inside its journal hold) until released."""
    entered, release = threading.Event(), threading.Event()
    stalled = {}
    commit = mods._commit_record

    def stalling(session_dir, record, change_id=None):
        if record['path'] == stalled.get('path'):
            entered.set()
            assert release.wait(30)
        return commit(session_dir, record, change_id)

    monkeypatch.setattr(mods, '_commit_record', stalling)

    def arm(path):
        stalled['path'] = path
        return entered, release
    return arm


def rows(base):
    return mods.get_modifications(base, include_content=False)


def test_stalled_writer_of_one_file_does_not_delay_another_file(project, stall):
    entered, release = stall('first.txt')
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(write, project, 'first.txt', 'A\n', 'task-a')
        try:
            assert entered.wait(10)
            # A wait for the stalled file would be refused after 0.3 s.
            write(project, 'second.txt', 'B\n', 'task-b')
        finally:
            release.set()
        first.result(timeout=10)
    assert sorted(row['path'] for row in rows(project)) == ['first.txt', 'second.txt']


def test_same_file_writers_serialize_and_busy_names_the_file(project, stall):
    from lib.project_mod.journal_locks import JournalLockBusy
    entered, release = stall('first.txt')
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(write, project, 'first.txt', 'one\n', 'task-1')
        try:
            assert entered.wait(10)
            with pytest.raises(JournalLockBusy) as busy:
                write(project, 'first.txt', 'two\n', 'task-2')
            assert 'file first.txt' in str(busy.value)
            assert 'no file mutation was performed' in str(busy.value)
            assert Path(project, 'first.txt').read_bytes() != b'two\n'
        finally:
            release.set()
        first.result(timeout=10)
    write(project, 'first.txt', 'two\n', 'task-2')
    history = [row['taskId'] for row in rows(project) if row['path'] == 'first.txt']
    assert history == ['task-1', 'task-2']
    assert mods.undo_task_modifications(project, 'task-2')['undone'] == 1
    assert Path(project, 'first.txt').read_bytes() == b'one\n'
    assert mods.undo_task_modifications(project, 'task-1')['undone'] == 1
    assert Path(project, 'first.txt').read_bytes() == b'old\n'


_CHILD = textwrap.dedent('''
    import os, sys
    from pathlib import Path
    from lib import file_history
    from lib.project_mod import modifications as mods
    from lib.project_mod.write_tools import _ops
    sessions, base, prefix, count, mode = sys.argv[1:6]
    mods.SESSIONS_DIR = sessions
    file_history.track_edit = lambda *a, **kw: None
    if mode == 'kill-before-commit':
        def die(*a, **kw):
            os.kill(os.getpid(), 9)
        mods._commit_record = die
    for index in range(int(count)):
        name = f'{prefix}{index}.txt'
        target = os.path.join(base, name)
        original = Path(target).read_bytes() if os.path.exists(target) else None
        _ops._write_with_undo(target, f'{prefix}{index}\\n'.encode(), base, name, original,
                              conv_id='conv', task_id=f'{prefix}{index}', tracked=True,
                              attribution=(base, name))
''')


def child(project, prefix, count, mode='write'):
    environment = {**os.environ, 'PYTHONPATH': str(ROOT)}
    return subprocess.Popen([sys.executable, '-c', _CHILD, mods.SESSIONS_DIR, project,
                             prefix, str(count), mode], env=environment, cwd=str(ROOT))


def test_threads_and_another_process_commit_every_record_exactly_once(project):
    process = child(project, 'proc', 12)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(write, project, f't{thread}-{index}.txt', f'{thread}{index}\n',
                               f't{thread}-{index}')
                   for thread in range(4) for index in range(6)]
        for future in futures:
            future.result(timeout=60)
    assert process.wait(timeout=120) == 0
    history = rows(project)
    change_ids = [row['changeId'] for row in history]
    assert len(change_ids) == len(set(change_ids)) == 36
    assert {row['path'] for row in history} == (
        {f't{t}-{i}.txt' for t in range(4) for i in range(6)} | {f'proc{i}.txt' for i in range(12)})
    # A fresh reader (no process cache) sees the same history.
    mods._invalidate_cache(mods._get_session_dir(project))
    assert [row['changeId'] for row in rows(project)] == change_ids
    assert mods.undo_task_modifications(project, 'proc3')['undone'] == 1
    assert not Path(project, 'proc3.txt').exists()
    assert mods.undo_task_modifications(project, 't2-5')['undone'] == 1
    assert not Path(project, 't2-5.txt').exists()
    assert len(rows(project)) == 34


def test_concurrent_folds_and_undo_lose_no_record(project, monkeypatch):
    # Folds and undo serialize among themselves; give them the contract budget.
    monkeypatch.setattr(write_intents, '_limit', _CONTRACT_LIMIT)
    session = mods._get_session_dir(project)
    for index in range(4):
        write(project, f'undo{index}.txt', 'u\n', 'undo-task')
    stop = threading.Event()
    failures = []

    def rewrite():
        while not stop.is_set():
            try:
                mods._locked_rmw(session, lambda history: None)
            except Exception as error:  # pragma: no cover - reported below
                failures.append(error)

    with ThreadPoolExecutor(max_workers=6) as pool:
        folder = pool.submit(rewrite)
        writers = [pool.submit(write, project, f'w{thread}-{index}.txt', 'w\n', f'w{thread}')
                   for thread in range(4) for index in range(8)]
        undo = mods.undo_task_modifications(project, 'undo-task')
        for future in writers:
            future.result(timeout=60)
        stop.set()
        folder.result(timeout=60)
    assert not failures
    assert undo['undone'] == 4
    mods._invalidate_cache(session)
    history = rows(project)
    assert sorted(row['path'] for row in history) == sorted(
        f'w{thread}-{index}.txt' for thread in range(4) for index in range(8))
    assert len({row['changeId'] for row in history}) == 32


def test_killed_writer_is_recovered_while_live_writer_of_other_file_is_untouched(project, stall):
    session = mods._get_session_dir(project)
    entered, release = stall('first.txt')
    with ThreadPoolExecutor(max_workers=1) as pool:
        live = pool.submit(write, project, 'first.txt', 'live\n', 'task-live')
        try:
            assert entered.wait(10)
            process = child(project, 'crash', 1, 'kill-before-commit')
            assert process.wait(timeout=60) == -signal.SIGKILL
            assert Path(project, 'crash0.txt').read_bytes() == b'crash0\n'
            intents = Path(session, 'write-intents-v1')
            assert {json.loads(p.read_text())['path'] for p in intents.glob('*.json')} == {
                'first.txt', 'crash0.txt'}
            history = rows(project)
            assert [row['path'] for row in history] == ['crash0.txt']
            assert [json.loads(p.read_text())['path'] for p in intents.glob('*.json')] == [
                'first.txt']
        finally:
            release.set()
        live.result(timeout=10)
    assert sorted(row['path'] for row in rows(project)) == ['crash0.txt', 'first.txt']
    assert not list(Path(session, 'write-intents-v1').glob('*.json'))
    assert mods.undo_task_modifications(project, 'crash0')['undone'] == 1
    assert not Path(project, 'crash0.txt').exists()


def test_writer_active_only_for_a_live_writer_of_that_path(project, stall):
    session = mods._get_session_dir(project)
    write(project, 'second.txt', 'done\n', 'task-done')
    done = rows(project)[0]
    image = done['postImage']
    done_reference = {'kind': 'project_file_write', 'changeId': done['changeId'],
                      'basePath': done['basePath'], 'path': 'second.txt', 'taskId': 'task-done',
                      'rootIdentity': image['root'], 'postDigest': image['digest'],
                      'postBytes': image['bytes']}
    entered, release = stall('first.txt')
    with ThreadPoolExecutor(max_workers=1) as pool:
        live = pool.submit(write, project, 'first.txt', 'live\n', 'task-live')
        try:
            assert entered.wait(10)
            intent = json.loads(next(Path(session, 'write-intents-v1').glob('*.json')).read_text())
            assert inspect_prepared_write(reference_for(intent), conversation_id='conv') == (
                'unknown', 'writer_active')
            assert inspect_prepared_write(done_reference, conversation_id='conv') == (
                'file_published', 'undo_committed')
        finally:
            release.set()
        live.result(timeout=10)
    assert inspect_prepared_write(reference_for(intent), conversation_id='conv') == (
        'file_published', 'undo_committed')


def test_legacy_index_and_append_log_load_identically(project):
    session = mods._get_session_dir(project)
    legacy = [{'changeId': f'{index:032x}', 'type': 'write_file', 'path': f'legacy{index}.txt',
               'basePath': project, 'convId': 'conv', 'taskId': 'legacy', 'existed': False,
               'postImage': {'kind': 'unavailable'}, 'timestamp': index} for index in range(5)]
    Path(session, 'modifications.json').write_text(json.dumps({'modifications': legacy[:3]}))
    Path(session, 'modifications.journal').write_bytes(
        b''.join(json.dumps(row).encode() + b'\n' for row in legacy[3:]) + b'{"torn')
    assert mods._load_from_disk(session) == legacy
    assert rows(project) == legacy
    write(project, 'first.txt', 'new\n', 'task-new')
    history = rows(project)
    assert history[:5] == legacy
    assert [row['path'] for row in history[5:]] == ['first.txt']
    # A whole-history rewrite absorbs the legacy log without changing history.
    mods._locked_rmw(session, lambda history: None)
    mods._invalidate_cache(session)
    assert [row['changeId'] for row in rows(project)] == [row['changeId'] for row in history]
    assert not Path(session, 'modifications.journal').exists()
