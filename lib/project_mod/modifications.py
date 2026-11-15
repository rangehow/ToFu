"""Modification history, undo/redo, and session tracking.

2026-04-22 — Per-session-dir storage (multi-project concurrency safe)
────────────────────────────────────────────────────────────────────────
Previously this module kept a *single* global list ``_state['modifications']``.
When the UI switched active projects, ``_start_new_session()`` wiped that
list and repopulated it from the newly-active project's file — while
background tasks belonging to the *old* project were still running and
calling ``_record_modification()``.  The result: those background writes
would flush the wrong (swapped-in) list into the old project's file,
silently clobbering history.

The robust fix below makes **disk the source of truth per session_dir**
and keeps an in-memory cache keyed by ``session_dir`` instead of a single
global list.  Every mutation goes through ``_locked_rmw`` (read-modify-
write under the lock) so concurrent tasks operating on *different*
roots cannot interfere with each other, and concurrent tasks on the
*same* root still serialise correctly.

``_state['modifications']`` / ``_state['sessionId']`` remain as a
*read-only* projection of the currently-active primary root for the
sake of ``get_state()`` / UI badges; nothing writes through them.
"""
from array import array
import base64
import binascii
from collections import OrderedDict, namedtuple
import hashlib
from functools import wraps
import re
import json
import os
import stat
import sys
import tempfile
import time
import uuid

from lib.log import get_logger
from lib.project_mod import undo_payloads
from lib.tools.execution_timing import timing_phase
from lib.project_mod.config import (
    SESSIONS_DIR,
    MAX_FILE_SIZE,
    _lock,
    _roots,
    _state,
)
from runtime_guards import resolve_resource_budget
from lib.project_mod.snapshot_content import UNDO_BATCH_CONTENT_BYTES
from lib.project_mod import journal_locks

logger = get_logger(__name__)


class FileHistoryFault(RuntimeError):
    """Undo/redo stopped by a server fault, not by the user's files or history.

    Refusals are the user's answer and are returned as data marked
    ``refused: True`` (whole results and ``details.failed`` rows). A fault's
    message is fixed text that is safe to record and display; the cause chain
    and the server log keep the detail. Not a ``ValueError``, so no boundary
    can mistake it for a client fault.
    """

    @staticmethod
    def reason(error: BaseException) -> str:
        """Disclosable text for ``error``: a fault's own message, else its type."""
        if isinstance(error, FileHistoryFault):
            return str(error)
        return f'internal error ({type(error).__name__})'


# ═══════════════════════════════════════════════════════════════════
#  Per-session-dir in-memory cache
# ═══════════════════════════════════════════════════════════════════
# Maps absolute session_dir → list[mod dict]. Disk remains authoritative, so an
# LRU eviction only trades a later read for bounded resident memory.
_MODS_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_PROJECT_UNDO_CACHE_CAPACITY', maximum=2048)
# Reuse the launch-probed/overridden session budget: each unit grants 256 KiB
# of resident history (16 MiB at the lean 64-session default, at most 512 MiB).
# Sessions share the total byte budget through LRU eviction; no fixed fraction
# excludes an active metadata index that fits the machine-resolved budget.
_MODS_CACHE_MAX_BYTES = _MODS_CACHE_CAPACITY * 256 * 1024
_MODS_CACHE_MAX_SESSION_BYTES = _MODS_CACHE_MAX_BYTES
_mods_cache: OrderedDict[str, list] = OrderedDict()
_mods_cache_identity = {}
_mods_cache_bytes = {}
# session_dir -> count of publications, extensions and invalidations of its
# entry. A reader that parsed without journal admission publishes only while
# the count is unchanged, so its snapshot never replaces a newer writer's entry.
_mods_cache_generation = {}
# session_dir -> (journal identity, budget) whose snapshot outweighed the cache
# budget. Appends only add rows, so a later snapshot of the same index and fold
# artifact with a longer log outweighs it too and is refused without a walk.
_mods_cache_oversized = {}


def _cache_value_weight(value, limit):
    """Count a detached value conservatively, stopping above the cache budget.

    Values are charged at every occurrence. Dict keys are charged once per key
    object: decoded index rows share their key strings, so charging them per
    row overstated a 41 MB history as 63 MB. Distinct key objects are bounded
    by the record schema plus the rows of the bounded append log.
    """
    size = 0
    charged_keys = set()

    def visit(value):
        nonlocal size
        size += sys.getsizeof(value)
        if size > limit:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if id(key) not in charged_keys:
                    charged_keys.add(id(key))
                    size += sys.getsizeof(key)
                visit(child)
                if size > limit:
                    break
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
                if size > limit:
                    break

    try:
        visit(value)
    except RecursionError:
        # Cyclic/deep data cannot be a useful bounded reconstructible cache.
        return limit + 1
    return size


def _mods_cache_weight(session_dir, mods, identity):
    """Weigh a cold snapshot outside the global lock, including its primary view.

    Shared values are charged repeatedly, without an unbounded seen-set.
    Appends charge only their new record and list growth after durable commit.
    """
    limit = min(_MODS_CACHE_MAX_SESSION_BYTES, _MODS_CACHE_MAX_BYTES)
    size = 512 + sys.getsizeof(mods)
    for value in (session_dir, identity, mods):
        size += _cache_value_weight(value, limit - size)
        if size > limit:
            break
    return size


_JOURNAL_INDEX_NAME = 'modifications.json'
# Append log, written only by a holder of the rewrite lock; a fold renames it to
# the fold artifact, publishes an index absorbing it, then unlinks the artifact.
_JOURNAL_TAIL_NAME = 'modifications.journal'
_JOURNAL_FOLD_NAME = 'modifications.journal.fold'
# A commit that finds the rewrite lock busy publishes its row here as one
# immutable file (rename), so no writer ever waits for another file's writer.
_JOURNAL_RECORDS_DIRECTORY = 'records'
_RECORD_NAME = re.compile(r'(\d{20}-\d{12})-([0-9a-f]{32})\.json')
# A log longer than this is folded into the index by the writer that grew it.
_JOURNAL_TAIL_MAX_BYTES = 512 * 1024
# session_dir -> _Validated. Identity-keyed and reconstructible: a commit
# detects a replayed changeId without materializing an 8 MB history.
_JOURNAL_VALIDATED = {}
# session_dir -> {record name: (row, bytes)}. Records are immutable, so a
# parsed record stays exact until a rewrite unlinks it.
_RECORD_ROWS = {}
# Session dirs whose payload directory this process swept for crash artifacts.
_PAYLOADS_SWEPT = set()


class _RecordVanished(Exception):
    """A listed record was unlinked by a concurrent rewrite; read again."""


def _journal_file(session_dir, name):
    return os.path.join(session_dir, name)


def _records_directory(session_dir):
    return os.path.join(session_dir, _JOURNAL_RECORDS_DIRECTORY)


def _record_names(session_dir):
    """Sorted committed record names; a fresh listing on every call."""
    try:
        names = os.listdir(_records_directory(session_dir))
    except FileNotFoundError:
        return ()
    # Cheap shape filter; each name is matched fully once, when it is parsed.
    return tuple(sorted(name for name in names
                        if len(name) == 71 and name[0] != '.' and name.endswith('.json')))


def _record_rows(session_dir, names):
    """Validated rows of ``names``; each file is parsed once per process."""
    with _lock:
        known = _RECORD_ROWS.setdefault(session_dir, {})
    directory = _records_directory(session_dir)
    current = {}
    rows = []
    for name in names:
        entry = known.get(name)
        if entry is None:
            match = _RECORD_NAME.fullmatch(name)
            if match is None:
                raise ValueError('Invalid modification journal record name')
            try:
                with open(os.path.join(directory, name), 'rb') as handle:
                    data = handle.read()
            except FileNotFoundError as error:
                raise _RecordVanished(name) from error
            try:
                row = json.loads(data)
            except ValueError as error:
                raise ValueError('Invalid modification journal record file') from error
            _validate_records([row])
            if row.get('journalKey') != match[1] or row.get('changeId') != match[2]:
                raise ValueError('Modification journal record identity mismatch')
            entry = (row, len(data))
            current[name] = entry
        rows.append(entry[0])
    if current:
        with _lock:
            _RECORD_ROWS.setdefault(session_dir, {}).update(current)
    return rows


def _forget_records(session_dir, names):
    with _lock:
        known = _RECORD_ROWS.get(session_dir)
        if known:
            for name in names:
                known.pop(name, None)


def _known_record(session_dir, change_id):
    """The parsed record of ``change_id`` among this process's known records."""
    suffix = f'-{change_id}.json'
    with _lock:
        known = _RECORD_ROWS.get(session_dir) or {}
        return next(((name, entry[0]) for name, entry in known.items()
                     if name.endswith(suffix)), None)


def _ordered(rows):
    """History order: legacy rows first as stored, then by commit key; one row per changeId."""
    rows = sorted(rows, key=lambda row: row.get('journalKey') or '')
    seen = set()
    result = []
    for row in rows:
        change_id = row.get('changeId')
        if change_id:
            if change_id in seen:
                continue
            seen.add(change_id)
        result.append(row)
    return result


def _file_identity(path):
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _rewrite_identity(session_dir):
    """The components only a history rewrite replaces: the index and the fold artifact."""
    return (_file_identity(_journal_file(session_dir, _JOURNAL_INDEX_NAME)),
            _file_identity(_journal_file(session_dir, _JOURNAL_FOLD_NAME)))


def _journal_identity(session_dir):
    """Identity of every durable journal component, not a TTL: (index, log, fold, records).

    Record names are listed before the log is examined: a writer moves a record
    into the log by appending its row before unlinking its file, so an identity
    never misses a committed record. A directory timestamp is not used; it is
    not a reliable change signal on every mount.
    """
    names = _record_names(session_dir)
    index, fold = _rewrite_identity(session_dir)
    return (index, _file_identity(_journal_file(session_dir, _JOURNAL_TAIL_NAME)), fold, names)


# ── Live-change listeners ─────────────────────────────────────────────
# Presentation-only channel: the task engine subscribes
# (``commit_round._derive.note_live_file_change``, registered from
# ``lib.tasks_pkg.handlers.project``) so a RUNNING turn's fileChanges card
# grows as edits land instead of appearing only at settlement.  The journal
# stays the sole authority — listeners observe, never mutate.
_live_change_listeners: list = []


