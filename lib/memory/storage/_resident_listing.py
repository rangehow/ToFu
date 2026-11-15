"""Resident memory listing: the prefetch corpus without a walk on the read path.

Responsibility
--------------
One process-wide owner keeps, for every memory store directory, the
metadata-only retrieval listing of its latest walk (``_list_entries_in_dir``:
the records ``_list_memories_in_dir`` returns, with each flat memory's stat
fingerprint) and answers the startup memory prefetch from those snapshots. A
read does no filesystem I/O for a directory whose snapshot it holds; walks run
on a background thread and re-read only entries whose fingerprint changed.

Freshness (docs/DEFENSIVE_PATTERNS.md, "Startup reads a resident memory
listing"; specs: ``tests/memory/test_resident_memory_listing.py``):

1. A write through this process's memory repository (create, update, toggle,
   delete, merge, clear) is listed by every read that starts after the write
   returns: a held snapshot applies it before the write returns, and the pass
   that loads a directory re-reads every entry this process wrote first.
2. Every read and every repository write requests a refresh: a walk that
   replaces the directory's snapshot when it completes, with this process's
   writes made meanwhile re-applied. A refresh requested while one runs starts
   after it. Any other change (another process, file tools, git, an editor,
   another host) is therefore listed by every read that starts after the
   first refresh of this process that started after the change has
   completed. The read that requests a refresh never waits for it. Staleness
   is bounded by these events, never by a time window.
3. A directory this process holds no snapshot of loads the records that the
   last refresh on this data root persisted (any process), plus rule 1's
   re-reads. Only a directory with no persisted snapshot, or one whose
   snapshot this process had to drop, is walked on the read path, once.
4. A walk keeps an entry without reading its file only while the file keeps
   the stat fingerprint of a read this process made after that fingerprint
   had settled, and only when the record has no runtime requirement; every
   other entry (packages, memories gated on binaries, environment or OS) is
   read again, so eligibility is evaluated by every walk that reads the
   record. Persisted records carry no such evidence: the first refresh after
   a load reads every file, so a record that another process, possibly
   running other code, derived never outlives it.

The prefetch re-reads every selected memory at hydration, so a stale record
can cost recall for one start but never injects content its file lacks.

Entry points: ``resident_listing`` (``eligible`` for reads, ``note_write`` for
the repository writers). Dependencies: the ``_files`` entry listing and
readers and ``_dirs._server_data_dir`` for persisted snapshots under
``<data>/cache/memory-listing/``; refreshes run on the listing's own
resident thread (``_RefreshWorker``).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import threading
import time
from collections import deque

from lib.json_store import write_text_atomic
from lib.log import get_logger

from . import _dirs
from ._files import (
    _MEMORY_RETRIEVAL_RECORD_KEYS,
    ListedMemoryEntry,
    _list_entries_in_dir,
    _read_named_listed_entry,
)

logger = get_logger(__name__)

_SNAPSHOT_SCHEMA = 1
_SNAPSHOT_SUBDIR = os.path.join('cache', 'memory-listing')
# Generations are unique across instances, so a merged-corpus memo never
# matches snapshots it was not built from.
_generations = itertools.count(1)


class _Directory:
    """One store directory's published snapshot and its refresh state."""

    __slots__ = ('path', 'scope', 'entries', 'walked_at', 'generation',
                 'refreshing', 'refresh_queued', 'written', 'walk_required',
                 'persisted')

    def __init__(self, path: str, scope: str) -> None:
        self.path = path
        self.scope = scope
        # ``ListedMemoryEntry`` rows in walk order; ``None`` until loaded.
        self.entries: tuple[ListedMemoryEntry, ...] | None = None
        # Wall clock at which the walk behind ``entries`` started.
        self.walked_at = 0.0
        self.generation = 0
        # A load or refresh pass owns the next publication.
        self.refreshing = False
        self.refresh_queued = False
        # Entries this process wrote that the next publication re-reads:
        # written while a pass ran, or while no snapshot was held.
        self.written: set[str] = set()
        # The snapshot was dropped, so a persisted one may predate this
        # process's earlier writes: the next load walks.
        self.walk_required = False
        # The records this process last read from or wrote to disk.
        self.persisted: tuple[dict, ...] | None = None


def _walk_order(entry: ListedMemoryEntry) -> tuple[bool, str]:
    # ``_entry_readers`` order: flat memories by name, then packages by name.
    return bool(entry.record['is_package']), entry.name


