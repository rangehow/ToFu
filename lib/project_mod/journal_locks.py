"""Per-file journal locks and the history-rewrite lock of one project journal.

Entry points: ``path_locks(session_dir, paths)`` (writers of those attributed
files, blocking up to the write-intent contract's ``lockWaitSeconds``),
``try_path_lock`` (recovery and inspection: is a live writer holding the
path?), ``next_journal_key`` (per-file commit order, under the path lock),
``rewrite_lock`` (folds, undo and other whole-history rewrites) and
``after_release`` (presentation callbacks after this thread's last release).

There is no project-wide writer admission (owner decision 2026-11-10,
``docs/PROJECT_FILE_UNDO.md``): writers of different files never wait for each
other. A path's lock is a cross-process ``file_lock`` (``flock``, or
``LockFileEx`` on Windows) on
``<session_dir>/file-locks/<sha256(path)>.lock`` behind an in-process per-path
lock, so threads queue in-process and only one thread per process polls the
file lock. A thread may re-enter locks it holds. Multi-path holders lock in
sorted order. Rewriters take the rewrite lock before path locks; writers only
ever try the rewrite lock without waiting, so that order cannot deadlock.

The lock file also stores the path's last commit sequence and time, read and
replaced only by the holder. Holder diagnostics there are best effort.
Dependencies: the standard library, ``file_lock``, ``post_commit`` and tool
admission context.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import stat
import threading
import time

from lib.project_mod import file_lock, post_commit
from lib.tools.execution_timing import timing_phase

_LOCK_DIRECTORY = 'file-locks'
_REWRITE_LOCK_NAME = '.journal-rewrite.lock'
# A holder in another process is polled at this interval (as the retired
# project admission did); in-process waiters are woken directly.
_POLL_SECONDS = 0.01

_guard = threading.Lock()
_held = threading.local()
_active_descriptors = set()
# (session_dir, key) -> _Local: the in-process owner of one path or rewrite lock.
_locals = {}


class JournalLockError(OSError):
    """A journal lock could not be taken safely; nothing was mutated."""


class JournalLockBusy(JournalLockError):
    """The file (or history rewrite) is held by another live operation."""

    def __init__(self, waited_seconds, session_dir, subject, holders=()):
        self.waited_seconds = waited_seconds
        self.session_dir = session_dir
        self.subject = subject
        self.holders = tuple(holders)
        holder = (' Held by ' + '; '.join(self.holders) + '.' if self.holders
                  else ' Held by another operation.')
        super().__init__(
            f'Journal lock for {subject} is busy after {waited_seconds:.2f}s; '
            'no file mutation was performed. Wait for the active operation on this '
            'file, then re-read the file and recompute the edit before retrying.'
            + holder + f' Journal: {session_dir}.')


class _Local:
    __slots__ = ('lock', 'owner', 'depth', 'descriptor', 'evidence', 'users', 'shared')

    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self.depth = 0
        self.shared = False
        self.descriptor = None
        self.evidence = None
        self.users = 0


def _after_fork_child():
    """Drop inherited ownership without unlocking the parent's file descriptions."""
    global _held
    try:
        for descriptor in _active_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _active_descriptors.clear()
        _locals.clear()
        _held = threading.local()
    finally:
        _guard.release()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_guard.acquire, after_in_parent=_guard.release,
                        after_in_child=_after_fork_child)


def path_key(path):
    """Stable lock name of one attributed relative path."""
    return hashlib.sha256(path.encode('utf-8', 'surrogatepass')).hexdigest()


def _wait_seconds():
    from lib.project_mod.write_intents import _limit
    return _limit('lockWaitSeconds')


def _operation():
    from lib.project_mod.tool_admission import cancellation
    control = cancellation()
    if control is None:
        return None, None
    task = control.task if isinstance(control.task, dict) else {}
    return control.tool_name or None, task.get('id')


def _describe(evidence, now_wall):
    """Name the holder by its tool call and task, not only its thread.

    Every task's tool pool names its first worker ``tool-parallel_0``, so a
    thread name alone cannot tell a sibling call from another conversation.
    """
    tool, task = evidence.get('tool'), evidence.get('task')
    where = f'thread {evidence.get("thread")}, pid {evidence.get("pid")}'
    if tool:
        _tool, own_task = _operation()
        relation = ''
        if task is not None and own_task is not None:
            relation = ' in this task' if task == own_task else ' in another task'
        who = f'{tool}{relation} ({where})'
    else:
        who = f'{where} outside a project tool call'
    since = evidence.get('since')
    held = f' for {max(0.0, now_wall - since):.2f}s' if isinstance(since, (int, float)) else ''
    return who + held


def _thread_holds():
    holds = getattr(_held, 'holds', None)
    if holds is None:
        holds = _held.holds = {}
    return holds