def register_live_change_listener(fn):
    """Subscribe ``fn(task_id, entry)`` to journaled file changes. Idempotent."""
    if fn not in _live_change_listeners:
        _live_change_listeners.append(fn)


def _default_live_action(mod_type, existed):
    """Map a journal record to the settlement action vocabulary
    (``commit_round._derive.derive_round_modified_files``)."""
    if mod_type == 'write_file':
        return 'written' if existed else 'created'
    if mod_type in ('apply_diff', 'apply_diffs'):
        return 'patched'
    if mod_type in ('insert_content', 'insert_contents'):
        return 'inserted'
    if mod_type == 'run_command':
        return 'modified' if existed else 'created'
    return mod_type


def _sync_journal_directory(directory):
    """Persist journal name changes; an unsupported flush is a visible failure."""
    descriptor = os.open(directory or '.', os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_JSON_WRITE_BUFFER_BYTES = 64 * 1024


def _write_compact_json(handle, data):
    """Encode one metadata row in C, then emit exact bounded ASCII batches."""
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(',', ':'))
    def metadata_tokens():
        yield '{"modifications":['
        for index, record in enumerate(data['modifications']):
            if index:
                yield ','
            yield encoder.encode(record)
        yield ']'
        for name in ('absorbedFold', 'absorbedRecords'):
            if data.get(name) is not None:
                yield f',"{name}":'
                yield encoder.encode(data[name])
        yield '}'
    tokens = (metadata_tokens()
              if isinstance(data, dict)
              and set(data) <= {'modifications', 'absorbedFold', 'absorbedRecords'}
              and 'modifications' in data
              else encoder.iterencode(data))
    pieces = []
    size = written = 0
    for token in tokens:
        offset = 0
        while offset < len(token):
            end = min(len(token), offset + _JSON_WRITE_BUFFER_BYTES - size)
            pieces.append(token[offset:end])
            size += end - offset
            offset = end
            if size == _JSON_WRITE_BUFFER_BYTES:
                handle.write(''.join(pieces))
                written += size
                pieces.clear()
                size = 0
    if pieces:
        handle.write(''.join(pieces))
        written += size
    return written


def _atomic_json_write(filepath, data):
    """Write JSON data to file atomically (write to temp, then rename).

    Prevents file corruption if the process is killed mid-write.
    os.replace() is atomic on POSIX when src and dst are on the same filesystem.
    """
    dir_name = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', prefix='.modifications_')
    try:
        with os.fdopen(fd, 'w', encoding='ascii', buffering=_JSON_WRITE_BUFFER_BYTES) as f:
            written_bytes = _write_compact_json(f, data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        _sync_journal_directory(dir_name)
        return written_bytes
    except BaseException:
        # Clean up temp file on any failure (including KeyboardInterrupt)
        try:
            os.unlink(tmp_path)
        except OSError as _e_audit:
            logger.debug('[modifications] _atomic_json_write caught %s: %s', type(_e_audit).__name__, _e_audit)
            pass
        raise


def _remember_project_root(session_dir, base_path):
    """Persist the server-resolved root independently of pending changes."""
    marker_file = os.path.join(session_dir, 'project.json')
    normalized = os.path.abspath(base_path)
    try:
        if os.path.isfile(marker_file):
            with open(marker_file) as f:
                if json.load(f).get('basePath') == normalized:
                    return
        _atomic_json_write(marker_file, {'basePath': normalized})
    except Exception as e:
        # A modification record also carries basePath, so a marker failure
        # must not turn an otherwise valid project write into an exception.
        logger.debug('[Modifications] project marker write failed: %s', e)


def _nudge_vscode(base_path, filepath):
    """Notify timestamp-polling editors without reopening unanchored paths."""
    from lib.project_mod import undo_filesystem
    try:
        undo_filesystem.touch_file(base_path, filepath)
    except (OSError, ValueError, NotImplementedError) as exc:
        logger.debug('Failed to bump mtime for VS Code watcher on %s: %s', filepath, exc)


def _ensure_durable_session_directory(directory):
    """Create ancestors in publication order, flushing each parent before descent.

    Retry must flush the deepest existing ancestor too: a previous mkdir may
    have succeeded while its parent flush failed. Do not create descendants
    until that uncertainty is resolved. The iterative walk is bounded by the
    requested path depth and holds no directory descriptors between flushes.
    """
    pending = []
    current = os.path.abspath(directory)
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            raise OSError('No existing ancestor for session directory')
        pending.append(current)
        current = parent
    _sync_journal_directory(os.path.dirname(current))
    for current in reversed(pending):
        try:
            os.mkdir(current)
        except FileExistsError:
            if not os.path.isdir(current):
                raise
        _sync_journal_directory(os.path.dirname(current))


def _get_session_dir(base_path, *, create=True):
    """Get session directory for storing modification history."""
    if not base_path:
        return None
    session_id = hashlib.md5(base_path.encode()).hexdigest()[:12]
    session_dir = os.path.join(SESSIONS_DIR, session_id)
    # The outer admission already initialized and flushed this directory.
    # Nested recovery/recording must not re-fsync its ancestors at every layer.
    if create and not journal_locks.held_paths(session_dir):
        _ensure_durable_session_directory(session_dir)
        _remember_project_root(session_dir, base_path)
    return session_dir


def resolve_base_path(task_id=None, conv_id=None):
    """Find the project ``base_path`` that recorded a given task/conversation.

    Scans every persisted session's modifications.json for a record whose
    ``taskId`` (preferred) or ``convId`` matches, and returns the
    ``basePath`` stored on that record.  This lets undo target the correct
    project even when the caller can't supply an explicit ``projectPath``
    and the globally-active project (``_state['path']``) points elsewhere
    — the exact situation that breaks undo when multiple conversations
    edit different projects concurrently.

    Returns the absolute base_path str, or ``None`` if no matching record
    is found (e.g. legacy records written before ``basePath`` tagging).
    """
    if not task_id and not conv_id:
        return None
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError as e:
        logger.debug('[Modifications] resolve_base_path: cannot list %s: %s',
                     SESSIONS_DIR, e)
        return None
    history_roots = []
    for name in names:
        session_dir = os.path.join(SESSIONS_DIR, name)
        marker_file = os.path.join(session_dir, 'project.json')
        try:
            with open(marker_file) as f:
                marker = json.load(f)
                marker_root = marker.get('basePath') if isinstance(marker, dict) else None
                if not isinstance(marker_root, str):
                    marker_root = ''
            if marker_root:
                history_roots.append(marker_root)
        except (OSError, TypeError, ValueError):
            pass
        # A crash can leave task attribution only in the prepared intent.
        # Resolve that root without recovering or mutating unrelated projects.
        if os.path.isdir(os.path.join(session_dir, 'write-intents-v1')):
            try:
                from lib.project_mod.write_intents import intent_store
                with intent_store(session_dir) as store:
                    for pending in store.read():
                        matches = (pending.get('taskId') == task_id if task_id
                                   else pending.get('convId') == conv_id)
                        expected_session = hashlib.md5(pending['basePath'].encode()).hexdigest()[:12]
                        if matches and name == expected_session:
                            return pending['basePath']
            except (OSError, ValueError) as exc:
                logger.debug('Prepared root lookup skipped %s: %s', name, exc)
        if (not any(os.path.isfile(_journal_file(session_dir, journal)) for journal in
                    (_JOURNAL_INDEX_NAME, _JOURNAL_TAIL_NAME, _JOURNAL_FOLD_NAME))
                and not _record_names(session_dir)):
            continue
        try:
            mods = _load_from_disk(session_dir)
        except (OSError, ValueError) as e:
            logger.debug('[Modifications] resolve_base_path: skip %s: %s', name, e)
            continue
        for m in mods:
            if task_id and m.get('taskId') == task_id and m.get('basePath'):
                return m['basePath']
            if conv_id and not task_id and m.get('convId') == conv_id and m.get('basePath'):
                return m['basePath']
    # Undo removes completed task records from modifications.json. The root
    # marker plus append-only file history keeps redo resolvable after reload
    # without accepting a filesystem path from the browser.
    if history_roots:
        try:
            from lib import file_history

            for base_path in dict.fromkeys(history_roots):
                snapshot_id = file_history.find_latest_snapshot_id(
                    base_path,
                    task_id=task_id or None,
                    conv_id=conv_id if not task_id else None,
                )
                if snapshot_id:
                    return base_path
        except Exception as e:
            logger.debug('[Modifications] history path resolution failed: %s', e)
    return None


# Only remove .tmp files older than this — crash artifacts are much
# older; in-flight atomic writes are sub-second.
_STALE_TMP_AGE_SECONDS = 60.0


def _clean_stale_tmp(session_dir):
    """Remove leftover atomic-write temp files from previous crashes.

    Safety rails:
      - Runs only on a bounded-cache cold load.
      - Deletes only files older than :data:`_STALE_TMP_AGE_SECONDS` so a
        concurrent in-flight :func:`_atomic_json_write` on the same
        session_dir cannot have its fresh .tmp file deleted before
        ``os.replace`` runs.

    Caller holds the rewrite lock, which every index writer holds; record
    writers hold none, so their temporaries rely on the age rule alone (a
    record whose temporary is reclaimed fails its commit visibly and its
    prepared intent stays the recovery evidence).
    """
    records = _records_directory(session_dir)
    try:
        names = [(session_dir, name) for name in os.listdir(session_dir)
                 if name.startswith('.modifications_') and name.endswith('.tmp')]
        try:
            names += [(records, name) for name in os.listdir(records)
                      if name.startswith('.record-') and name.endswith('.tmp')]
        except FileNotFoundError:
            pass
        now = time.time()
        for directory, name in names:
            stale = os.path.join(directory, name)
            try:
                age = now - os.path.getmtime(stale)
                if age < _STALE_TMP_AGE_SECONDS:
                    continue  # likely in-flight, don't touch
                os.unlink(stale)
                logger.debug('Cleaned up stale temp file (age=%.0fs): %s', age, name)
            except OSError as e:
                logger.debug('Could not remove stale temp %s: %s', name, e)
    except OSError as e:
        logger.debug('Failed to list session dir for cleanup %s: %s', session_dir, e)


_History = namedtuple('_History', 'index_records rows identity absorbed logged_ids log_end')


def _load_from_disk(session_dir):
    """Load authoritative history; only a missing journal means empty history.

    History is the index envelope, then the fold artifact and the append log,
    then every record file the index does not record absorbing, ordered by
    commit key (rows without one, from older journals, first as stored).

    Read failures and malformed records remain visible and never rename,
    replace or silently discard durable evidence. A rejected load must not
    populate the cache, so later mutations cannot overwrite it with [] plus
    a newly committed record.
    """
    return _read_history(session_dir).rows


def _read_index(session_dir):
    session_file = os.path.join(session_dir, _JOURNAL_INDEX_NAME)
    try:
        size = os.stat(session_file).st_size
        with timing_phase('undo_index_read', bytes_read=size):
            with open(session_file) as handle:
                data = json.load(handle)
    except FileNotFoundError:
        data = {'modifications': []}
    if not isinstance(data, dict) or not isinstance(data.get('modifications'), list):
        raise ValueError('Invalid modification journal envelope')
    absorbed = data.get('absorbedRecords', [])
    if not isinstance(absorbed, list) or not all(
            isinstance(name, str) and _RECORD_NAME.fullmatch(name) for name in absorbed):
        raise ValueError('Invalid modification journal envelope')
    _validate_records(data['modifications'])
    return data, frozenset(absorbed)


def _read_log(session_dir):
    """``(identity, rows, complete-line end)`` of the append log as one read saw it."""
    try:
        handle = open(_journal_file(session_dir, _JOURNAL_TAIL_NAME), 'rb')
    except FileNotFoundError:
        return None, [], 0
    with handle:
        identity = _stat_identity(os.fstat(handle.fileno()))
        data = handle.read(identity[2])
    with timing_phase('undo_journal_tail', bytes_read=len(data)):
        return identity, _log_records(data), data.rfind(b'\n') + 1


def _read_history(session_dir):
    """One consistent history snapshot; takes no lock and writes nothing.

    Records are immutable, the log only grows (its single appender holds the
    rewrite lock), and a rewrite replaces the index by rename before unlinking
    the records it absorbed. Records are read before the log, because a writer
    appends a record's row to the log before unlinking its file. A read that
    overlapped a rewrite (index or fold artifact replaced, a listed record
    gone) is repeated.
    """
    while True:
        rewrite = _rewrite_identity(session_dir)
        data, absorbed = _read_index(session_dir)
        legacy = []
        absorbed_fold = data.get('absorbedFold')
        if absorbed_fold is None or list(absorbed_fold) != list(rewrite[1] or ()):
            legacy += _read_tail(session_dir, _JOURNAL_FOLD_NAME)
        names = _record_names(session_dir)
        try:
            committed = _record_rows(session_dir, [name for name in names if name not in absorbed])
        except _RecordVanished:
            continue
        log_identity, logged, log_end = _read_log(session_dir)
        if _rewrite_identity(session_dir) != rewrite:
            continue
        with _lock:
            known = _RECORD_ROWS.get(session_dir)
            if known:
                listed = set(names)
                for name in [name for name in known if name not in listed]:
                    del known[name]
        index_records = data['modifications']
        base_rows = index_records + legacy + logged
        return _History(index_records, _ordered(base_rows + committed),
                        (rewrite[0], log_identity, rewrite[1], names), absorbed,
                        {row['changeId'] for row in base_rows if row.get('changeId')}, log_end)


def _validate_records(records):
    for record in records:
        if (not isinstance(record, dict)
                or not isinstance(record.get('type'), str) or not record['type']
                or not isinstance(record.get('path'), str) or not record['path']):
            raise ValueError('Invalid modification journal record')
        for name in ('basePath', 'taskId', 'convId', 'changeId', 'journalKey'):
            if record.get(name) is not None and not isinstance(record[name], str):
                raise ValueError('Invalid modification journal attribution')
    for record in records:
        for field in undo_payloads.FIELDS:
            if undo_payloads.is_reference(record.get(field)):
                undo_payloads.validate_reference(record[field])


def _read_tail(session_dir, name):
    """Parse one append log or fold artifact; a torn final line was never acknowledged.

    A commit fsyncs a complete newline-terminated line before acknowledging, so
    only a line WITHOUT its newline can be a crash mid-append: it is dropped
    here and its prepared intent is reconciled by write recovery.
    """
    path = _journal_file(session_dir, name)
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except FileNotFoundError:
        return []
    with timing_phase('undo_journal_tail', bytes_read=len(data)):
        return _log_records(data)


def _log_records(data):
    """Validated records of log bytes; bytes after the last newline are torn."""
    complete = data.rfind(b'\n')
    if complete < 0:
        return []
    records = []
    for line in data[:complete].split(b'\n'):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise ValueError('Invalid modification journal tail record') from error
        _validate_records([record])
        records.append(record)
    return records


def _fold_identity(session_dir):
    return _file_identity(_journal_file(session_dir, _JOURNAL_FOLD_NAME))


def _append_to_tail(session_dir, rows):
    """Append rows as newline-terminated lines and fsync; returns the log's new length.

    The caller holds the rewrite lock, the log's only appender, so a torn
    suffix left by a crash can be cut back safely and a failed append is
    truncated away: an unacknowledged row never becomes visible.
    """
    payload = b''.join(_tail_line(row) for row in rows)
    path = _journal_file(session_dir, _JOURNAL_TAIL_NAME)
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _sync_journal_directory(session_dir)
        except BaseException:
            os.close(descriptor)
            raise
    try:
        previous = _torn_tail_offset(descriptor)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError('Short journal append')
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            try:
                os.ftruncate(descriptor, previous)
            except OSError:
                pass
            raise
        return previous + len(payload)
    finally:
        os.close(descriptor)


def _torn_tail_offset(descriptor):
    """Length of the log's complete-line prefix, discarding a crashed append."""
    size = os.fstat(descriptor).st_size
    if size == 0:
        return 0
    data = os.pread(descriptor, size, 0)
    complete = data.rfind(b'\n') + 1
    if complete != size:
        os.ftruncate(descriptor, complete)
        os.fsync(descriptor)
    return complete


def _publish_record(session_dir, name, record):
    """Publish one immutable record file: same-directory temp, fsync, rename, dir fsync."""
    directory = _records_directory(session_dir)
    if not os.path.isdir(directory):
        _ensure_durable_session_directory(directory)
    payload = _tail_line(record)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix='.record-', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, os.path.join(directory, name))
        temporary = None
        _sync_journal_directory(directory)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return len(payload)


