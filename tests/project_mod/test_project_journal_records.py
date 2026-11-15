"""A commit costs one row, never waits for another file, and stays crash-safe.

Problem: every writer of a project shared one journal admission, so one slow
writer stalled writes of unrelated files (owner decision 2026-11-10 removed it).
Decision: under its file's lock a commit takes the rewrite lock without waiting
and appends its row to `modifications.journal`; if a fold or undo holds it, the
row is published as `records/<key>-<changeId>.json` instead, and the next
appending writer moves it into the log. Rewrites record the record names they
absorbed and only then unlink them. Readers replay index + fold artifact + log
+ unabsorbed records, ordered by commit key.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading

import pytest

from lib.project_mod import journal_locks, modifications as mods

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(mods, '_JOURNAL_VALIDATED', {})
    monkeypatch.setattr(mods, '_RECORD_ROWS', {})
    mods._mods_cache.clear()
    mods._mods_cache_identity.clear()
    mods._mods_cache_bytes.clear()
    mods._state.update(sessionId=None, modifications=[])
    yield


def session_of(root):
    return mods._get_session_dir(str(root))


def index_path(session):
    return Path(session, 'modifications.json')


def tail_path(session):
    return Path(session, 'modifications.journal')


def records_of(session):
    return sorted(Path(session, 'records').glob('*.json'))


@contextmanager
def rewrite_busy(session):
    """Hold the rewrite lock in another thread, as a running fold or undo does."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with journal_locks.rewrite_lock(session):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(10)
    try:
        yield
    finally:
        release.set()
        thread.join(10)


def record(path, content='', change_id=None):
    row = {'type': 'write_file', 'path': path, 'existed': True}
    if content:
        row['originalContent'] = content
    if change_id:
        row['changeId'] = change_id
    return row