def _open_lock_file(path):
    with _guard:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                             | getattr(os, 'O_CLOEXEC', 0), 0o600)
        _active_descriptors.add(descriptor)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise JournalLockError('Journal lock is not a regular file')
    except BaseException:
        _close(descriptor)
        raise
    return descriptor


def _close(descriptor):
    with _guard:
        _active_descriptors.discard(descriptor)
        os.close(descriptor)


def _read_state(descriptor):
    try:
        data = os.pread(descriptor, 1 << 16, 0)
        state = json.loads(data) if data.strip() else {}
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(descriptor, state, *, durable):
    payload = json.dumps(state, separators=(',', ':')).encode('ascii')
    os.pwrite(descriptor, payload, 0)
    os.ftruncate(descriptor, len(payload))
    if durable:
        os.fsync(descriptor)


def _lock_path(session_dir, key):
    if key == _REWRITE_LOCK_NAME:
        return os.path.join(session_dir, _REWRITE_LOCK_NAME)
    return os.path.join(session_dir, _LOCK_DIRECTORY, key + '.lock')


def _ensure_lock_directory(session_dir):
    directory = os.path.join(session_dir, _LOCK_DIRECTORY)
    if not os.path.isdir(directory):
        from lib.project_mod.modifications import _ensure_durable_session_directory
        _ensure_durable_session_directory(directory)


def _local(directory, key):
    with _guard:
        entry = _locals.setdefault((directory, key), _Local())
        entry.users += 1
        return entry


def _leave(directory, key, entry):
    with _guard:
        entry.users -= 1
        if entry.users == 0 and _locals.get((directory, key)) is entry:
            del _locals[(directory, key)]


def _acquire(directory, key, subject, *, deadline, started, shared=False, reentrant=True):
    """Take one lock for this thread; ``False`` only when ``deadline`` is None and busy.

    Raises JournalLockBusy once a blocking wait reaches ``deadline``.
    """
    holds = _thread_holds()
    held = holds.get((directory, key))
    if held is not None:
        if not reentrant:
            return False
        held.depth += 1
        return True
    entry = _local(directory, key)
    locally = False
    descriptor = None
    try:
        timeout = -1 if deadline is None else max(0.0, deadline - time.monotonic())
        if not entry.lock.acquire(blocking=deadline is not None, timeout=timeout):
            if deadline is None:
                _leave(directory, key, entry)
                return False
            with _guard:
                evidence = entry.evidence
            raise JournalLockBusy(time.monotonic() - started, directory, subject,
                                  [_describe(evidence, time.time())] if evidence else ())
        locally = True
        descriptor = _open_lock_file(_lock_path(directory, key))
        while True:
            try:
                file_lock.lock(descriptor, shared=shared, blocking=False)
                break
            except BlockingIOError:
                if deadline is None:
                    _close(descriptor)
                    entry.lock.release()
                    _leave(directory, key, entry)
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    holder = _read_state(descriptor).get('holder')
                    raise JournalLockBusy(
                        time.monotonic() - started, directory, subject,
                        [_describe(holder, time.time())] if isinstance(holder, dict) else ())
                time.sleep(min(_POLL_SECONDS, remaining))
        current = os.stat(_lock_path(directory, key), follow_symlinks=False)
        metadata = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise JournalLockError('Journal lock changed during acquisition')
        tool, task = _operation()
        evidence = {'pid': os.getpid(), 'thread': threading.current_thread().name,
                    'tool': tool, 'task': task, 'since': time.time()}
        with _guard:
            entry.owner = threading.get_ident()
            entry.descriptor = descriptor
            entry.evidence = evidence
        entry.depth = 1
        entry.shared = shared
        holds[(directory, key)] = entry
        if not shared and key != _REWRITE_LOCK_NAME:
            try:
                state = _read_state(descriptor)
                state['holder'] = evidence
                _write_state(descriptor, state, durable=False)
            except OSError:
                pass  # Diagnostics only; the flock is the exclusion.
        return True
    except BaseException:
        if holds.get((directory, key)) is entry:
            del holds[(directory, key)]
            with _guard:
                entry.owner = entry.descriptor = entry.evidence = None
        if descriptor is not None and descriptor in _active_descriptors:
            _close(descriptor)
        if locally:
            entry.lock.release()
        _leave(directory, key, entry)
        raise


def _release(directory, key, owner_pid):
    if os.getpid() != owner_pid:
        # A forked child unwinding its parent's context: the at-fork hook
        # already dropped the inherited ownership and closed the descriptor.
        return
    holds = _thread_holds()
    entry = holds[(directory, key)]
    entry.depth -= 1
    if entry.depth:
        return
    del holds[(directory, key)]
    descriptor = entry.descriptor
    with _guard:
        entry.owner = None
        entry.descriptor = None
        entry.evidence = None
    if descriptor is not None and descriptor in _active_descriptors:
        # Closing releases the lock too, but Windows may release a closed
        # handle's lock late; unlock first so a polling waiter sees it free.
        try:
            file_lock.unlock(descriptor)
        finally:
            _close(descriptor)
    entry.lock.release()
    _leave(directory, key, entry)