def _with_entries(entries, directory: str, scope: str, names):
    """Return ``entries`` with the entries ``names`` read again from disk."""
    names = set(names)
    kept = [entry for entry in entries if entry.name not in names]
    for name in names:
        entry = _read_named_listed_entry(directory, scope, name)
        if entry is not None:
            kept.append(entry)
    return tuple(sorted(kept, key=_walk_order))


def _walk(directory: str, scope: str, previous=(), *, background=False):
    # A load on the read path overlaps its round trips; a background refresh
    # walks on one thread so readers wait on the interpreter lock for less.
    return tuple(_list_entries_in_dir(
        directory, scope, {entry.name: entry for entry in previous or ()},
        overlap=not background))


def _snapshot_path(directory: str, scope: str) -> str:
    identity = hashlib.sha256(
        f'{scope}\0{directory}'.encode('utf-8', 'surrogateescape')).hexdigest()
    return os.path.join(_dirs._server_data_dir(), _SNAPSHOT_SUBDIR,
                        identity[:32] + '.json')


def _persisted_entry(row, directory: str) -> ListedMemoryEntry:
    name, values = row
    record = dict(zip(_MEMORY_RETRIEVAL_RECORD_KEYS, values, strict=True))
    location = record['package_dir'] or record['filepath']
    if (not isinstance(name, str) or name.startswith('.')
            or location != os.path.join(directory, name)):
        raise ValueError('persisted entry outside its directory')
    # No fingerprint and not reusable: a persisted record serves reads until
    # this process's first refresh reads its file (rule 4).
    return ListedMemoryEntry(name, record, None, False)


def _load_persisted(directory: str, scope: str):
    """Return the persisted ``(entries, walked_at)`` of a directory, or ``None``.

    A missing, unreadable, foreign or differently shaped document is absent.
    """
    try:
        with open(_snapshot_path(directory, scope), encoding='utf-8') as handle:
            document = json.load(handle)
        if (document.get('schema') != _SNAPSHOT_SCHEMA
                or document.get('directory') != directory
                or document.get('scope') != scope
                or tuple(document.get('keys') or ())
                != _MEMORY_RETRIEVAL_RECORD_KEYS):
            return None
        entries = tuple(_persisted_entry(row, directory)
                        for row in document['entries'])
        walked_at = float(document['walkedAt'])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        logger.debug('[MemoryListing] persisted snapshot of %s is unusable',
                     directory, exc_info=True)
        return None
    return entries, walked_at