def _tail_line(record):
    return json.dumps(record, ensure_ascii=True, separators=(',', ':')).encode('ascii') + b'\n'


def _publish_cache(session_dir, mods, identity, *, generation=None):
    """Admit a confirmed snapshot by bytes/count; never truncate durable history.

    A reader without journal admission passes the entry ``generation`` it
    sampled before reading; its snapshot is dropped when a writer published,
    extended or invalidated the entry meanwhile.
    """
    limit = min(_MODS_CACHE_MAX_SESSION_BYTES, _MODS_CACHE_MAX_BYTES)
    with _lock:
        oversized = _extends_oversized(session_dir, identity, limit)
    weight = limit + 1 if oversized else _mods_cache_weight(session_dir, mods, identity)
    with _lock:
        if weight > limit:
            _mods_cache_oversized[session_dir] = (identity, limit)
        if generation is not None and _mods_cache_generation.get(session_dir, 0) != generation:
            return
        _invalidate_cache(session_dir)
        if weight > limit:
            return
        _mods_cache[session_dir] = mods
        _mods_cache_identity[session_dir] = identity
        _mods_cache_bytes[session_dir] = weight
        _mods_cache.move_to_end(session_dir)
        _evict_cache_over_budget()
        if _state.get('sessionId') == session_dir:
            _state['modifications'] = list(mods)


def _extends_oversized(session_dir, identity, limit):
    """Whether ``identity`` only added rows to a snapshot already over ``limit``.

    Caller holds ``_lock``. The same index and fold artifact plus the same log
    grown (records only ever move into the log or stay) hold every remembered row.
    """
    memo = _mods_cache_oversized.get(session_dir)
    if memo is None or memo[1] != limit:
        return False
    before = memo[0]
    if before[0] != identity[0] or before[2] != identity[2]:
        return False
    tail, tail_now = before[1], identity[1]
    return tail is None or (tail_now is not None and tail_now[:2] == tail[:2]
                            and tail_now[2] >= tail[2])


def _evict_cache_over_budget():
    """Apply the existing shared LRU budget; caller holds the cache lock."""
    total = sum(_mods_cache_bytes[key] for key in _mods_cache)
    while len(_mods_cache) > _MODS_CACHE_CAPACITY or total > _MODS_CACHE_MAX_BYTES:
        evicted_dir = next(iter(_mods_cache))
        total -= _mods_cache_bytes[evicted_dir]
        _invalidate_cache(evicted_dir)


def _publish_extension(session_dir, cached, cached_identity, rows, appended, identity, generation):
    """Admit ``cached`` plus newer records by weighing only the added rows.

    Publishes nothing when the entry changed meanwhile; the history itself is
    never traversed, and the shared cache lock never covers filesystem I/O.
    """
    limit = min(_MODS_CACHE_MAX_SESSION_BYTES, _MODS_CACHE_MAX_BYTES)
    increment = sum(_cache_value_weight(row, limit) for row in appended)
    increment += max(0, _cache_value_weight(identity, limit)
                     - _cache_value_weight(cached_identity, limit))
    increment += max(0, sys.getsizeof(rows) - sys.getsizeof(cached))
    # Reserve a standalone list of the new records for the primary view.
    increment += sys.getsizeof(list(appended))
    with _lock:
        if (_mods_cache.get(session_dir) is not cached
                or _mods_cache_identity.get(session_dir) != cached_identity
                or _mods_cache_generation.get(session_dir, 0) != generation):
            return
        weight = _mods_cache_bytes[session_dir] + increment
        if weight > limit:
            _invalidate_cache(session_dir)
            return
        _mods_cache[session_dir] = rows
        _mods_cache_identity[session_dir] = identity
        _mods_cache_bytes[session_dir] = weight
        _mods_cache_generation[session_dir] = generation + 1
        _mods_cache.move_to_end(session_dir)
        _evict_cache_over_budget()
        if _state.get('sessionId') == session_dir:
            _state['modifications'] = list(rows)


