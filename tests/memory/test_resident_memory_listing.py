"""Executable specification of the resident memory listing.

Startup memory prefetch reads its corpus from per-directory snapshots held by
``lib/memory/storage/_resident_listing.py`` so that no walk of a store
directory runs on the startup path. The freshness rules under test:

1. a repository write of this process is listed by every later read;
2. any other change is listed once a refresh that started after it completed,
   and every read or write requests one, without waiting for it;
3. a process without a snapshot loads the persisted records plus its own
   writes, and walks only when there are none or it dropped its snapshot;
4. a walk keeps a record unread only while its file keeps the fingerprint of
   a settled read this process made and the record has no runtime
   requirement, so the first refresh after a load reads every file.
Throughout, the rows equal what the exact listing returns for the same files.
"""

from __future__ import annotations

import builtins
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit

_WAIT_SECONDS = 10.0


class _Refreshes:
    """Background refreshes that the test runs explicitly, in request order."""

    def __init__(self) -> None:
        self.pending = []

    def submit(self, function, *args):
        self.pending.append((function, args))

    def run(self) -> None:
        while self.pending:
            function, args = self.pending.pop(0)
            function(*args)


def _write(directory: Path, memory_id: str, description: str, **front) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    lines = ['---', f'name: {memory_id}', f'description: {description}']
    lines += [f'{key}: {value}' for key, value in front.items()]
    lines += ['---', '', f'body of {memory_id}', '']
    path = directory / f'{memory_id}.md'
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def _write_package(directory: Path, package_id: str, description: str) -> Path:
    package = directory / package_id
    package.mkdir(parents=True, exist_ok=True)
    (package / 'SKILL.md').write_text(
        f'---\nname: {package_id}\ndescription: {description}\n---\n\nguide\n',
        encoding='utf-8')
    return package


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import lib.memory.storage._crud as crud
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files
    import lib.memory.storage._resident_listing as resident

    data_dir = tmp_path / 'data'
    root = tmp_path / 'project'
    extra = tmp_path / 'extra'
    (root / '.tofu' / 'memories').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_store_migrated', True)
    for migrated in (str(root), str(extra)):
        storage_dirs._migrated_roots.add(migrated)
    refreshes = _Refreshes()
    monkeypatch.setattr(resident, '_submit_refresh', refreshes.submit)
    listing = resident.ResidentMemoryListing()
    monkeypatch.setattr(crud, 'resident_listing', listing)
    memory_files._metadata_cache.clear()
    yield SimpleNamespace(
        root=root, extra=extra, data=data_dir,
        memories=root / '.tofu' / 'memories',
        global_memories=data_dir / 'memories' / 'global',
        refreshes=refreshes, listing=listing, resident=resident, crud=crud)
    memory_files._metadata_cache.clear()
    for migrated in (str(root), str(extra)):
        storage_dirs._migrated_roots.discard(migrated)


def _read(store, extra_paths=()):
    from lib.memory.storage import resident_eligible_memories

    details = {}
    rows = resident_eligible_memories(
        str(store.root), list(extra_paths), details=details)
    return rows, details


def _exact(store, extra_paths=()):
    from lib.memory.storage import get_eligible_memories

    return get_eligible_memories(
        str(store.root), extra_paths=list(extra_paths), include_body=False,
        record_view='retrieval')


def _ids(rows):
    return [row['id'] for row in rows]


def _restart(store, monkeypatch):
    """A new process: no snapshots, a cold frontmatter cache, same data root."""
    import lib.memory.storage._files as memory_files

    listing = store.resident.ResidentMemoryListing()
    monkeypatch.setattr(store.crud, 'resident_listing', listing)
    memory_files._metadata_cache.clear()
    store.refreshes.pending.clear()
    store.listing = listing
    return listing


# ── Reads ────────────────────────────────────────────────────────────

