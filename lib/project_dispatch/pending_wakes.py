"""Durable pending wakes: recovery passes that did not complete, retained until one does.

A Coordinator wake is derived, never queued: the dispatch record and Turn
evidence decide whether a continuation is due (``continuation._proposal``), and
its deterministic command identity makes every repeated derivation the same
wake. What must outlive a pass that could not finish (a lost snapshot read or
CAS, an unavailable executor, a process exit) is only the obligation to derive
it again. One ``pendingWakes`` record per owner and canonical project, in the
dispatch contract's ``wakeNamespace``, holds these obligations as entries:

- ``source:<attempt>``: one request's Coordinator wake;
- ``recipient:<conversation>``: a settlement drain of one lane that failed
  before it reached its requests;
- ``dependents:<assignment>``: the wake of one prerequisite's dependents;
- ``project``: every open request of the project (a timed or commitment wake).

An entry is not authority and never bypasses a proposal's own validation. It is
processed until a pass for it completes by: every later recovery pass of the
project, the next dispatch commit in this process that wrote a document the
entry's pass reads (``written_entries``: the writer whose commit made a snapshot
read lose is that next storage event; a commit of unrelated rows cannot change
the pass's outcome, so it runs nothing), the task-maintenance tick and startup.
Completion removes it under the record CAS.

Each entry is one fault episode: its first failure records one typed Watchdog
signal whose identity names the episode (``since``), later failures of the same
episode add none, and completion resolves that signal. A failure after
completion opens a new episode, so an old signal never absorbs a new fault.

Entry points: retain, complete, notify_commit, process, process_all.
Dependencies: the dispatch contract, the portable record CAS, recovery and
continuation (lazily), the Watchdog signal producers (lazily).
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import threading
import time
import uuid

from lib.log import get_logger
from lib.storage.errors import StorageError

from .repository import CONTRACT, LIMITS, _validate_schema

logger = get_logger(__name__)

NAMESPACE = CONTRACT['wakeNamespace']
PROJECT = 'project'


def source_entry(source_attempt_id: str) -> str:
    return 'source:' + source_attempt_id


def recipient_entry(conversation_id: str) -> str:
    return 'recipient:' + conversation_id


def dependents_entry(assignment_id: str) -> str:
    return 'dependents:' + assignment_id


def written_entries(changes):
    """The entries whose pass reads a document a project document commit wrote.

    A ``source:`` pass reads its request's rows, settlement and request budget;
    ``recipient:`` its lane's rows (as recipient or Coordinator); ``dependents:``
    the prerequisite's row and the rows naming it. A ``project`` pass discovers
    from the order indexes, which only a head commit changes. Returns None (every
    entry) for a change this cannot attribute.
    """
    names = set()
    for change in changes:
        record = (change.get('value') or {}).get('record')
        collection = change.get('collection')
        if collection == 'assignments' and isinstance(record, Mapping):
            request = record['request']
            delegation = request.get('delegation') or {}
            if delegation:
                names.add(source_entry(delegation['attemptId']))
            names.update((recipient_entry(request['recipientId']), recipient_entry(request['coordinatorId']),
                          dependents_entry(record['id'])))
            names.update(dependents_entry(ref['assignmentId'])
                         for ref in request.get('dependencies', []) + ([request['reworkOf']]
                                                                      if request.get('reworkOf') else []))
        elif collection == 'settlements' and isinstance(record, Mapping):
            names.add(source_entry(record['sourceAttemptId']))
        elif collection == 'requests' and isinstance(record, Mapping):
            names.update(source_entry(account['attemptId']) for account in record['accounts'])
        else:
            return None
    return frozenset(names)


def _client(client, *, write=False):
    if client is None:
        from lib.storage import get_storage_client
        return get_storage_client(write=write)
    return client(write=write)


def _key(owner: int, project: str) -> str:
    return f'{owner}:{hashlib.sha256(project.encode("utf-8")).hexdigest()}'


class PendingWakes:
    """One owner/project record of pending wake entries under its own record CAS."""

    def __init__(self, owner: int, project: str, *, client=None, clock=None):
        self.owner, self.project, self.client = owner, project, client
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.key = _key(owner, project)

    def read(self):
        row = _client(self.client).query('record.get', {'namespace': NAMESPACE, 'key': self.key})
        if row is None:
            return {'version': 1, 'owner': self.owner, 'projectPath': self.project, 'wakes': {}}, 0
        value = row.get('value') if isinstance(row, Mapping) else None
        _validate_schema(value, 'pendingWakes')
        if value['owner'] != self.owner or value['projectPath'] != self.project:
            raise StorageError('database_integrity', 'Pending wakes escaped their owner/project')
        return value, int(row['version'])

    def entries(self) -> dict:
        return self.read()[0]['wakes']

    def _update(self, change):
        """Apply ``change(wakes)`` under the record CAS; returns its result.

        The bound is the dispatch contract's ``casAttempts``: only wake owners of
        this one project write this record, so a lost race is re-read and applied.
        """
        last = None
        for _ in range(LIMITS['casAttempts']):
            value, version = self.read()
            result, changed = change(value['wakes'])
            if not changed:
                return result
            _validate_schema(value, 'pendingWakes')
            try:
                _client(self.client, write=True).command('record.put', {
                    'namespace': NAMESPACE, 'key': self.key, 'value': value,
                    'expected_version': version}, 'project-dispatch-wake:' + uuid.uuid4().hex)
                return result
            except StorageError as error:
                if error.code != 'database_conflict' and not error.retryable:
                    raise
                last = error
        from .repository import DispatchContention
        raise DispatchContention('Concurrent pending wake updates; retry the same wake') from last

    def retain(self, entry: str, kind: str, error: BaseException, **fields):
        """Record the entry once; returns it when this call opened its episode."""
        proposed = {'kind': kind, **{name: value for name, value in fields.items() if value},
                    'since': self.clock(), 'error': f'{type(error).__name__}: {error}'[:256],
                    'signalId': None}

        def add(wakes):
            if entry in wakes:
                return None, False
            wakes[entry] = proposed
            return dict(proposed), True
        return self._update(add)

    def note_signal(self, entry: str, since: int, signal_id: str) -> None:
        def mark(wakes):
            current = wakes.get(entry)
            if current is None or current['since'] != since or current['signalId'] == signal_id:
                return None, False
            current['signalId'] = signal_id
            return None, True
        self._update(mark)

    def complete(self, entry: str):
        """Remove the entry; returns what it held, or None when nothing was pending."""
        def remove(wakes):
            if entry not in wakes:
                return None, False
            return wakes.pop(entry), True
        return self._update(remove)


# Process-local triggers. The record is the authority; these only decide
# whether a dispatch commit in this process has a pending wake to run after it.
# _PENDING: the entry names this process last knew pending, per project.
# _DUE: the entries an event made due since the project's last pass started
# (None: every entry), consumed by the next pass.
_PENDING: dict = {}
_RUNNING: set = set()
_DUE: dict = {}
_LOCK = threading.Lock()
_NOTHING = object()


def _known(key, names) -> None:
    """Record the entries a read of the project's record found (caller holds no lock)."""
    with _LOCK:
        if names:
            _PENDING[key] = frozenset(names)
        else:
            _PENDING.pop(key, None)