def _invalidate_cache(session_dir):
    with _lock:
        _mods_cache_generation[session_dir] = _mods_cache_generation.get(session_dir, 0) + 1
        _mods_cache.pop(session_dir, None)
        _mods_cache_identity.pop(session_dir, None)
        _mods_cache_bytes.pop(session_dir, None)
        if _state.get('sessionId') == session_dir:
            _state['modifications'] = []


def _cache_get(session_dir):
    """Committed history for undo and project open, plus the per-process sweep.

    Takes no lock and does not hold ``_lock`` over I/O. Returned records are
    read-only; journal rewrites use ``_locked_rmw``.
    """
    rows = _committed_rows(session_dir)
    _sweep_crash_artifacts(session_dir)
    return rows


def _sweep_crash_artifacts(session_dir, *, crash_evidence=False):
    """Reclaim payload crash artifacts once per process, or after crash evidence.

    Holds the rewrite lock without waiting (a running fold or undo defers the
    sweep) and reads the committed rows under it. A commit's pack is
    unreferenced until its row is published, so the commit holds a shared lock
    on the pack until then and the sweep skips locked packs (see
    ``undo_payloads.reclaim_orphans``). Only a commit that crashed or failed
    leaves an unreferenced pack or stage.
    """
    if session_dir in _PAYLOADS_SWEPT and not crash_evidence:
        return
    try:
        with journal_locks.rewrite_lock(session_dir, blocking=False) as rewriting:
            if not rewriting:
                return
            history = _read_history(session_dir)

            def committed_since():
                # Commits that found this rewrite lock held published record files.
                appended = _rows_appended_since(
                    session_dir, history.identity, _journal_identity(session_dir))
                return _read_history(session_dir).rows if appended is None else appended

            with timing_phase('undo_orphan_cleanup'):
                undo_payloads.reclaim_orphans(session_dir, history.rows,
                                              committed_since=committed_since)
    except (OSError, ValueError):
        logger.warning('Undo crash-artifact cleanup deferred; journal remains authoritative')
        return
    _PAYLOADS_SWEPT.add(session_dir)


def _committed_rows(session_dir):
    """Committed history; takes no lock and writes nothing durable.

    A warm entry whose index and legacy files are unchanged catches up by
    reading only the record files committed since; anything else is one
    consistent ``_read_history``. It never waits for or delays a writer. The
    result may be the shared cache entry: callers iterate it, never mutate it.
    """
    with _lock:
        generation = _mods_cache_generation.get(session_dir, 0)
    identity = _journal_identity(session_dir)
    with _lock:
        cached = _mods_cache.get(session_dir)
        cached_identity = _mods_cache_identity.get(session_dir)
        if cached is not None and cached_identity == identity:
            _mods_cache.move_to_end(session_dir)
            return cached
    if cached is not None:
        appended = _rows_appended_since(session_dir, cached_identity, identity)
        if appended is not None:
            rows = _merge_appended(cached, appended)
            _publish_extension(session_dir, cached, cached_identity, rows, appended,
                               identity, generation)
            return rows
        # A replaced history: never leave the stale rows resident or shown in
        # the active projection, even when the new read fails.
        _invalidate_cache(session_dir)
        with _lock:
            generation = _mods_cache_generation.get(session_dir, 0)
    history = _read_history(session_dir)
    _publish_cache(session_dir, history.rows, history.identity, generation=generation)
    return history.rows


def _rows_appended_since(session_dir, before, after):
    """Rows committed between journal identities ``before`` and ``after``.

    ``None`` unless that is a pure addition: the same index and fold artifact,
    the same log grown from a complete line, and every record file that
    disappeared accounted for by a row the log gained. A record a rewrite
    unlinked meanwhile also answers ``None``. The rows describe the history at
    ``after`` without parsing the index.
    """
    if before[0] != after[0] or before[2] != after[2]:
        return None
    appended = []
    if before[1] != after[1]:
        previous, current = before[1], after[1]
        if current is None:
            return None
        offset = 0
        if previous is not None:
            if current[:2] != previous[:2] or current[2] < previous[2]:
                return None
            offset = previous[2]
        try:
            handle = open(_journal_file(session_dir, _JOURNAL_TAIL_NAME), 'rb')
        except FileNotFoundError:
            return None
        with handle:
            if tuple(_stat_identity(os.fstat(handle.fileno()))[:2]) != current[:2]:
                return None
            if offset:
                handle.seek(offset - 1)
                if handle.read(1) != b'\n':
                    return None
            data = handle.read(current[2] - offset)
        if not data.endswith(b'\n') and data:
            return None
        with timing_phase('undo_journal_tail', bytes_read=len(data)):
            appended = _log_records(data)
    known = set(before[3])
    present = set(after[3])
    known_ids = {name[34:66] for name in known}
    logged_ids = {row.get('changeId') for row in appended}
    if not {name[34:66] for name in known - present} <= logged_ids:
        return None
    rows = [row for row in appended if row.get('changeId') not in known_ids]
    try:
        rows += _record_rows(session_dir, [name for name in after[3] if name not in known])
    except _RecordVanished:
        return None
    seen = set()
    unique = []
    for row in rows:
        change_id = row.get('changeId')
        if change_id in seen:
            continue
        seen.add(change_id)
        unique.append(row)
    return unique


def _merge_appended(prefix, appended):
    """``prefix`` plus newer rows in history order, as a new list."""
    if not appended:
        return list(prefix)
    last = (prefix[-1].get('journalKey') or '') if prefix else ''
    keys = [row.get('journalKey') or '' for row in appended]
    if keys == sorted(keys) and keys[0] >= last:
        return prefix + appended
    return _ordered(list(prefix) + appended)


# session_dir -> _IndexLocator of the exact index file it scanned.
_INDEX_LOCATORS = {}
# session_dir -> (index identity, legacy log identity, rows): the parsed legacy
# append log of that exact file.
_LOG_ROWS = {}


class _IndexLocator:
    """Byte ranges of each conversation's rows within one exact index file.

    Two integers per row, so a history that outweighs the cache budget still
    answers one conversation by decoding only that conversation's rows.
    """

    __slots__ = ('identity', 'ranges', 'absorbed')

    def __init__(self, identity, ranges, absorbed):
        self.identity = identity
        self.ranges = ranges
        self.absorbed = absorbed


def _stat_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _json_space(text, index):
    while index < len(text) and text[index] in ' \t\n\r':
        index += 1
    return index


def _index_row_spans(text):
    """``(rows, spans, absorbed)`` of the index envelope's arrays."""
    decoder = json.JSONDecoder()
    rows = spans = None
    absorbed = []
    try:
        index = _json_space(text, 0)
        if text[index] != '{':
            raise ValueError('Invalid modification journal envelope')
        index = _json_space(text, index + 1)
        while text[index] != '}':
            key, index = decoder.raw_decode(text, index)
            index = _json_space(text, index)
            if text[index] != ':':
                raise ValueError('Invalid modification journal envelope')
            index = _json_space(text, index + 1)
            if key == 'absorbedRecords':
                absorbed, index = decoder.raw_decode(text, index)
            elif key != 'modifications':
                _, index = decoder.raw_decode(text, index)
            elif text[index] != '[':
                raise ValueError('Invalid modification journal envelope')
            else:
                rows, spans = [], []
                index = _json_space(text, index + 1)
                while text[index] != ']':
                    start = index
                    row, index = decoder.raw_decode(text, index)
                    rows.append(row)
                    spans.append((start, index))
                    index = _json_space(text, index)
                    if text[index] == ',':
                        index = _json_space(text, index + 1)
                        if text[index] == ']':
                            raise ValueError('Invalid modification journal envelope')
                    elif text[index] != ']':
                        raise ValueError('Invalid modification journal envelope')
                index += 1
            index = _json_space(text, index)
            if text[index] == ',':
                index = _json_space(text, index + 1)
            elif text[index] != '}':
                raise ValueError('Invalid modification journal envelope')
        if rows is None or _json_space(text, index + 1) != len(text):
            raise ValueError('Invalid modification journal envelope')
    except IndexError as error:
        raise ValueError('Invalid modification journal envelope') from error
    if not isinstance(absorbed, list) or not all(
            isinstance(name, str) and _RECORD_NAME.fullmatch(name) for name in absorbed):
        raise ValueError('Invalid modification journal envelope')
    return rows, spans, frozenset(absorbed)


def _locate_index(session_dir):
    """Scan the index once, validating every row; ``None`` without an ASCII index.

    The compact writer emits ASCII, so character and byte offsets agree; an
    older non-ASCII index keeps being read whole until its next fold.
    """
    try:
        with open(_journal_file(session_dir, _JOURNAL_INDEX_NAME), 'rb') as handle:
            info = os.fstat(handle.fileno())
            data = handle.read()
    except FileNotFoundError:
        return None
    if not data.isascii():
        return None
    with timing_phase('undo_index_read', bytes_read=len(data)):
        rows, spans, absorbed = _index_row_spans(data.decode('ascii'))
        _validate_records(rows)
    ranges = {}
    for (start, end), row in zip(spans, rows):
        ranges.setdefault(row.get('convId'), array('q')).extend((start, end))
    return _IndexLocator(_stat_identity(info), ranges, absorbed)


def _located_rows(session_dir, locator, conv_id):
    """One conversation's index rows, or ``None`` once that index was replaced.

    Rows that sit next to each other are read with one ``pread``.
    """
    ranges = locator.ranges.get(conv_id, ())
    try:
        descriptor = os.open(_journal_file(session_dir, _JOURNAL_INDEX_NAME), os.O_RDONLY)
    except FileNotFoundError:
        return None
    rows = []
    try:
        if _stat_identity(os.fstat(descriptor)) != locator.identity:
            return None
        position = 0
        while position < len(ranges):
            run_start, run_end, last = ranges[position], ranges[position + 1], position + 2
            while last < len(ranges) and ranges[last] - run_end <= 1:
                run_end, last = ranges[last + 1], last + 2
            data = os.pread(descriptor, run_end - run_start, run_start)
            if len(data) != run_end - run_start:
                raise OSError('Short modification journal index read')
            for item in range(position, last, 2):
                rows.append(json.loads(data[ranges[item] - run_start:ranges[item + 1] - run_start]))
            position = last
    finally:
        os.close(descriptor)
    return rows