def test_commits_append_without_rewriting_the_index(tmp_path):
    session = session_of(tmp_path)
    mods._flush_to_disk(session, [record('first')])
    published = index_path(session).read_bytes()
    stamp = index_path(session).stat().st_mtime_ns
    for name in ('a.txt', 'b.txt', 'c.txt'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
        assert index_path(session).read_bytes() == published
        assert index_path(session).stat().st_mtime_ns == stamp
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == [
        'first', 'a.txt', 'b.txt', 'c.txt']
    written = [json.loads(line)['path'] for line in tail_path(session).read_text().splitlines()]
    assert written == ['a.txt', 'b.txt', 'c.txt']
    assert records_of(session) == []


def test_commit_during_a_rewrite_is_a_record_file_moved_into_the_log_later(tmp_path):
    session = session_of(tmp_path)
    assert mods._record_modification(str(tmp_path), 'write_file', 'before') is True
    with rewrite_busy(session):
        for name in ('during-1', 'during-2'):
            assert mods._record_modification(str(tmp_path), 'write_file', name) is True
        written = [json.loads(path.read_text()) for path in records_of(session)]
        assert [row['path'] for row in written] == ['during-1', 'during-2']
        for path, row in zip(records_of(session), written):
            assert path.name == f"{row['journalKey']}-{row['changeId']}.json"
        assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == [
            'before', 'during-1', 'during-2']
    assert mods._record_modification(str(tmp_path), 'write_file', 'after') is True
    assert records_of(session) == []
    logged = [json.loads(line)['path'] for line in tail_path(session).read_text().splitlines()]
    assert sorted(logged) == ['after', 'before', 'during-1', 'during-2']
    mods._mods_cache.clear()
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == [
        'before', 'during-1', 'during-2', 'after']


def test_a_logged_record_left_by_a_crash_is_read_once_and_reclaimed(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    with rewrite_busy(session):
        assert mods._record_modification(str(tmp_path), 'write_file', 'waiting') is True
    unlink = os.unlink

    def crash(path, *args, **kwargs):
        if '/records/' in str(path):
            raise OSError('injected crash before unlinking a logged record')
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mods.os, 'unlink', crash)
        assert mods._record_modification(str(tmp_path), 'write_file', 'next') is True
    assert len(records_of(session)) == 1
    mods._mods_cache.clear()
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == ['waiting', 'next']
    assert mods._record_modification(str(tmp_path), 'write_file', 'last') is True
    assert records_of(session) == []
    logged = [json.loads(line)['path'] for line in tail_path(session).read_text().splitlines()]
    assert logged == ['waiting', 'next', 'last']


def test_commit_work_does_not_scale_with_history_size(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    mods._flush_to_disk(session, [record(f'old-{index}') for index in range(500)])
    index_before = index_path(session).stat().st_size
    folds = []
    fold = mods._flush_to_disk
    monkeypatch.setattr(mods, '_flush_to_disk',
                        lambda *a, **kw: (folds.append(a[0]), fold(*a, **kw))[1])
    for index in range(10):
        assert mods._record_modification(str(tmp_path), 'write_file', f'new-{index}') is True
    assert folds == []
    assert index_path(session).stat().st_size == index_before
    assert len(mods.get_modifications(str(tmp_path))) == 510


def test_one_files_commit_order_is_exact_even_if_the_clock_goes_back(tmp_path, monkeypatch):
    clock = iter([5_000, 4_000, 3_000, 2_000])
    monkeypatch.setattr(journal_locks.time, 'time_ns', lambda: next(clock))
    for task in ('one', 'two', 'three', 'four'):
        assert mods._record_modification(str(tmp_path), 'write_file', 'same', task_id=task)
    mods._mods_cache.clear()
    assert [row['taskId'] for row in mods.get_modifications(str(tmp_path))] == [
        'one', 'two', 'three', 'four']


def test_torn_final_append_is_not_trusted_and_later_commits_follow_it(tmp_path):
    session = session_of(tmp_path)
    tail_path(session).write_bytes(
        b'{"type":"write_file","path":"committed","existed":true}\n'
        b'{"type":"write_file","path":"half-writ')
    assert [row['path'] for row in mods._load_from_disk(session)] == ['committed']
    assert mods._record_modification(str(tmp_path), 'write_file', 'after-crash') is True
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == [
        'committed', 'after-crash']
    assert tail_path(session).read_bytes().endswith(b'\n')


def test_failed_append_is_rolled_back_and_never_acknowledged(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    for name in ('first', 'second'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
    original = mods.get_modifications(str(tmp_path))
    before = tail_path(session).read_bytes()
    fsync = os.fsync

    def fail(descriptor):
        if os.readlink(f'/proc/self/fd/{descriptor}').endswith('modifications.journal'):
            raise OSError('injected disk full')
        return fsync(descriptor)

    monkeypatch.setattr(mods.os, 'fsync', fail)
    assert mods._record_modification(str(tmp_path), 'write_file', 'third') is False
    assert tail_path(session).read_bytes() == before
    mods._mods_cache.clear()
    assert mods.get_modifications(str(tmp_path)) == original


@pytest.mark.parametrize('failure', ['fsync', 'replace'])
def test_failed_record_publication_is_never_acknowledged(tmp_path, monkeypatch, failure):
    session = session_of(tmp_path)
    for name in ('first', 'second'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
    original = mods.get_modifications(str(tmp_path))
    before = records_of(session)
    real = getattr(mods.os, failure)

    def fail(*args, **kwargs):
        target = (os.readlink(f'/proc/self/fd/{args[0]}') if failure == 'fsync'
                  else str(args[1]))
        if '/records/' in target and target.endswith(('.json', '.tmp')):
            raise OSError('injected disk failure')
        return real(*args, **kwargs)

    with monkeypatch.context() as patch, rewrite_busy(session):
        patch.setattr(mods.os, failure, fail)
        assert mods._record_modification(str(tmp_path), 'write_file', 'third') is False
    assert records_of(session) == before
    assert not list(Path(session, 'records').glob('.record-*'))
    mods._mods_cache.clear()
    assert mods.get_modifications(str(tmp_path)) == original


def test_bounded_log_folds_into_the_index_and_keeps_order(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    monkeypatch.setattr(mods, '_JOURNAL_TAIL_MAX_BYTES', 1200)
    names = [f'f-{index}' for index in range(20)]
    sizes = []
    for index, name in enumerate(names):
        if index % 5 == 4:
            with rewrite_busy(session):
                assert mods._record_modification(str(tmp_path), 'write_file', name) is True
        else:
            assert mods._record_modification(str(tmp_path), 'write_file', name) is True
        sizes.append(tail_path(session).stat().st_size if tail_path(session).exists() else 0)
    # The log is folded once it outgrows its bound; the index absorbs it and
    # readers still see every record in commit order.
    assert max(sizes) <= 1200
    assert json.loads(index_path(session).read_text())['modifications']
    mods._mods_cache.clear()
    assert [row['path'] for row in mods.get_modifications(str(tmp_path))] == names


def test_crash_before_rewrite_publication_keeps_every_record(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    with rewrite_busy(session):
        for name in ('a', 'b'):
            assert mods._record_modification(str(tmp_path), 'write_file', name) is True
    write = mods._atomic_json_write

    def crash(path, data):
        if str(path) == str(index_path(session)):
            raise OSError('injected crash before index publication')
        return write(path, data)

    monkeypatch.setattr(mods, '_atomic_json_write', crash)
    with pytest.raises(OSError):
        mods._locked_rmw(session, lambda rows: [])
    assert len(records_of(session)) == 2
    assert [row['path'] for row in mods._load_from_disk(session)] == ['a', 'b']


def test_crash_after_rewrite_publication_does_not_resurrect_undone_records(tmp_path, monkeypatch):
    session = session_of(tmp_path)
    with rewrite_busy(session):
        for task in ('task-a', 'task-b'):
            (tmp_path / f'{task}.txt').write_text(f'created by {task}\n')
            assert mods._record_modification(
                str(tmp_path), 'write_file', f'{task}.txt', task_id=task) is True
    unlink = os.unlink

    def crash(path, *args, **kwargs):
        if str(path).endswith('.json') and '/records/' in str(path):
            raise OSError('injected crash before unlinking absorbed records')
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mods.os, 'unlink', crash)
        with pytest.raises(OSError):
            mods.undo_task_modifications(str(tmp_path), 'task-b')
    assert not (tmp_path / 'task-b.txt').exists()
    assert len(records_of(session)) == 2  # Leftovers, recorded as absorbed.
    mods._mods_cache.clear()
    assert [row['taskId'] for row in mods.get_modifications(str(tmp_path))] == ['task-a']
    # The next rewrite reclaims the leftovers.
    mods._locked_rmw(session, lambda rows: None)
    assert records_of(session) == []
    assert [row['taskId'] for row in mods.get_modifications(str(tmp_path))] == ['task-a']


def legacy_rows(*names):
    return [{'type': 'write_file', 'path': name, 'existed': True} for name in names]


def test_legacy_fold_artifact_is_replayed_until_the_index_absorbs_it(tmp_path):
    session = session_of(tmp_path)
    mods._atomic_json_write(str(index_path(session)), {'modifications': legacy_rows('folded')})
    fold = Path(session, 'modifications.journal.fold')
    fold.write_text(json.dumps(legacy_rows('appended')[0]) + '\n')
    assert [row['path'] for row in mods._load_from_disk(session)] == ['folded', 'appended']
    envelope = {'modifications': legacy_rows('folded', 'appended'),
                'absorbedFold': list(mods._fold_identity(session))}
    mods._atomic_json_write(str(index_path(session)), envelope)
    assert [row['path'] for row in mods._load_from_disk(session)] == ['folded', 'appended']


def test_undo_after_an_interrupted_legacy_fold_does_not_resurrect_undone_changes(tmp_path):
    session = session_of(tmp_path)
    for task in ('task-a', 'task-b'):
        (tmp_path / f'{task}.txt').write_text(f'created by {task}\n')
    rows = [{**legacy_rows(f'{task}.txt')[0], 'existed': False, 'taskId': task,
             'changeId': task[-1] * 32, 'basePath': str(tmp_path),
             'postImage': None} for task in ('task-a', 'task-b')]
    for row in rows:
        row.pop('postImage')
    mods._atomic_json_write(str(index_path(session)), {'modifications': rows[:1]})
    Path(session, 'modifications.journal.fold').write_text(json.dumps(rows[1]) + '\n')
    mods._locked_rmw(session, lambda history: [r for r in history if r['taskId'] != 'task-b'])
    mods._mods_cache.clear()
    assert [row['taskId'] for row in mods.get_modifications(str(tmp_path))] == ['task-a']
    assert not Path(session, 'modifications.journal.fold').exists()


def test_repeated_change_identity_is_idempotent_across_records_and_index(tmp_path):
    session = session_of(tmp_path)
    identity = 'a' * 32
    assert mods._commit_record(session, record('once', 'body', identity), identity) is True
    assert mods._commit_record(session, record('once', 'body', identity), identity) is False
    mods._locked_rmw(session, lambda rows: None)  # Now an index row.
    assert mods._commit_record(session, record('once', 'body', identity), identity) is False
    with pytest.raises(mods._RecoveryIdentityConflict):
        mods._commit_record(session, record('once', 'other', identity), identity)
    assert [row['path'] for row in mods._load_from_disk(session)] == ['once']


def test_undo_compaction_removes_every_journal_component(tmp_path):
    session = session_of(tmp_path)
    tail_path(session).write_text(json.dumps(legacy_rows('legacy')[0]) + '\n')
    for name in ('a', 'b'):
        assert mods._record_modification(str(tmp_path), 'write_file', name) is True
    mods._locked_rmw(session, lambda rows: [])
    assert not index_path(session).exists()
    assert not tail_path(session).exists()
    assert not Path(session, 'modifications.journal.fold').exists()
    assert records_of(session) == []
    assert mods.get_modifications(str(tmp_path)) == []


def test_edit_validation_is_by_identity_instead_of_reparsing(tmp_path, monkeypatch):
    """A single-file edit must not pay for the whole journal on every call."""
    session = session_of(tmp_path)
    mods._flush_to_disk(session, [record(f'old-{index}') for index in range(300)])
    mods._invalidate_cache(session)
    loads = []
    load = mods._read_history
    monkeypatch.setattr(mods, '_read_history', lambda s: (loads.append(s), load(s))[1])
    mods._assert_journal_ready(session)
    validated = len(loads)  # One validation (plus this process's one crash-artifact sweep).
    for _ in range(20):
        mods._assert_journal_ready(session)
    assert len(loads) == validated <= 2
    # A commit by this or another process adds a record without a re-parse.
    assert mods._record_modification(str(tmp_path), 'write_file', 'next') is True
    for _ in range(5):
        mods._assert_journal_ready(session)
    assert len(loads) == validated
    assert len(mods.get_modifications(str(tmp_path))) == 301


def test_concurrent_unrelated_file_commits_never_wait(tmp_path, monkeypatch):
    """The reported failure: a second writer timed out on an unrelated file."""
    from concurrent.futures import ThreadPoolExecutor

    base = str(tmp_path)
    session = session_of(tmp_path)
    mods._flush_to_disk(session, [record(f'history-{index}') for index in range(400)])
    mods._invalidate_cache(session)
    # No waiting budget at all: a commit that had to wait would be refused.
    monkeypatch.setattr(journal_locks, '_wait_seconds', lambda: 0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(
            lambda index: mods._record_modification(
                base, 'write_file', f'parallel-{index}'), range(16)))
    assert all(outcomes)
    assert len(mods.get_modifications(base)) == 416