def _report(owner, project, entry, kind, error, fields, since):
    """One typed fault per episode; the identity names the entry and its ``since``."""
    from lib.router.watchdog import signals
    episode = 'pending:' + entry + ':' + str(since)
    if kind == 'source':
        return signals.report_coordinator_wake_failure(
            owner, project, error, conversation_id=fields.get('coordinatorId'),
            source_attempt_id=fields.get('sourceAttemptId'), wake=episode)
    if kind == 'project':
        return signals.report_coordinator_wake_failure(owner, project, error, wake=episode)
    return signals.report_recovery_failure(owner, project, error, episode=episode)


def retain(owner: int, project: str, entry: str, kind: str, error: BaseException, *,
           client=None, report=True, **fields) -> bool:
    """Keep a pass that did not complete until a later one does; True when it is durable.

    The first failure of an episode records its Watchdog fault, unless the
    caller reports every occurrence itself (``report=False``: the settlement
    drain's one report call site). Never raises: a retention that cannot be
    written is logged and reported, and the caller keeps its own failure path.
    """
    try:
        store = PendingWakes(owner, project, client=client)
        opened = store.retain(entry, kind, error, **fields)
    except Exception:
        logger.warning('[ProjectDispatch] pending wake NOT retained owner=%s project=%s entry=%s '
                       'after %s', owner, project, entry, type(error).__name__, exc_info=True)
        if report:
            _report(owner, project, entry, kind, error, fields,
                    'unretained:' + str(int(time.time() * 1000)))
        return False
    with _LOCK:
        _PENDING[(owner, project)] = _PENDING.get((owner, project), frozenset()) | {entry}
    if opened is not None:
        logger.warning('[ProjectDispatch] wake retained for a later pass owner=%s project=%s entry=%s: '
                       '%s', owner, project, entry, opened['error'])
        if not report:
            return True
        signal = _report(owner, project, entry, kind, error, fields, opened['since'])
        if isinstance(signal, Mapping) and signal.get('signalId'):
            try:
                store.note_signal(entry, opened['since'], signal['signalId'])
            except Exception:
                logger.warning('[ProjectDispatch] pending wake signal not linked owner=%s entry=%s',
                               owner, entry, exc_info=True)
    return True