def _current_log_rows(session_dir, index_identity, log_identity):
    """Parsed rows of the index's append log up to ``log_identity``'s length.

    Only appended bytes are parsed: the retained offset always ends on a
    complete line, and a torn suffix is only ever cut back to its last newline.
    ``None`` when the log at the path is no longer that inode.
    """
    if log_identity is None:
        return []
    with _lock:
        entry = _LOG_ROWS.get(session_dir)
    if (entry is None or entry[0] != index_identity or entry[1] != log_identity[:2]
            or entry[2] > log_identity[2]):
        entry = (index_identity, log_identity[:2], 0, [])
    offset, rows = entry[2], entry[3]
    try:
        handle = open(_journal_file(session_dir, _JOURNAL_TAIL_NAME), 'rb')
    except FileNotFoundError:
        return None
    with handle:
        if tuple(_stat_identity(os.fstat(handle.fileno()))[:2]) != log_identity[:2]:
            return None
        handle.seek(offset)
        data = handle.read(log_identity[2] - offset)
    complete = data.rfind(b'\n') + 1
    if complete:
        with timing_phase('undo_journal_tail', bytes_read=len(data)):
            rows = rows + _log_records(data[:complete])
        with _lock:
            _LOG_ROWS[session_dir] = (index_identity, log_identity[:2], offset + complete, rows)
    return rows


def _conversation_rows(session_dir, conv_id):
    """One conversation's committed rows without decoding the rest of the history.

    The index is scanned once per exact index identity into an
    ``_IndexLocator``; later reads decode that conversation's index rows plus
    the record files and the log's appended lines. ``None`` while a fold
    artifact or a non-ASCII index requires the whole-history read.
    """
    while True:
        identity = _journal_identity(session_dir)
        if identity[0] is None or identity[2]:
            return None
        with _lock:
            locator = _INDEX_LOCATORS.get(session_dir)
        if locator is None or locator.identity != identity[0]:
            locator = _locate_index(session_dir)
            if locator is None:
                return None
            with _lock:
                _INDEX_LOCATORS[session_dir] = locator
            if locator.identity != identity[0]:
                continue
        try:
            committed = _record_rows(session_dir, [name for name in identity[3]
                                                   if name not in locator.absorbed])
        except _RecordVanished:
            continue
        log_rows = _current_log_rows(session_dir, identity[0], identity[1])
        if log_rows is None:
            continue
        index_rows = _located_rows(session_dir, locator, conv_id)
        if index_rows is None or _rewrite_identity(session_dir) != (identity[0], identity[2]):
            continue
        return _ordered(index_rows + [row for row in log_rows + committed
                                      if row.get('convId') == conv_id])


def _metadata_session(base_path):
    """``base_path``'s session dir after pending intents are reconciled, or None.

    Pending write intents without a live writer are reconciled first, as for
    every history read; reading the history itself takes no lock, so a
    projection of a large history cannot hold up the project's writers.
    """
    from lib.project_mod.write_recovery import recover_pending_writes
    session_dir = _get_session_dir(base_path, create=False)
    if not session_dir:
        return None
    recover_pending_writes(base_path)
    return session_dir if os.path.isdir(session_dir) else None


def _metadata_history(base_path):
    """Committed rows of ``base_path``'s journal for read-only projections."""
    session_dir = _metadata_session(base_path)
    return _committed_rows(session_dir) if session_dir else []


def _conversation_history(base_path, conv_id):
    """Committed rows of one conversation for read-only projections.

    A history the cache holds is filtered in memory. One already known to
    outweigh the cache budget is answered from its located index rows and
    appended log lines, so its size never makes each read a full decode.
    """
    session_dir = _metadata_session(base_path)
    if not session_dir:
        return []
    identity = _journal_identity(session_dir)
    limit = min(_MODS_CACHE_MAX_SESSION_BYTES, _MODS_CACHE_MAX_BYTES)
    with _lock:
        oversized = (session_dir not in _mods_cache
                     and _extends_oversized(session_dir, identity, limit))
    if oversized:
        rows = _conversation_rows(session_dir, conv_id)
        if rows is not None:
            return rows
    return [row for row in _committed_rows(session_dir) if row.get('convId') == conv_id]


def modification_cache_snapshot() -> dict[str, int]:
    """Return bounded, content-free cache telemetry for diagnostics/tests."""
    with _lock:
        return {
            'entries': len(_mods_cache),
            'capacity': _MODS_CACHE_CAPACITY,
            'pendingRecords': sum(len(mods) for mods in _mods_cache.values()),
        }


def _flush_to_disk(session_dir, mods, *, loaded=(), absorbed=()):
    """Rewrite the index as ``mods``, absorbing the listed record files.

    The only whole-journal rewrite: a fold, or a whole-history replacement
    (undo). Caller holds the rewrite lock, and ``absorbed`` names every record
    file it read (plus leftovers of an earlier rewrite). Sequence: move a
    legacy log aside, publish the index that contains every kept row AND the
    names of the records and artifact it absorbed, then unlink exactly those.
    A crash before publication changes nothing; after it, readers skip the
    absorbed names and the next rewrite unlinks the leftovers. Records
    committed meanwhile are not in ``absorbed`` and stay authoritative.

    ``loaded`` is the history the caller read before mutating it, so records it
    removed are not taken back from a leftover fold artifact.
    """
    session_file = _journal_file(session_dir, _JOURNAL_INDEX_NAME)
    tail = _journal_file(session_dir, _JOURNAL_TAIL_NAME)
    fold = _journal_file(session_dir, _JOURNAL_FOLD_NAME)
    absorbed = sorted(set(absorbed))
    try:
        if mods or absorbed:
            if mods and os.path.exists(fold):
                # An earlier fold left an artifact behind: keep any record this
                # caller's snapshot does not already carry.
                known = {row.get('changeId') for row in (*mods, *loaded)
                         if row.get('changeId')}
                mods.extend(row for row in _read_tail(session_dir, _JOURNAL_FOLD_NAME)
                            if row.get('changeId') not in known)
            with timing_phase('undo_content_compaction', records=len(mods)):
                compact = undo_payloads.compact_records(session_dir, mods,
                    buffer_budget=_MODS_CACHE_MAX_SESSION_BYTES)
            if os.path.exists(tail):
                os.replace(tail, fold)
                _sync_journal_directory(session_dir)
            absorbed_fold = _fold_identity(session_dir)
            envelope = {'modifications': compact}
            if absorbed_fold is not None:
                envelope['absorbedFold'] = list(absorbed_fold)
            if absorbed:
                envelope['absorbedRecords'] = absorbed
            with timing_phase('undo_index_write', records=len(compact)) as measurement:
                written = _atomic_json_write(session_file, envelope)
                if measurement is not None and type(written) is int:
                    measurement['bytes_written'] += written
            mods[:] = compact
            if absorbed_fold is not None:
                os.unlink(fold)
                _sync_journal_directory(session_dir)
            if absorbed:
                directory = _records_directory(session_dir)
                for name in absorbed:
                    try:
                        os.unlink(os.path.join(directory, name))
                    except FileNotFoundError:
                        pass
                _sync_journal_directory(directory)
                _forget_records(session_dir, absorbed)
            if not mods:
                os.remove(session_file)
                _sync_journal_directory(session_dir)
        else:
            removed = False
            for path in (session_file, tail, fold):
                if os.path.exists(path):
                    os.remove(path)
                    removed = True
            if removed:
                _sync_journal_directory(session_dir)
    except Exception as e:
        logger.error('[Modifications] failed to save %s: %s',
                     session_file, e, exc_info=True)
        raise
    finally:
        # The base identity changed (or is uncertain): validate it again.
        _JOURNAL_VALIDATED.pop(session_dir, None)


def _sync_primary_view(session_dir, mods):
    """Mirror the cached mods for ``session_dir`` into ``_state`` so that
    ``get_state()`` / the UI badge reflects the currently-active primary
    root.  ``_state['modifications']`` is intentionally **read-only** from
    the rest of the codebase — nothing should ever mutate it directly.
    Caller must hold ``_lock``.
    """
    # Only an admitted cache entry may stay resident in the active projection.
    # Oversized history remains available from the authoritative journal API.
    _state['sessionId'] = session_dir
    _state['modifications'] = list(mods) if _mods_cache.get(session_dir) is mods else []


def _locked_rmw(session_dir, mutator):
    """Publish one whole-history rewrite under the rewrite lock.

    Serializes only folds and rewrites; writers keep committing records. The
    mutator receives a private list of the exact history read under the lock
    (index plus every record file then listed) and may return a replacement
    list. The new index absorbs exactly those record files; records committed
    meanwhile stay as files. Only a confirmed durable rewrite updates the
    projection; an uncertain one invalidates it.
    """
    with journal_locks.rewrite_lock(session_dir):
        _clean_stale_tmp(session_dir)
        history = _read_history(session_dir)
        mods = list(history.rows)
        previous = list(mods)
        ret = mutator(mods)
        if isinstance(ret, list):
            mods = ret
        _invalidate_cache(session_dir)
        _flush_to_disk(session_dir, mods, loaded=previous, absorbed=history.identity[3])
        identity = _journal_identity(session_dir)
        try:
            absorbed = set(history.identity[3]) if mods else set()
            later = _record_rows(session_dir, [name for name in identity[3]
                                               if name not in absorbed])
        except _RecordVanished:
            later = None
        if later is not None:
            _publish_cache(session_dir, _ordered(mods + later), identity)
        try:
            undo_payloads.reclaim_removed(session_dir, previous, mods)
        except OSError:
            logger.warning('Undo payload reclamation deferred; committed history remains authoritative')
        return ret


class _Validated:
    """What a full validation of one journal proved, advanced by later commits."""

    __slots__ = ('rewrite', 'log', 'log_end', 'absorbed', 'ids', 'inline')

    def __init__(self, rewrite, log, log_end, absorbed, ids, inline):
        self.rewrite = rewrite
        self.log = log
        self.log_end = log_end
        self.absorbed = absorbed
        self.ids = ids
        self.inline = inline