def test_rows_equal_the_exact_listing_across_stores_and_roots(store):
    _write(store.global_memories, 'shared-note', 'global copy wins over project')
    _write(store.memories, 'shared-note', 'project copy is shadowed')
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _write(store.memories, 'disabled-rule', 'turned off', enabled='false')
    _write(store.memories, 'needs-env', 'gated by an unset variable',
           requires_env='[TOFU_TEST_UNSET_VARIABLE_FOR_MEMORY]')
    _write_package(store.memories, 'packaged-guide', 'a package claims its ID')
    _write(store.memories, 'packaged-guide-note', 'flat note beside a package')
    _write(store.extra / '.tofu' / 'memories', 'parser-rule',
           'extra root copy is shadowed')
    _write(store.extra / '.tofu' / 'memories', 'extra-only', 'only in extra')

    rows, details = _read(store, [str(store.extra)])

    assert rows == _exact(store, [str(store.extra)])
    assert _ids(rows) == ['shared-note', 'packaged-guide-note', 'parser-rule',
                          'extra-only']
    assert details['listing'] == 'walked'


def test_a_held_directory_is_read_without_filesystem_io(store, monkeypatch):
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _read(store)
    store.refreshes.run()

    def no_io(*args, **kwargs):
        raise AssertionError('a resident read touched the filesystem')

    for name in ('scandir', 'lstat', 'stat', 'listdir'):
        monkeypatch.setattr(os, name, no_io)
    monkeypatch.setattr(builtins, 'open', no_io)
    rows, details = _read(store)
    monkeypatch.undo()

    assert _ids(rows) == ['parser-rule']
    assert details['listing'] == 'resident'
    assert details['snapshotAgeMs'] >= 0
    # The read requested a refresh; it did not wait for one.
    assert store.refreshes.pending


def test_rows_handed_out_are_never_mutated_by_a_later_write(store):
    from lib.memory.storage import update_memory

    _write(store.memories, 'parser-rule', 'ParserState rule')
    before, _ = _read(store)

    update_memory('parser-rule', {'description': 'ParserState rule, revised'},
                  project_path=str(store.root))
    after, _ = _read(store)

    assert before[0]['description'] == 'ParserState rule'
    assert after[0]['description'] == 'ParserState rule, revised'


# ── Rule 1: repository writes ────────────────────────────────────────

def _create(store):
    from lib.memory.storage import create_memory

    create_memory(name='Fresh Note', description='written through the repository',
                  scope='project', project_path=str(store.root))


def _create_global(store):
    from lib.memory.storage import create_memory

    create_memory(name='Global Note', description='a global repository write',
                  scope='global', project_path=str(store.root))


def _update(store):
    from lib.memory.storage import update_memory

    update_memory('parser-rule', {'description': 'ParserState rule, revised',
                                  'tags': ['parser', 'revised']},
                  project_path=str(store.root))


def _disable(store):
    from lib.memory.storage import toggle_memory

    toggle_memory('parser-rule', enabled=False, project_path=str(store.root))


def _disable_package(store):
    from lib.memory.storage import toggle_memory

    toggle_memory('packaged-guide', enabled=False, project_path=str(store.root))


def _delete(store):
    from lib.memory.storage import delete_memory

    assert delete_memory('parser-rule', project_path=str(store.root))


def _merge(store):
    from lib.memory.storage import merge_memories

    merge_memories(['parser-rule', 'format-rule'], name='Merged Rule',
                   description='one rule replacing two', body='merged',
                   project_path=str(store.root))


def _clear(store):
    from lib.memory.storage import clear_memories

    clear_memories(project_path=str(store.root))


@pytest.mark.parametrize('write', [
    _create, _create_global, _update, _disable, _disable_package, _delete,
    _merge, _clear,
], ids=lambda write: write.__name__.strip('_'))
def test_a_repository_write_is_listed_by_the_next_read(store, write):
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _write(store.memories, 'format-rule', 'formatting rule')
    _write_package(store.memories, 'packaged-guide', 'a package')
    _read(store)
    store.refreshes.run()

    write(store)
    rows, details = _read(store)

    assert details['listing'] == 'resident'
    assert rows == _exact(store)