def complete(owner: int, project: str, entry: str, *, client=None) -> None:
    """A pass for this entry completed: remove it and resolve its episode's signal."""
    try:
        done = PendingWakes(owner, project, client=client).complete(entry)
    except Exception:
        # The entry stays pending and a later pass completes it again.
        logger.warning('[ProjectDispatch] pending wake completion not recorded owner=%s entry=%s',
                       owner, entry, exc_info=True)
        return
    with _LOCK:
        known = _PENDING.get((owner, project), frozenset()) - {entry}
        if known:
            _PENDING[(owner, project)] = known
        else:
            _PENDING.pop((owner, project), None)
    if done is None:
        return
    logger.info('[ProjectDispatch] pending wake completed owner=%s project=%s entry=%s pending_since=%s',
                owner, project, entry, done['since'])
    if done.get('signalId'):
        from lib.router.watchdog.signals import FaultSignals
        try:
            FaultSignals(owner, client=client).resolve(
                [done['signalId']], f'Resolved by its producer: the retained wake {entry} completed on a '
                                    f'later pass ({int(time.time() * 1000) - done["since"]} ms after '
                                    f'its first failure).')
        except Exception:
            logger.warning('[ProjectDispatch] pending wake signal %s not resolved owner=%s',
                           done['signalId'], owner, exc_info=True)


def process(owner: int, project: str, *, client=None, repository=None, service=None, due=None) -> bool:
    """One pass over the project's pending entries; returns whether any remain.

    ``due`` names the entries to run (None: every entry). Every entry runs the
    same recovery pass that failed; that pass itself completes or keeps its
    entry, so an entry ends only when a pass for it completes. Exactly once is
    the wake's own command identity and CAS.
    """
    if client is None and repository is not None:
        client = repository.client
    store = PendingWakes(owner, project, client=client)
    entries = store.entries()
    _known((owner, project), entries)
    if due is not None:
        entries = {name: entry for name, entry in entries.items() if name in due}
    if not entries:
        return bool(_PENDING.get((owner, project)))
    from .continuation import recover_coordinator
    from .recovery import recover_project_dispatch, recover_recipient
    if repository is None:
        from .repository import ProjectDispatchRepository
        repository = ProjectDispatchRepository(owner, project)
    sources = [entry['sourceAttemptId'] for entry in entries.values() if entry['kind'] == 'source']
    if sources:
        recover_coordinator(repository, service=service, sources=sources)
    for name, entry in entries.items():
        if entry['kind'] == 'recipient':
            try:
                recover_recipient(owner, project, entry['recipientId'], repository=repository,
                                  service=service)
            except Exception:
                logger.warning('[ProjectDispatch] retained settlement recovery failed again owner=%s '
                               'entry=%s', owner, name, exc_info=True)
        elif entry['kind'] == 'dependents':
            try:
                recover_project_dispatch(owner, project, prerequisite_id=entry['assignmentId'],
                                         repository=repository, service=service, clock=repository.clock)
            except Exception:
                logger.warning('[ProjectDispatch] retained dependent wake failed again owner=%s entry=%s',
                               owner, name, exc_info=True)
            else:
                complete(owner, project, name, client=client)
        elif entry['kind'] == 'project':
            try:
                recover_coordinator(repository, service=service)
            except Exception:
                logger.warning('[ProjectDispatch] retained project wake failed again owner=%s project=%s',
                               owner, project, exc_info=True)
    remaining = store.entries()
    _known((owner, project), remaining)
    return bool(remaining)