def _assert_journal_ready(session_dir):
    """Fail closed on an unreadable authority without re-parsing a large index.

    A write must reject a corrupt journal before replacing user bytes, but a full
    parse of an 8 MB index is not an edit cost. Validation is memoized per
    index and fold artifact; log lines appended since the validated offset and
    record files not yet parsed by this process are validated incrementally. A
    rewrite or a replaced log forces a full validation.

    Returns the materialized records when this call performed a full load, else
    ``None``.
    """
    rewrite = _rewrite_identity(session_dir)
    log = _file_identity(_journal_file(session_dir, _JOURNAL_TAIL_NAME))
    memo = _JOURNAL_VALIDATED.get(session_dir)
    if (memo is not None and memo.rewrite == rewrite
            and (log[:2] if log else None) == memo.log and (log[2] if log else 0) >= memo.log_end):
        try:
            if log is not None and log[2] > memo.log_end:
                with open(_journal_file(session_dir, _JOURNAL_TAIL_NAME), 'rb') as handle:
                    handle.seek(memo.log_end)
                    data = handle.read(log[2] - memo.log_end)
                end = data.rfind(b'\n') + 1
                rows = _log_records(data[:end])
                memo.ids.update(row['changeId'] for row in rows if row.get('changeId'))
                memo.log_end += end
            names = _record_names(session_dir)
            _record_rows(session_dir, [name for name in names if name not in memo.absorbed])
        except (_RecordVanished, FileNotFoundError):
            pass
        else:
            if _rewrite_identity(session_dir) == rewrite:
                return None
    history = _read_history(session_dir)
    inline = any(row.get(field) is not None and not undo_payloads.is_reference(row[field])
                 for row in history.index_records for field in undo_payloads.FIELDS)
    log = history.identity[1]
    _JOURNAL_VALIDATED[session_dir] = _Validated(
        (history.identity[0], history.identity[2]), log[:2] if log else None,
        history.log_end, history.absorbed, set(history.logged_ids), inline)
    _sweep_crash_artifacts(session_dir)
    return history.rows


def _committed_change(session_dir, change_id):
    """The committed row of ``change_id``, else None; caller validated the journal.

    Validation parsed every listed record (names carry their changeId) and
    collected the ids of the index, fold artifact and log, so an absent id never
    materializes the history or lists the records again.
    """
    memo = _JOURNAL_VALIDATED.get(session_dir)
    if memo is None:
        _assert_journal_ready(session_dir)
        memo = _JOURNAL_VALIDATED[session_dir]
    known = _known_record(session_dir, change_id)
    if known is not None and known[0] not in memo.absorbed:
        return known[1]
    if change_id not in memo.ids and memo.rewrite == _rewrite_identity(session_dir):
        return None
    return next((row for row in _read_history(session_dir).rows
                 if row.get('changeId') == change_id), None)


def _commit_record(session_dir, record, change_id=None):
    """Durably commit one new record; ``False`` when its identity is present.

    Holds the record path's file lock (reentrantly) and validates the journal
    by identity. If the rewrite lock is free it is taken without waiting and
    the row is appended to the log, together with any record files writers
    left while it was busy; otherwise the row is published as its own record
    file, so a commit never waits for another file's writer, a fold or an
    undo. The row is fsynced before the caller is told the change is recorded,
    and an unreadable journal still fails loudly.
    """
    with journal_locks.path_locks(session_dir, [record['path']]):
        _assert_journal_ready(session_dir)
        if change_id is not None:
            existing = _committed_change(session_dir, change_id)
            if existing is not None:
                if not _same_recovery_evidence(
                        undo_payloads.hydrate_record(session_dir, existing), record):
                    raise _RecoveryIdentityConflict(
                        'Prepared change identity has conflicting journal evidence')
                return False
        packs = []
        try:
            with timing_phase('undo_content_compaction', records=1):
                compact = undo_payloads.compact_records(session_dir, [record],
                    buffer_budget=_MODS_CACHE_MAX_SESSION_BYTES, hold=packs)[0]
            compact['journalKey'] = journal_locks.next_journal_key(session_dir, record['path'])
            with timing_phase('undo_index_write', records=0) as measurement:
                with journal_locks.rewrite_lock(session_dir, blocking=False) as logging:
                    if logging:
                        written = _log_commit(session_dir, compact)
                    else:
                        name = f"{compact['journalKey']}-{compact['changeId']}.json"
                        written = _publish_record(session_dir, name, compact)
                        with _lock:
                            _RECORD_ROWS.setdefault(session_dir, {})[name] = (compact, written)
                if measurement is not None:
                    measurement['bytes_written'] += written
        finally:
            for descriptor in packs:
                os.close(descriptor)
        with _lock:
            # A reader that sampled the entry before this commit must not
            # publish its older snapshot afterwards; later reads extend it.
            _mods_cache_generation[session_dir] = _mods_cache_generation.get(session_dir, 0) + 1
        return True


def _log_commit(session_dir, row):
    """Append ``row`` plus waiting record files to the log; caller holds the rewrite lock.

    Each record's row is durable in the log before its file is unlinked, so a
    crash between leaves a duplicate that readers drop by changeId. A legacy
    inline index or a log past ``_JOURNAL_TAIL_MAX_BYTES`` is then folded into
    the index; a failed fold leaves the committed row authoritative.
    """
    # A rewrite may have finished since the caller validated; under the lock
    # held now nothing else can change the index, fold artifact or log.
    _assert_journal_ready(session_dir)
    memo = _JOURNAL_VALIDATED[session_dir]
    names = [name for name in _record_names(session_dir) if name not in memo.absorbed]
    waiting = _record_rows(session_dir, names)
    rows = [entry for entry in waiting if entry['changeId'] not in memo.ids] + [row]
    end = _append_to_tail(session_dir, rows)
    log = _file_identity(_journal_file(session_dir, _JOURNAL_TAIL_NAME))
    memo.log, memo.log_end = log[:2], end
    memo.ids.update(entry['changeId'] for entry in rows)
    if names:
        directory = _records_directory(session_dir)
        try:
            for name in names:
                try:
                    os.unlink(os.path.join(directory, name))
                except FileNotFoundError:
                    pass
            _sync_journal_directory(directory)
        except OSError:
            logger.warning('Logged record files remain; readers drop the duplicates', exc_info=True)
        _forget_records(session_dir, names)
    if memo.inline or end > _JOURNAL_TAIL_MAX_BYTES:
        try:
            _locked_rmw(session_dir, lambda mods: None)
        except Exception:
            logger.warning('Journal fold deferred; committed rows remain authoritative',
                           exc_info=True)
    return len(_tail_line(row))


def _with_recovered_session(function):
    @wraps(function)
    def recovered(base_path, *args, **kwargs):
        if base_path:
            from lib.project_mod.write_recovery import recover_pending_writes
            _get_session_dir(base_path)
            recover_pending_writes(base_path)
        return function(base_path, *args, **kwargs)
    return recovered


@_with_recovered_session
def _start_new_session(base_path):
    """Warm the cache for ``base_path``'s session_dir and point the
    primary-view mirror at it.  Non-destructive: other session_dirs'
    caches are untouched, so background tasks still running against a
    previously-active project continue to record safely.
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return None
    mods = _cache_get(session_dir)
    with _lock:
        _sync_primary_view(session_dir, mods)
    return session_dir


def _encode_original(original_content, mod):
    """Make ``original_content`` JSON-safe for the modifications record.

    Binary pre-images (captured by run_command when a command touches a
    non-UTF-8 file) arrive as ``bytes``, which ``json.dump`` cannot
    serialise — that would poison both ``modifications.json`` persistence
    and every ``get_state()``/jsonify response that mirrors this list.
    base64-encode bytes into a str and flag it so undo can decode back.
    """
    if isinstance(original_content, bytes):
        mod['originalContentB64'] = True
        return base64.b64encode(original_content).decode('ascii')
    return original_content


def _decode_original(mod):
    """Return a mod's original content as the right type for restore.

    Inverse of :func:`_encode_original`: base64-decodes back to ``bytes``
    when the record was flagged binary, else returns the stored str.
    """
    original = mod.get('originalContent')
    if mod.get('originalContentB64') and isinstance(original, str):
        try:
            return base64.b64decode(original)
        except binascii.Error as error:
            raise FileHistoryFault('Recorded original content is corrupt') from error
    return original


def _with_file_lock(function):
    """Hold the recorded path's file lock (reentrant) around one journal commit."""
    @wraps(function)
    def locked(base_path, mod_type, path, *args, **kwargs):
        session_dir = _get_session_dir(base_path)
        if not session_dir:
            return False
        with journal_locks.path_locks(session_dir, [path]):
            return function(base_path, mod_type, path, *args, **kwargs)
    return locked


def _same_recovery_evidence(existing, proposed):
    for key in ('basePath', 'path', 'convId', 'taskId', 'existed', 'postImage'):
        if existing.get(key) != proposed.get(key):
            return False
    def original(record):
        value = record.get('originalContent')
        if record.get('originalContentB64'):
            return base64.b64decode(value, validate=True)
        return value.encode('utf-8') if isinstance(value, str) else value
    return original(existing) == original(proposed)


class _RecoveryIdentityConflict(ValueError):
    """A change ID must never acknowledge different durable evidence."""