def test_a_repository_write_during_a_refresh_survives_its_publication(
        store, monkeypatch):
    """The refresh walked before the write; its result must not drop it."""
    from lib.memory.storage import create_memory

    _write(store.memories, 'parser-rule', 'ParserState rule')
    _read(store)
    store.refreshes.run()
    walked, release = threading.Event(), threading.Event()
    original = store.resident._list_entries_in_dir

    def walk_then_wait(directory, scope, previous=None, **kwargs):
        records = original(directory, scope, previous, **kwargs)
        if os.path.normpath(directory) == os.path.normpath(str(store.memories)):
            walked.set()
            assert release.wait(_WAIT_SECONDS)
        return records

    monkeypatch.setattr(store.resident, '_list_entries_in_dir', walk_then_wait)
    state = store.listing._directories[
        store.resident._key(str(store.memories), 'project')]
    with store.listing._lock:
        assert store.listing._request_refresh_locked(state)
    refresh = threading.Thread(target=store.listing._refresh, args=(state,),
                               daemon=True)
    refresh.start()
    assert walked.wait(_WAIT_SECONDS)

    written = create_memory(name='Written Mid Refresh', description='during a walk',
                            scope='project', project_path=str(store.root))
    release.set()
    refresh.join(_WAIT_SECONDS)
    store.refreshes.run()

    rows, _ = _read(store)
    assert written['id'] in _ids(rows)
    assert rows == _exact(store)


# ── Rule 2: every other change ───────────────────────────────────────

def _external_create(store):
    _write(store.memories, 'external-note', 'written by another process')


def _external_edit_in_place(store):
    path = store.memories / 'parser-rule.md'
    with open(path, 'r+', encoding='utf-8') as handle:
        text = handle.read()
        handle.seek(0)
        handle.write(text.replace('ParserState rule', 'ParserState edit!'))


def _external_delete(store):
    (store.memories / 'parser-rule.md').unlink()


@pytest.mark.parametrize('change', [
    _external_create, _external_edit_in_place, _external_delete,
], ids=lambda change: change.__name__.strip('_'))
def test_another_change_is_listed_after_the_next_completed_refresh(store, change):
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _read(store)
    store.refreshes.run()
    listed = _read(store)[0]

    change(store)
    stale, _ = _read(store)
    assert stale == listed  # the read did not wait for its refresh
    store.refreshes.run()
    fresh, _ = _read(store)

    assert fresh == _exact(store)
    assert fresh != listed


def test_a_refresh_requested_while_one_runs_starts_after_it(store, monkeypatch):
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _read(store)
    store.refreshes.run()
    walked, release = threading.Event(), threading.Event()
    original = store.resident._list_entries_in_dir
    walks = []

    def counted(directory, scope, previous=None, **kwargs):
        records = original(directory, scope, previous, **kwargs)
        if os.path.normpath(directory) == os.path.normpath(str(store.memories)):
            walks.append(directory)
            if len(walks) == 1:
                walked.set()
                assert release.wait(_WAIT_SECONDS)
        return records

    monkeypatch.setattr(store.resident, '_list_entries_in_dir', counted)
    state = store.listing._directories[
        store.resident._key(str(store.memories), 'project')]
    with store.listing._lock:
        assert store.listing._request_refresh_locked(state)
    refresh = threading.Thread(target=store.listing._refresh, args=(state,),
                               daemon=True)
    refresh.start()
    assert walked.wait(_WAIT_SECONDS)

    _external_create(store)  # after the running walk read the directory
    rows, _ = _read(store)  # requests a refresh while one runs
    assert 'external-note' not in _ids(rows)
    release.set()
    refresh.join(_WAIT_SECONDS)

    assert len(walks) == 2
    assert not state.refreshing
    assert 'external-note' in _ids(_read(store)[0])


def test_a_failed_refresh_keeps_the_snapshot_and_a_later_one_recovers(
        store, monkeypatch):
    _write(store.memories, 'parser-rule', 'ParserState rule')
    listed, _ = _read(store)
    original = store.resident._walk

    def broken(directory, scope, previous=(), **kwargs):
        raise RuntimeError('volume unavailable')

    monkeypatch.setattr(store.resident, '_walk', broken)
    store.refreshes.run()
    assert _read(store)[0] == listed

    monkeypatch.setattr(store.resident, '_walk', original)
    _external_create(store)
    _read(store)
    store.refreshes.run()
    assert 'external-note' in _ids(_read(store)[0])