def _persist(directory: str, scope: str, entries, walked_at: float) -> None:
    path = _snapshot_path(directory, scope)
    document = {
        'schema': _SNAPSHOT_SCHEMA,
        'directory': directory,
        'scope': scope,
        'walkedAt': walked_at,
        'keys': list(_MEMORY_RETRIEVAL_RECORD_KEYS),
        'entries': [
            [entry.name,
             [entry.record[key] for key in _MEMORY_RETRIEVAL_RECORD_KEYS]]
            for entry in entries],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Reconstructible: a crash may lose the file, the rename never tears it.
    write_text_atomic(path, json.dumps(document, ensure_ascii=False,
                                       separators=(',', ':')),
                      fsync=False, mode=0o600)


class _RefreshWorker:
    """The process's one resident thread for background listing work.

    Readers request refreshes on the startup path, so a request is a queue
    append: starting a thread there waits for the new thread to be scheduled,
    which took milliseconds on a loaded host. The thread starts with the
    first request and then idles on its queue. It is a daemon: refreshes are
    reconstructible work that never holds up process exit, and a snapshot
    file is published by an atomic rename. Each directory has at most one
    job queued (``ResidentMemoryListing`` coalesces requests), so the queue
    is bounded by the directories listed.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._jobs: deque = deque()
        self._thread: threading.Thread | None = None

    def submit(self, function, *args) -> None:
        with self._lock:
            self._jobs.append((function, args))
            self._ready.notify()
            if self._thread is not None and self._thread.is_alive():
                return
            # Started under the lock, so concurrent submitters never start a
            # second worker; a forked child starts its own.
            thread = threading.Thread(target=self._run, daemon=True,
                                      name='memory-listing-refresh')
            try:
                thread.start()
            except BaseException:
                self._jobs.pop()  # the caller learns the job never ran
                raise
            self._thread = thread

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._jobs:
                    self._ready.wait()
                function, args = self._jobs.popleft()
            try:
                function(*args)
            except Exception:
                logger.warning('[MemoryListing] background listing work '
                               'failed', exc_info=True)


_refresh_worker = _RefreshWorker()


def _submit_refresh(function, *args):
    """Queue listing work for the process's resident refresh thread."""
    _refresh_worker.submit(function, *args)


def _key(directory: str, scope: str) -> tuple[str, str]:
    # String normalization only: no filesystem round trip on the read path.
    return os.path.normpath(os.path.abspath(directory)), scope


def _is_eligible(record) -> bool:
    return bool(record['enabled'] and record['eligible']
                and not record['is_package'])


class ResidentMemoryListing:
    """Per-directory snapshots of the prefetch corpus, kept fresh off the path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._published = threading.Condition(self._lock)
        self._directories: dict[tuple[str, str], _Directory] = {}
        # One slot: (directory keys, generations, eligible rows).
        self._merged: tuple[tuple, tuple, list[dict]] | None = None

    def _directory_locked(self, key) -> _Directory:
        state = self._directories.get(key)
        if state is None:
            state = self._directories[key] = _Directory(*key)
        return state

    # ── Reads ──────────────────────────────────────────────────────────

    def eligible(self, store_dirs, details=None) -> list[dict]:
        """Return the eligible corpus of ``store_dirs`` in precedence order.

        ``store_dirs`` is ``_iter_memory_store_dirs`` output. Rows are shared
        snapshot records; callers treat them as read-only. ``details``
        receives ``listing`` (``resident``, ``persisted`` or ``walked``: the
        costliest way a directory was obtained) and ``snapshotAgeMs`` (since
        the oldest walk behind the result started).
        """
        keys, snapshots, sources = [], [], set()
        for directory, scope in store_dirs:
            key = _key(directory, scope)
            entries, generation, walked_at, source = self._snapshot(key)
            keys.append(key)
            snapshots.append((entries, generation, walked_at))
            sources.add(source)
        keys = tuple(keys)
        generations = tuple(snapshot[1] for snapshot in snapshots)
        merged = self._merged
        if merged is not None and merged[0] == keys and merged[1] == generations:
            rows = merged[2]
        else:
            rows = self._merge(snapshot[0] for snapshot in snapshots)
            self._merged = (keys, generations, rows)
        if details is not None:
            oldest = min((snapshot[2] for snapshot in snapshots), default=None)
            details.update(
                listing=('walked' if 'walked' in sources else
                         'persisted' if 'persisted' in sources else 'resident'),
                snapshotAgeMs=(0.0 if oldest is None else
                               max(0.0, time.time() - oldest) * 1000))
        return list(rows)

    @staticmethod
    def _merge(directories) -> list[dict]:
        # ``_iter_unique_memories``: the first record of an ID wins, eligible
        # or not, so packages and disabled memories still shadow later ones.
        seen = set()
        rows = []
        for entries in directories:
            for entry in entries:
                record = entry.record
                if record['id'] in seen:
                    continue
                seen.add(record['id'])
                if _is_eligible(record):
                    rows.append(record)
        return rows

    def _snapshot(self, key):
        """Return ``(entries, generation, walked_at, source)`` of one directory."""
        with self._lock:
            state = self._directory_locked(key)
            while state.entries is None and state.refreshing:
                self._published.wait()
            if state.entries is None:
                state.refreshing = True
                walk_required = state.walk_required
                captured = None
            else:
                captured = (state.entries, state.generation, state.walked_at)
                start = self._request_refresh_locked(state)
        if captured is None:
            return self._load(state, walk_required)
        if start:
            self._start_refresh(state)
        return (*captured, 'resident')

    def _load(self, state: _Directory, walk_required: bool):
        """Load a directory this process holds no snapshot of (read path)."""
        source = 'walked'
        try:
            loaded = (None if walk_required
                      else _load_persisted(state.path, state.scope))
            if loaded is not None:
                entries, walked_at = loaded
                source = 'persisted'
            else:
                walked_at = time.time()
                entries = _walk(state.path, state.scope)
            self._publish(state, entries, walked_at)
        except BaseException:
            with self._lock:
                state.refreshing = False
                self._published.notify_all()
            raise
        with self._lock:
            if source == 'persisted':
                state.persisted = tuple(entry.record for entry in entries)
            captured = (state.entries, state.generation, state.walked_at)
            # The load ends its pass; the refresh it requests persists a walk
            # and replaces a persisted snapshot that may predate changes.
            state.refreshing = False
            state.refresh_queued = False
            start = self._request_refresh_locked(state)
        if start:
            self._start_refresh(state)
        return (*captured, source)

    # ── Writes ─────────────────────────────────────────────────────────

    def note_write(self, directory: str, scope: str, name: str) -> None:
        """Apply a durable repository write of entry ``name`` to the listing.

        Writers call this while they still hold the record's mutation lock,
        so writes of one entry apply in their write order. A directory this
        process holds no snapshot of costs no I/O here: the pass that loads
        it re-reads the entry.
        """
        path, scope = key = _key(directory, scope)
        with self._lock:
            state = self._directory_locked(key)
            if state.refreshing or state.entries is None:
                # A running pass may have read the entry before this write,
                # and a load may start from a snapshot persisted before it.
                state.written.add(name)
            if state.entries is None:
                return
        try:
            written = _read_named_listed_entry(path, scope, name)
        except Exception:
            # Never fail the durable write; give up the snapshot instead.
            logger.warning('[MemoryListing] could not read written entry %s '
                           'in %s; the next read walks the directory', name,
                           path, exc_info=True)
            with self._lock:
                state.entries = None
                state.walk_required = True
            return
        with self._lock:
            if state.entries is None:
                return  # dropped meanwhile: the next read walks
            kept = [entry for entry in state.entries if entry.name != name]
            if written is not None:
                kept.append(written)
                kept.sort(key=_walk_order)
            state.entries = tuple(kept)
            state.generation = next(_generations)
            start = self._request_refresh_locked(state)
        if start:
            self._start_refresh(state)

    # ── Refresh ────────────────────────────────────────────────────────

    @staticmethod
    def _request_refresh_locked(state: _Directory) -> bool:
        """Request a refresh; return whether the caller must start one."""
        if state.refreshing:
            state.refresh_queued = True
            return False
        state.refreshing = True
        return True

    def _continue_locked(self, state: _Directory) -> bool:
        """End a pass; return whether a queued refresh runs next."""
        if state.refresh_queued:
            state.refresh_queued = False
            return True
        state.refreshing = False
        self._published.notify_all()
        return False

    def _start_refresh(self, state: _Directory) -> None:
        try:
            _submit_refresh(self._refresh, state)
        except Exception:
            with self._lock:
                state.refresh_queued = False
                self._continue_locked(state)
            logger.warning('[MemoryListing] refresh of %s not started; its '
                           'snapshot stays until the next request',
                           state.path, exc_info=True)

    def _refresh(self, state: _Directory) -> None:
        again = True
        try:
            while again:
                walked_at = time.time()
                try:
                    with self._lock:
                        previous = state.entries
                    self._publish(
                        state, _walk(state.path, state.scope, previous,
                                     background=True),
                        walked_at)
                    self._store(state, walked_at)
                except Exception:
                    logger.warning('[MemoryListing] refresh of %s failed; the '
                                   'previous snapshot stays', state.path,
                                   exc_info=True)
                with self._lock:
                    again = self._continue_locked(state)
        except BaseException:
            with self._lock:
                state.refresh_queued = False
                self._continue_locked(state)
            raise

    def _publish(self, state: _Directory, entries, walked_at: float) -> None:
        """Publish a pass's entries with this process's writes re-applied.

        An unchanged listing keeps its snapshot object and generation, so the
        merged corpus of every root set reading it stays valid.
        """
        entries = tuple(entries)
        while True:
            with self._lock:
                written, state.written = state.written, set()
                current = state.entries
            if written:
                try:
                    entries = _with_entries(entries, state.path, state.scope,
                                            written)
                except BaseException:
                    with self._lock:
                        state.written |= written  # the next pass re-reads
                    raise
                continue
            unchanged = entries == current
            with self._lock:
                # While a pass runs, every write-through also lands in
                # ``written``; none arrived, so ``current`` is still published.
                if state.written:
                    continue
                if not unchanged:
                    state.entries = entries
                    state.generation = next(_generations)
                state.walked_at = walked_at
                state.walk_required = False
                self._published.notify_all()
                return

    def _store(self, state: _Directory, walked_at: float) -> None:
        """Persist the records a refresh just published, unless already on disk."""
        with self._lock:
            entries = state.entries
        if entries is None:
            return
        records = tuple(entry.record for entry in entries)
        if records == state.persisted:
            # Equal records: hold the published ones, not a loaded copy.
            state.persisted = records
            return
        try:
            _persist(state.path, state.scope, entries, walked_at)
        except (OSError, TypeError, ValueError):
            logger.warning('[MemoryListing] snapshot of %s not persisted; a '
                           'restarted process walks it once', state.path,
                           exc_info=True)
            return
        state.persisted = records


resident_listing = ResidentMemoryListing()


__all__ = ['ResidentMemoryListing', 'resident_listing']