@_with_file_lock
def _record_modification(base_path, mod_type, path, original_content=None, reverse_patch=None, conv_id=None, task_id=None, action=None, fingerprint_byte_budget=None,
                         change_id=None, post_image=None):
    """Record a modification for later undo, tagged with conv_id and task_id.

    Args:
        mod_type: 'write_file', 'apply_diff', or 'run_command'.
        conv_id: Conversation ID for per-conversation rollback.
        task_id: Task ID for per-round rollback (one user message = one task).
        action: Optional live-display action hint in the settlement
            vocabulary (run_command already knows deleted/created/modified
            at record time; other types derive it from ``existed``).
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return False

    if change_id is not None and (not isinstance(change_id, str)
                                  or not re.fullmatch(r'[0-9a-f]{32}', change_id)):
        raise ValueError('Invalid prepared change identity')
    mod = {
        'changeId': change_id or uuid.uuid4().hex,
        'type': mod_type,
        'path': path,
        'timestamp': time.time(),
    }
    # Record the absolute base_path so undo can recover WHICH project a
    #   round belongs to from a taskId/convId alone — without trusting the
    #   globally-active UI project (_state['path']), which may point at a
    #   different project when conversations are edited concurrently.
    try:
        mod['basePath'] = os.path.abspath(base_path) if base_path else ''
    except Exception as e:
        logger.debug('[Modifications] basePath abspath failed for %s: %s', base_path, e)
    # Freeze auxiliary-worktree capability before the checkout can be merged
    # or removed. A task's primary managed checkout keeps normal undo; a
    # manually added checkout sharing its Git history is display evidence,
    # not authority for the single-root undo gateway to undo canonical bytes.
    try:
        from lib.project_mod.config import get_conv_roots
        from lib.project_mod.write_tools._paths import _git_common_dir
        execution_roots = get_conv_roots(conv_id)
        primary = next(iter(execution_roots.values()), {}).get('path')
        abs_base = mod.get('basePath')
        if primary and abs_base:
            for name, state in execution_roots.items():
                if os.path.abspath(state['path']) == abs_base:
                    mod['root'] = name
                    break
            if os.path.abspath(primary) != abs_base:
                common = _git_common_dir(abs_base)
                if common and common == _git_common_dir(primary):
                    mod['undoAvailable'] = False
    except Exception as error:
        logger.debug('[Modifications] auxiliary root capability probe failed: %s',
                     type(error).__name__)
    from lib.project_mod.tool_admission import journal_conv_id
    conv_id = journal_conv_id(conv_id)
    if conv_id:
        mod['convId'] = conv_id
    if task_id:
        mod['taskId'] = task_id

    # Record workspace-root name so the frontend can display a
    #   'rootname:path' prefix for modifications made outside the primary
    #   root in multi-root workspaces.  base_path is the absolute root
    #   path the tool was actually executed against (result of
    #   _resolve_base / resolve_namespaced_path).
    try:
        abs_base = os.path.abspath(base_path) if base_path else ''
        with _lock:
            for rn, rs in _roots.items():
                if os.path.abspath(rs['path']) == abs_base:
                    mod.setdefault('root', rn)
                    break
    except Exception as e:
        logger.debug('[Modifications] root-name lookup failed for %s: %s', base_path, e)

    if mod_type == 'write_file':
        if original_content is not None:
            mod['originalContent'] = _encode_original(original_content, mod)
            mod['existed'] = True
        else:
            mod['existed'] = False
    elif mod_type == 'apply_diff':
        mod['reversePatch'] = reverse_patch  # {search, replace}
        if original_content is not None:
            # Pre-image is also stored as a backup blob (when small enough)
            # so undo can use the deterministic file-history restore path.
            mod['existed'] = True
            mod['originalContent'] = _encode_original(original_content, mod)
    elif mod_type == 'run_command':
        # Absence of backup bytes means capture was unavailable, not that the
        # file was absent. Only the snapshot's explicit creation observation
        # may authorize undo-by-delete. Unknown legacy callers fail closed.
        mod['action'] = action if action in ('created', 'deleted', 'modified') else 'unknown'
        mod['existed'] = action != 'created' or original_content is not None
        if original_content is not None:
            mod['originalContent'] = _encode_original(original_content, mod)

    # Capture what this change left on disk, separately from its pre-image.
    # Content evidence survives atomic inode replacement during multi-step
    # rollback, while detecting same-size edits on coarse timestamp filesystems.
    if post_image is not None:
        # Already verified by the prepared write; do not re-read/hash the file
        # only to discard that sample in favor of the durable intent evidence.
        mod['postImage'] = dict(post_image)
    else:
        try:
            from lib.project_mod.undo_filesystem import fingerprint_file
            target = _resolve_undo_target(base_path, mod)
            mod['postImage'] = fingerprint_file(
                base_path, target, byte_budget=fingerprint_byte_budget,
                file_byte_limit=MAX_FILE_SIZE if mod_type == 'run_command' else None)
        except (OSError, TypeError, ValueError):
            mod['postImage'] = {'kind': 'unavailable'}

    try:
        duplicate = _commit_record(session_dir, mod, change_id) is False
    except _RecoveryIdentityConflict:
        raise
    except Exception as e:
        logger.error('[Modifications] record failed type=%s path=%s session=%s: %s',
                     mod_type, path, os.path.basename(session_dir), e, exc_info=True)
        return False

    logger.debug('Recorded modification: %s %s (conv=%s task=%s session=%s)',
                 mod_type, path, conv_id or '?', task_id or '?',
                 os.path.basename(session_dir))

    if duplicate:
        return True

    # This durable side projection still uses a process-local history lock.
    # Preserve cross-process journal commit order until it has its own ordering
    # fence: process-local deferred lanes alone cannot prevent a late pre-image
    # from regressing the tracked version after another process snapshots it.
    # Exact undo evidence is already durable; no second journal rewrite is needed.
    try:
        if mod.get('existed', True) and original_content is not None:
            from lib import file_history as fh
            with timing_phase('file_history'):
                fh.track_edit(base_path, path, message_id=task_id,
                              pre_content=original_content, task_id=task_id)
    except Exception as e:
        logger.debug('[Modifications] file-history track_edit failed for %s: %s', path, e)

    listeners = tuple(_live_change_listeners) if task_id else ()
    if not listeners:
        return True

    def project_committed_change():
        entry = {
            'path': path, 'changeId': mod['changeId'], 'source': 'local',
            **({'rootPath': mod['basePath']} if mod.get('basePath') else {}),
            'action': action or _default_live_action(mod_type, mod.get('existed', True)),
            **({'root': mod['root']} if mod.get('root') else {}),

            **({'undoAvailable': False} if mod.get('undoAvailable') is False else {}),
        }
        for listener in listeners:
            try:
                listener(task_id, entry)
            except Exception as e:
                logger.debug('[Modifications] live-change listener failed for %s: %s', path, e)

    journal_locks.after_release(session_dir, project_committed_change, key=path)
    return True


def get_modifications(base_path, conv_id=None, *, include_content=True):
    """Get list of pending modifications for undo, optionally filtered by conv_id.

    Metadata rows (``include_content=False``) never take the journal admission;
    hydrated contents keep it from selection through their exact byte reads.
    """
    if not include_content:
        rows = (_conversation_history(base_path, conv_id) if conv_id
                else _metadata_history(base_path))
        return [dict(mod) for mod in rows]
    return _hydrated_modifications(base_path, conv_id)


@_with_recovered_session
def _hydrated_modifications(base_path, conv_id):
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return []
    # Hold the rewrite lock shared through selected hydration: a concurrent
    # undo cannot reclaim a pack between the snapshot and this exact byte read.
    with journal_locks.rewrite_lock(session_dir, shared=True):
        mods = list(_cache_get(session_dir))
        if conv_id:
            mods = [m for m in mods if m.get('convId') == conv_id]
        return [undo_payloads.hydrate_record(session_dir, mod) for mod in mods]


def get_task_modification_paths(base_path, task_id, *, conv_id=None, limit,
                                exclude=()):
    """Project at most ``limit`` distinct exact-task file addresses.

    Pending write intents are reconciled before selection, as for every history
    read; the history itself is read without journal admission. A warm cache is
    borrowed without copying historical rows or hydrating undo contents. A cold
    cache still loads the complete authoritative index; this is a bounded
    projection, not a new index. Returned dictionaries contain only immutable
    string values, never cache rows.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError('Modification path limit must be a nonnegative integer')
    if not task_id or not limit:
        return []
    seen = set(exclude)
    result = []
    rows = (_conversation_history(base_path, conv_id) if conv_id
            else _metadata_history(base_path))
    for row in rows:
        if row.get('taskId') != task_id:
            continue
        path = row.get('path')
        root = row.get('basePath') or base_path
        if not isinstance(path, str) or not path or str(root).startswith('remote:'):
            continue
        address = (path, str(root))
        if address in seen:
            continue
        seen.add(address)
        result.append({'path': path, 'rootPath': str(root)})
        if len(result) >= limit:
            break
    return result


def get_conv_ids_with_modifications(base_path):
    """Get list of conversation IDs that have pending modifications."""
    return list({m.get('convId') for m in _metadata_history(base_path) if m.get('convId')})


def _resolve_undo_target(base_path, modification):
    """Validate stored path attribution before any undo reads or mutations.

    The journal is evidence of a change, not authority to access another root.
    Symlinks and special files are not interchangeable with tracked files.
    """
    base = os.path.realpath(os.path.abspath(base_path))
    recorded_base = modification.get('basePath')
    if recorded_base and os.path.realpath(os.path.abspath(recorded_base)) != base:
        raise ValueError('Modification belongs to a different project root')
    path = modification['path']
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ValueError('Invalid modification path')
    target = os.path.abspath(os.path.join(base, path))
    if target == base or os.path.commonpath([base, target]) != base:
        raise ValueError('Undo path escapes the selected project root')
    relative = os.path.relpath(target, base)
    current = base
    parts = relative.split(os.sep)
    for index, component in enumerate(parts):
        current = os.path.join(current, component)
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            break
        reparse_point = getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
        if stat.S_ISLNK(metadata.st_mode) or reparse_point:
            raise ValueError('Undo path contains a symlink or reparse point')
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError('Undo parent is not a directory')
        if index == len(parts) - 1 and not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Undo target is not a regular file')
    return target