# ── Rule 4: what a walk may keep without reading ────────────────

def _settled(monkeypatch):
    """Treat every fingerprint as settled (its timestamps are old enough).

    ``os.utime`` cannot age a file's ctime, so the test removes the window.
    """
    import lib.memory.storage._files as memory_files

    monkeypatch.setattr(memory_files, 'MEMORY_METADATA_FINGERPRINT_SETTLE_NS', 0)


def _count_file_reads(monkeypatch):
    import lib.memory.storage._files as memory_files

    reads = []
    original = memory_files._read_memory_source

    def counted(filepath, **kwargs):
        reads.append(os.path.basename(filepath))
        return original(filepath, **kwargs)

    monkeypatch.setattr(memory_files, '_read_memory_source', counted)
    return reads


def test_a_refresh_reads_only_files_whose_fingerprint_changed(
        store, monkeypatch):
    import lib.memory.storage._files as memory_files

    _settled(monkeypatch)
    for name in ('parser-rule', 'format-rule', 'css-rule'):
        _write(store.memories, name, f'{name} description')
    _read(store)
    store.refreshes.run()
    memory_files._metadata_cache.clear()
    reads = _count_file_reads(monkeypatch)

    _read(store)
    store.refreshes.run()
    assert reads == []

    _write(store.memories, 'format-rule', 'format-rule, edited')
    _read(store)
    store.refreshes.run()
    assert reads == ['format-rule.md']
    assert _read(store)[0] == _exact(store)


def test_an_unsettled_file_is_read_by_every_walk(store, monkeypatch):
    import lib.memory.storage._files as memory_files

    _write(store.memories, 'parser-rule', 'written just now')
    _read(store)
    store.refreshes.run()
    memory_files._metadata_cache.clear()
    reads = _count_file_reads(monkeypatch)

    _read(store)
    store.refreshes.run()

    # A same-size edit inside one timestamp tick keeps the fingerprint, so a
    # fresh file is never kept on its fingerprint alone.
    assert 'parser-rule.md' in reads


def test_a_read_inside_the_settle_window_is_never_cached():
    """The frontmatter cache keeps only text read after its fingerprint settled.

    A same-size edit in the timestamp tick of an earlier read keeps the
    fingerprint; caching that read would let a later lookup return the older
    text, and a walk would keep it.
    """
    from lib.memory.storage._metadata_cache import (
        MEMORY_METADATA_FINGERPRINT_SETTLE_NS as settle,
        MemoryMetadataCache,
    )

    cache = MemoryMetadataCache(max_entries=8, max_bytes=1 << 20)
    written = 1_000 * settle
    fingerprint = (1, 2, 30, written, written)
    cache._clock_ns = lambda: written + 10 * settle

    assert not cache.store('/early', fingerprint, {'name': 'early'},
                           read_started_ns=written + settle - 1)
    assert cache.store('/settled', fingerprint, {'name': 'settled'},
                       read_started_ns=written + settle)
    assert cache.lookup('/early', fingerprint) == (False, {})
    assert cache.lookup('/settled', fingerprint) == (True, {'name': 'settled'})


def test_eligibility_of_a_gated_memory_follows_every_walk(store, monkeypatch):
    variable = 'TOFU_TEST_RESIDENT_LISTING_GATE'
    monkeypatch.delenv(variable, raising=False)
    _settled(monkeypatch)
    _write(store.memories, 'gated-rule', 'needs a variable',
           requires_env=f'[{variable}]')
    assert _ids(_read(store)[0]) == []
    store.refreshes.run()

    monkeypatch.setenv(variable, '1')
    _read(store)
    store.refreshes.run()

    assert _ids(_read(store)[0]) == ['gated-rule']
    assert _read(store)[0] == _exact(store)


# ── Rule 3: a process without a snapshot ─────────────────────────────