def _run(key):
    owner, project = key
    while True:
        # A relevant commit that landed during the last pass may be what it lost
        # to; one more pass of those entries then runs after it. Without one,
        # the next relevant commit, the maintenance tick or startup is the next
        # event: a pass whose evidence nothing rewrote would fail the same way.
        with _LOCK:
            due = _DUE.pop(key, _NOTHING)
            if due is _NOTHING or key not in _PENDING:
                _DUE.pop(key, None)
                _RUNNING.discard(key)
                return
        try:
            process(owner, project, due=due)
        except Exception:
            logger.warning('[ProjectDispatch] pending wake pass failed owner=%s project=%s',
                           owner, project, exc_info=True)


def _spawn(target, key) -> None:
    threading.Thread(target=target, args=(key,), daemon=True, name='project-dispatch-pending-wakes').start()


def _start(key, due=None) -> None:
    """Make ``due`` (None: every entry) due and run a pass unless one is running."""
    with _LOCK:
        if key not in _PENDING:
            return
        prior = _DUE.get(key, _NOTHING)
        _DUE[key] = (None if due is None or prior is None
                     else frozenset(due) | (frozenset() if prior is _NOTHING else prior))
        if key in _RUNNING:
            return
        _RUNNING.add(key)
    _spawn(_run, key)


def notify_commit(owner: int, project: str, written=None) -> None:
    """A dispatch commit of this project landed in this process: run the pending wakes it concerns.

    ``written`` is ``written_entries`` of a document commit; None (a head or
    legacy record commit) concerns every entry. A lookup and a set intersection
    are the whole cost for a commit that concerns no pending entry.
    """
    key = (owner, project)
    known = _PENDING.get(key)
    if not known:
        return
    if written is None:
        _start(key)
    elif not known.isdisjoint(written):
        _start(key, known & written)


def process_all(*, client=None) -> int:
    """The task-maintenance tick and startup: start one pass per project with pending entries."""
    from lib.storage.generated_project_dispatch_contract import SPEC
    from lib.conversations.project_identity import normalize_project_path
    from lib.identity import require_user_id
    limit = SPEC['limits']['pageRows']
    cursor, started = '', 0
    while True:
        page = _client(client).query('record.list', {'namespace': NAMESPACE, 'prefix': '',
                                                      'after_key': cursor, 'limit': limit})
        if not isinstance(page, list) or len(page) > limit:
            raise StorageError('database_integrity', 'Invalid pending wake page')
        for item in page:
            key, value = item.get('key'), item.get('value')
            if not isinstance(key, str) or key <= cursor or not isinstance(value, Mapping):
                raise StorageError('database_integrity', 'Invalid pending wake cursor')
            cursor = key
            if not value.get('wakes'):
                continue
            owner = require_user_id(value.get('owner'), context='pending project dispatch wake')
            project = normalize_project_path(value.get('projectPath'))
            if _key(owner, project) != key:
                raise StorageError('database_integrity', 'Pending wake key differs from owner/project')
            _known((owner, project), value['wakes'])
            _start((owner, project))
            started += 1
        if len(page) < limit:
            return started


__all__ = ['NAMESPACE', 'PendingWakes', 'complete', 'dependents_entry', 'notify_commit', 'process',
           'process_all', 'recipient_entry', 'retain', 'source_entry', 'written_entries']