@contextmanager
def _session_scope(directory):
    """Collect presentation callbacks until this thread releases its last lock."""
    scopes = getattr(_held, 'scopes', None)
    if scopes is None:
        scopes = _held.scopes = {}
    outer = directory not in scopes
    if outer:
        scopes[directory] = []
    owner_pid = os.getpid()
    try:
        yield
    finally:
        if outer and os.getpid() == owner_pid:
            post_commit.publish(scopes.pop(directory))


@contextmanager
def path_locks(session_dir, paths):
    """Hold every attributed path's lock of one journal, in sorted key order."""
    directory = os.path.abspath(session_dir)
    owner_pid = os.getpid()
    keys = sorted({path_key(path): path for path in paths}.items())
    started = time.monotonic()
    deadline = started + _wait_seconds()
    acquired = []
    if keys:
        _ensure_lock_directory(directory)
    with _session_scope(directory):
        try:
            with timing_phase('undo_journal_lock_wait'):
                for key, path in keys:
                    _acquire(directory, key, f'file {path}', deadline=deadline, started=started)
                    acquired.append(key)
            yield
        finally:
            for key in reversed(acquired):
                _release(directory, key, owner_pid)


@contextmanager
def try_path_lock(session_dir, path):
    """Yield whether ``path`` had no live writer and is now held by this thread.

    Never waits and never reenters: a lock this thread already holds belongs to
    an operation in progress, so it reports busy like any other live holder.
    """
    directory = os.path.abspath(session_dir)
    owner_pid = os.getpid()
    key = path_key(path)
    _ensure_lock_directory(directory)
    with _session_scope(directory):
        got = _acquire(directory, key, f'file {path}', deadline=None,
                       started=time.monotonic(), reentrant=False)
        try:
            yield got
        finally:
            if got:
                _release(directory, key, owner_pid)


def held_paths(session_dir):
    """Lock keys this thread holds in ``session_dir``."""
    directory = os.path.abspath(session_dir)
    return {key for (owner, key) in _thread_holds()
            if owner == directory and key != _REWRITE_LOCK_NAME}


def next_journal_key(session_dir, path):
    """Allocate the next commit key of ``path``; the caller holds its lock.

    Time orders commits of different files approximately; for one file the time
    is forced strictly above its previous commit, so file order is exact.
    """
    directory = os.path.abspath(session_dir)
    entry = _thread_holds().get((directory, path_key(path)))
    if entry is None:
        raise JournalLockError('Journal commit requires the file lock of its path')
    state = _read_state(entry.descriptor)
    previous_ns = state.get('lastNs') if type(state.get('lastNs')) is int else 0
    sequence = (state.get('sequence') if type(state.get('sequence')) is int else 0) + 1
    moment = max(time.time_ns(), previous_ns + 1)
    state.update(sequence=sequence, lastNs=moment)
    # Not fsynced: the state survives a process crash in the page cache; only a
    # host crash combined with a clock stepped backwards could reorder this
    # file's next commit, and the record itself is durable either way.
    _write_state(entry.descriptor, state, durable=False)
    return f'{moment:020d}-{sequence:012d}'


@contextmanager
def rewrite_lock(session_dir, *, shared=False, blocking=True):
    """Serialize folds, undo and hydration against each other; yields acquired.

    Writers never wait here: they try a fold with ``blocking=False``. Content
    hydration takes it shared so an undo cannot reclaim a selected payload
    before its exact read. Reentrant on one thread (an exclusive hold covers a
    nested shared request).
    """
    directory = os.path.abspath(session_dir)
    owner_pid = os.getpid()
    started = time.monotonic()
    holds = _thread_holds()
    held = holds.get((directory, _REWRITE_LOCK_NAME))
    if held is not None:
        if held.shared and not shared:
            if blocking:
                raise JournalLockError('A shared history hold cannot become a rewrite')
            yield False
            return
        held.depth += 1
        try:
            yield True
        finally:
            _release(directory, _REWRITE_LOCK_NAME, owner_pid)
        return
    got = _acquire(directory, _REWRITE_LOCK_NAME, 'history rewrite',
                   deadline=started + _wait_seconds() if blocking else None,
                   started=started, shared=shared)
    try:
        yield got
    finally:
        if got:
            _release(directory, _REWRITE_LOCK_NAME, owner_pid)


def after_release(session_dir, callback, *, key=None):
    """Run an optional projection after this thread releases its journal locks.

    Same-file notifications in this process keep commit order; independent
    files drain independently. Not a cross-process fence for durable history.
    """
    directory = os.path.abspath(session_dir)
    scopes = getattr(_held, 'scopes', None)
    if not scopes or directory not in scopes:
        raise RuntimeError('Post-commit effects require a held journal lock')
    scopes[directory].append(post_commit.register(directory, key, callback))