def test_a_restarted_process_serves_the_persisted_snapshot_without_walking(
        store, monkeypatch):
    _settled(monkeypatch)
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _write(store.global_memories, 'global-rule', 'a global rule')
    _read(store)
    store.refreshes.run()
    listed = _exact(store)

    _restart(store, monkeypatch)

    def no_walk(directory, scope, previous=(), **kwargs):
        raise AssertionError('the read path walked a persisted directory')

    original = store.resident._walk
    monkeypatch.setattr(store.resident, '_walk', no_walk)
    rows, details = _read(store)
    monkeypatch.setattr(store.resident, '_walk', original)

    assert rows == listed
    assert details['listing'] == 'persisted'
    # Its refresh replaces the persisted snapshot with a current walk.
    _external_create(store)
    store.refreshes.run()
    assert _read(store)[0] == _exact(store)


def _snapshot_file(store) -> Path:
    return Path(store.resident._snapshot_path(
        store.resident._key(str(store.memories), 'project')[0], 'project'))


def test_the_first_refresh_after_a_load_reads_every_persisted_record_again(
        store, monkeypatch):
    """Another process, possibly running other code, derived the records.

    An API host and an execution host can run different revisions, and a
    restart usually follows a code change, so a persisted record is never
    kept on its fingerprint: the first refresh reads every file, and later
    refreshes keep only what this process read.
    """
    _settled(monkeypatch)
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _write(store.memories, 'format-rule', 'formatting rule')
    _read(store)
    store.refreshes.run()
    snapshot = _snapshot_file(store)
    document = json.loads(snapshot.read_text(encoding='utf-8'))
    description = document['keys'].index('description')
    for _name, values in document['entries']:
        values[description] = 'derived by other code'
    snapshot.write_text(json.dumps(document), encoding='utf-8')

    _restart(store, monkeypatch)
    served, _details = _read(store)
    assert {row['description'] for row in served} == {'derived by other code'}

    reads = _count_file_reads(monkeypatch)
    store.refreshes.run()
    assert sorted(reads) == ['format-rule.md', 'parser-rule.md']
    assert _read(store)[0] == _exact(store)

    reads.clear()
    store.refreshes.run()
    assert reads == []


def test_a_write_before_any_read_costs_no_io_and_joins_the_loaded_snapshot(
        store, monkeypatch):
    from lib.memory.storage import create_memory

    _write(store.memories, 'parser-rule', 'ParserState rule')
    _read(store)
    store.refreshes.run()

    _restart(store, monkeypatch)
    reads = _count_file_reads(monkeypatch)
    written = create_memory(name='Before Any Read', description='newer than disk',
                            scope='project', project_path=str(store.root))
    # A process that holds no snapshot (an API host serving the editor)
    # pays nothing for the listing on its write path.
    assert reads == []
    rows, details = _read(store)

    # The persisted snapshot predates the write; the load re-read it.
    assert details['listing'] == 'persisted'
    assert written['id'] in _ids(rows)
    assert rows == _exact(store)


def test_a_write_the_listing_cannot_read_drops_the_snapshot_and_walks(
        store, monkeypatch):
    from lib.memory.storage import create_memory

    _write(store.memories, 'parser-rule', 'ParserState rule')
    _read(store)
    store.refreshes.run()
    original = store.resident._read_named_listed_entry

    def broken(directory, scope, name):
        raise RuntimeError('listing defect')

    monkeypatch.setattr(store.resident, '_read_named_listed_entry', broken)
    written = create_memory(name='Unreadable Here', description='durable anyway',
                            scope='project', project_path=str(store.root))
    monkeypatch.setattr(store.resident, '_read_named_listed_entry', original)
    rows, details = _read(store)

    # The persisted snapshot misses the write, so the read path walks.
    assert details['listing'] == 'walked'
    assert written['id'] in _ids(rows)
    assert rows == _exact(store)


def _corrupt(path: Path) -> None:
    path.write_text('{not json', encoding='utf-8')