def _undo_modifications_list(base_path, modifications, *, completed_change_ids=None):
    """Internal: undo a list of modifications in reverse order. Returns (undone, failed).

    Each failed row is ``{path, reason, refused}``. ``refused: True`` is the
    engine's authored answer: missing or stale evidence, or a target that the
    path validator or the anchored undo filesystem (``ValueError``) refuses.
    Any other exception is a fault whose reason discloses only its type.
    """
    from lib.project_mod import undo_filesystem
    undone = []
    failed = []
    blocked_paths = set()
    from lib.project_mod.write_intents import recovery_file_bytes
    fingerprint_byte_budget = [max(UNDO_BATCH_CONTENT_BYTES, recovery_file_bytes() + 1)]

    def mark_undone(entry):
        undone.append(entry)
        change_id = mod.get('changeId')
        if completed_change_ids is not None and isinstance(change_id, str) and change_id:
            completed_change_ids.add(change_id)

    def mark_failed(entry):
        failed.append(entry)
        blocked_paths.add(os.path.abspath(target))

    def refuse(reason):
        mark_failed({'path': path, 'reason': reason, 'refused': True})

    # Optional file-history short-circuit: if the mod carries a backup
    # version we can restore deterministically from the backup blob.
    try:
        from lib import file_history as fh
        from lib.file_history.store import read_blob as _fh_read_blob
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[Modifications] file_history import failed: %s', e)
        fh = None
        _fh_read_blob = None
    for mod in reversed(modifications):
        mod_type = mod['type']
        path = mod['path']
        try:
            target = _resolve_undo_target(base_path, mod)
        except ValueError as exc:
            failed.append({'path': path, 'reason': str(exc), 'refused': True})
            continue
        except (OSError, TypeError) as exc:
            logger.error('Undo target check failed for %s: %s', path, exc, exc_info=True)
            failed.append({'path': path, 'reason': FileHistoryFault.reason(exc), 'refused': False})
            continue
        if os.path.abspath(target) in blocked_paths:
            refuse('A later change to this path could not be undone')
            continue
        try:
            mod = undo_payloads.hydrate_record(_get_session_dir(base_path), mod)
        except (OSError, ValueError, TypeError) as exc:
            logger.error('Undo payload for %s is unavailable: %s', path, exc, exc_info=True)
            mark_failed({'path': path, 'reason': 'Undo payload is unavailable or corrupt',
                         'refused': False})
            continue
        post_image = mod.get('postImage')
        legacy_patch = post_image is None and mod_type == 'apply_diff' and 'reversePatch' in mod
        if not legacy_patch:
            if (not isinstance(post_image, dict) or post_image.get('kind') not in ('content', 'absent')
                    or not undo_filesystem.valid_postimage(post_image)):
                refuse('Undo has no verified post-image evidence')
                continue
            current_image = undo_filesystem.fingerprint_file(
                base_path, target, byte_budget=fingerprint_byte_budget)
            if current_image != post_image:
                refuse('File changed after the recorded modification')
                continue
        # Legacy reverse patches may match their exact replacement against a
        # bounded live read, but must never bypass that check via a full blob.
        # ── Preferred path: file-history blob restore (works for any mod_type) ──
        try:
            v = mod.get('fhVersion')
            if (v and not legacy_patch and mod.get('existed', True)
                    and fh and fh.is_enabled() and _fh_read_blob):
                blob = _fh_read_blob(base_path, path, int(v))
                if blob is not None:
                    undo_filesystem.write_file(base_path, target, blob, expected_image=post_image)
                    _nudge_vscode(base_path, target)
                    mark_undone({'type': 'fh_restore', 'path': path})
                    logger.info('Undo: restored %s via fh@v%s', path, v)
                    continue
        except Exception as e:
            logger.debug('[Modifications] fh undo fallback for %s: %s', path, e)
        try:
            if mod_type == 'write_file':
                if not mod.get('existed', True):
                    if os.path.exists(target):
                        undo_filesystem.remove_file(base_path, target, expected_image=post_image)
                        mark_undone({'type': 'delete', 'path': path})
                        logger.info('Undo: deleted created file %s', path)
                else:
                    if 'originalContent' in mod and os.path.exists(target):
                        original = _decode_original(mod)
                        undo_filesystem.write_file(base_path, target, original, expected_image=post_image)
                        _nudge_vscode(base_path, target)
                        mark_undone({'type': 'restore', 'path': path})
                        logger.info('Undo: restored original content for %s', path)
                    else:
                        refuse('No original content available')
            elif mod_type == 'apply_diff':
                if 'reversePatch' in mod and os.path.exists(target):
                    content = undo_filesystem.read_text(base_path, target)
                    rev = mod['reversePatch']
                    # rev['search'] = the text currently in file (the replacement)
                    # rev['replace'] = the original text we want to restore
                    if rev['search'] not in content:
                        refuse('Reverse patch target no longer matches')
                        continue
                    new_content = content.replace(rev['search'], rev['replace'], 1)
                    undo_filesystem.write_file(base_path, target, new_content,
                                               expected_content=content.encode('utf-8'), expected_image=post_image)
                    _nudge_vscode(base_path, target)
                    mark_undone({'type': 'reverse_patch', 'path': path})
                    logger.info('Undo: reversed patch for %s', path)
                else:
                    refuse('No reverse patch available')
            elif mod_type == 'run_command':
                if not mod.get('existed', True) and mod.get('action') != 'created':
                    refuse('Legacy command record does not prove file creation')
                    continue
                if not mod.get('existed', True):
                    # File was CREATED by the command → delete it
                    if os.path.exists(target):
                        undo_filesystem.remove_file(base_path, target, expected_image=post_image)
                        mark_undone({'type': 'delete', 'path': path})
                        logger.info('Undo run_command: deleted created file %s', path)
                    else:
                        mark_undone({'type': 'already_gone', 'path': path})
                else:
                    # File was DELETED or MODIFIED → restore original content
                    if 'originalContent' in mod:
                        original = _decode_original(mod)
                        undo_filesystem.write_file(base_path, target, original, expected_image=post_image)
                        _nudge_vscode(base_path, target)
                        mark_undone({'type': 'restore', 'path': path})
                        logger.info('Undo run_command: restored %s', path)
                    else:
                        refuse('No original content saved for run_command change')
        except ValueError as e:
            # The anchored undo filesystem refuses a target that changed or
            # became unsafe after the evidence check, with an authored reason.
            refuse(str(e))
            logger.info('Undo refused for %s: %s', path, e)
        except Exception as e:
            mark_failed({'path': path, 'reason': FileHistoryFault.reason(e), 'refused': False})
            logger.error('Undo failed for %s: %s', path, e, exc_info=True)
    return undone, failed


def _undo_selection(base_path, session_dir, select):
    """Undo the selected committed rows; ``(selected, undone, failed)``.

    Holds the rewrite lock from selection through the index rewrite, and the
    file locks of every selected path while restoring and rewriting, so no
    writer of those files interleaves. Writers of other files keep committing;
    their records survive the rewrite.
    """
    with journal_locks.rewrite_lock(session_dir):
        selected = [m for m in _cache_get(session_dir) if select(m)]
        while True:
            paths = {m['path'] for m in selected}
            with journal_locks.path_locks(session_dir, paths):
                current = [m for m in _committed_rows(session_dir) if select(m)]
                if not {m['path'] for m in current} <= paths:
                    selected = current
                    continue
                if not current:
                    return 0, [], []
                completed_change_ids = set()
                undone, failed = _undo_modifications_list(
                    base_path, current, completed_change_ids=completed_change_ids)
                _locked_rmw(session_dir, lambda mods: [
                    m for m in mods if m.get('changeId') not in completed_change_ids])
                return len(current), undone, failed


@_with_recovered_session
def undo_conv_modifications(base_path, conv_id):
    """Undo modifications for a specific conversation (对话粒度回撤)."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'refused': True, 'error': 'No session found'}

    selected, undone, failed = _undo_selection(
        base_path, session_dir, lambda m: m.get('convId') == conv_id)
    if not selected:
        return {'ok': True, 'message': 'No modifications to undo for this conversation',
                'undone': 0, 'failed': 0}

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'convId': conv_id,
        'details': {'undone': undone, 'failed': failed},
    }


@_with_recovered_session
def undo_task_modifications(base_path, task_id):
    """Undo modifications for a specific task/round (本轮对话回撤).

    Each user message creates one task. This undoes only the file changes
    made during that single round of conversation.
    """
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'refused': True, 'error': 'No session found'}

    selected, undone, failed = _undo_selection(
        base_path, session_dir, lambda m: m.get('taskId') == task_id)
    if not selected:
        return {'ok': True, 'message': 'No modifications to undo for this task',
                'undone': 0, 'failed': 0}

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'taskId': task_id,
        'details': {'undone': undone, 'failed': failed},
    }


def redo_task_modifications(base_path, task_id):
    """Re-apply a previously-undone round via the file-history snapshot.

    Unlike undo_*, this does NOT replay individual modification records —
    instead it locates the snapshot tagged with ``task_id`` and calls
    :func:`lib.file_history.restore_from` to re-apply the file versions that
    round changed with per-file anchored mutations. Returns the same shape as
    :func:`undo_task_modifications` so the frontend can reuse display code.

    A refusal is ``ok: False`` with ``refused: True``; per-file failures keep
    their typed ``details.failed`` rows. A fault that leaves no per-file row
    raises :class:`FileHistoryFault`.
    """
    try:
        from lib import file_history as fh
    except Exception as e:
        logger.warning('[Modifications] redo requires file_history: %s', e)
        raise FileHistoryFault('Redo is unavailable: file history could not be loaded') from e
    if not fh.is_enabled():
        return {'ok': False, 'refused': True,
                'error': 'File-history disabled (TOFU_FILE_HISTORY=0)'}
    if not task_id:
        return {'ok': False, 'refused': True, 'error': 'taskId is required'}
    snap_id = fh.find_latest_snapshot_id(base_path, task_id=task_id)
    if not snap_id:
        return {'ok': False, 'refused': True, 'taskId': task_id,
                'error': 'No snapshot found for this task (was it ever committed?)'}
    result = fh.restore_from(base_path, snap_id)
    if not result.get('ok'):
        failed = result.get('failed', [])
        if not failed and result.get('refused') is not True:
            # restore_from logged the cause; its error envelope is not user text.
            raise FileHistoryFault('Redo failed because of an internal file-history error')
        files = [f['path'] for f in result.get('files', [])]
        return {'ok': False, 'taskId': task_id,
                'refused': all(row.get('refused') is True for row in failed),
                'error': result.get('error') or 'restore failed',
                'redone': len(files), 'files': files,
                'details': {'failed': failed}}
    files = [f.get('path') for f in result.get('files', []) if f.get('path')]
    logger.info('[Modifications] redo task=%s files=%d snap=%s',
                task_id, len(files), snap_id[:8])
    return {
        'ok': True,
        'taskId': task_id,
        'redone': len(files),
        'files': files,
        'sha': result.get('newSnapshotId'),
        'snapshotId': result.get('newSnapshotId'),
    }


@_with_recovered_session
def undo_all_modifications(base_path):
    """Undo all recorded modifications in reverse order."""
    session_dir = _get_session_dir(base_path)
    if not session_dir:
        return {'ok': False, 'refused': True, 'error': 'No session found'}

    selected, undone, failed = _undo_selection(base_path, session_dir, lambda m: True)
    if not selected:
        return {'ok': True, 'message': 'No modifications to undo', 'undone': 0, 'failed': 0}

    return {
        'ok': True,
        'undone': len(undone),
        'failed': len(failed),
        'details': {'undone': undone, 'failed': failed},
    }