def _foreign_directory(path: Path) -> None:
    document = json.loads(path.read_text(encoding='utf-8'))
    document['directory'] = '/elsewhere'
    path.write_text(json.dumps(document), encoding='utf-8')


def _record_outside(path: Path) -> None:
    document = json.loads(path.read_text(encoding='utf-8'))
    keys = document['keys']
    document['entries'][0][1][keys.index('filepath')] = '/etc/passwd'
    path.write_text(json.dumps(document), encoding='utf-8')


@pytest.mark.parametrize('damage', [_corrupt, _foreign_directory, _record_outside],
                         ids=lambda damage: damage.__name__.strip('_'))
def test_an_unusable_persisted_snapshot_is_walked_instead(store, monkeypatch,
                                                          damage):
    _write(store.memories, 'parser-rule', 'ParserState rule')
    _read(store)
    store.refreshes.run()
    snapshot = Path(store.resident._snapshot_path(
        store.resident._key(str(store.memories), 'project')[0], 'project'))
    assert snapshot.is_file()
    damage(snapshot)

    _restart(store, monkeypatch)
    rows, details = _read(store)

    assert rows == _exact(store)
    assert details['listing'] == 'walked'


def test_refresh_requests_reuse_one_resident_daemon_thread():
    import lib.memory.storage._resident_listing as resident

    worker = resident._RefreshWorker()
    ran, done = [], threading.Event()

    def job(index):
        ran.append((index, threading.current_thread()))
        if len(ran) == 6:
            done.set()

    for index in range(3):
        worker.submit(job, index)
    submitters = [threading.Thread(target=worker.submit, args=(job, index))
                  for index in range(3, 6)]
    for submitter in submitters:
        submitter.start()
    for submitter in submitters:
        submitter.join(_WAIT_SECONDS)
    assert done.wait(_WAIT_SECONDS)

    threads = {thread for _index, thread in ran}
    assert len(threads) == 1
    [thread] = threads
    assert thread.daemon and thread is not threading.current_thread()
    assert [index for index, _thread in ran][:3] == [0, 1, 2]


def test_concurrent_first_reads_share_one_load(store, monkeypatch):
    _write(store.memories, 'parser-rule', 'ParserState rule')
    loads = []
    entered, release = threading.Event(), threading.Event()
    original = store.resident._walk

    def slow_walk(directory, scope, previous=(), **kwargs):
        if os.path.normpath(directory) == os.path.normpath(str(store.memories)):
            loads.append(directory)
            entered.set()
            assert release.wait(_WAIT_SECONDS)
        return original(directory, scope, previous, **kwargs)

    monkeypatch.setattr(store.resident, '_walk', slow_walk)
    results = []
    readers = [threading.Thread(target=lambda: results.append(_read(store)[0]),
                                daemon=True) for _ in range(4)]
    for reader in readers:
        reader.start()
    assert entered.wait(_WAIT_SECONDS)
    release.set()
    for reader in readers:
        reader.join(_WAIT_SECONDS)

    assert len(loads) == 1
    assert len(results) == 4
    assert all(_ids(rows) == ['parser-rule'] for rows in results)


# ── The prefetch reads it ────────────────────────────────────────────

def test_prefetch_selects_from_the_resident_listing_like_the_exact_one(
        store, monkeypatch):
    import lib.memory.prefetch._run as prefetch_run

    _write(store.memories, 'parser-state-rollback',
           'ParserState rollback rule in lib/parser/state.py')
    _write(store.memories, 'parser-format', 'ParserState formatting rule')
    _write(store.memories, 'css-palette', 'CSS palette and typography')
    messages = [{'role': 'user', 'content': 'fix ParserState in lib/parser/state.py'}]

    resident = prefetch_run.run_memory_prefetch(
        messages, str(store.root), task={'id': 'resident'})
    monkeypatch.setattr(
        prefetch_run, 'list_prefetch_corpus',
        lambda project_path, extra_paths, details=None: _exact(store))
    exact = prefetch_run.run_memory_prefetch(
        messages, str(store.root), task={'id': 'exact'})

    assert _ids(resident) == ['parser-state-rollback', 'parser-format']
    assert resident == exact
