"""Office facts: the owner's team facts as one snapshot plus ordered deltas.

Responsibility: derive the facts the office shows (docs/PROJECT_COORDINATION.md,
"Office team facts") from their existing owners, and deliver them as one
snapshot (``GET /api/v4/router/office``) plus ``office.fact`` push frames
(docs/contracts/EVENTS.md). Every fact is an exact diff of two consecutive
reads of its owner. The feed starts no work and grants no access; it writes
nothing of its own, and reading owner questions through their index
(``open_questions``) lets that index record the answers it finds.

A pass runs only after the conversation commit watch reports a commit of the
owner (``broker.add_scope_listener``; the watch sees the commits of every
process, the execution host's included), after a watch gap, or after an API-host
``project`` hint (a report marked read over HTTP commits no conversation), and
only for an owner whose browser holds an ``office`` push subscription or has
just read the snapshot. An owner nobody watches keeps its baseline (one per
owner) with no pass run for it; its next pass or snapshot catches up as one
watch gap, with the ``project`` hints it missed. Passes are single flight: one
thread runs them, and triggers that arrive meanwhile join that owner's next
pass. No timer, interval or poll exists.

Facts derived by this build:

- ``busy``: a conversation whose attempt runs now, placed by the attempt's
  accepted dispatch admission. A running attempt whose conversation committed is
  read again while no pass waits: its terminal commit precedes its job's
  completion, which commits nothing;
- ``report``: a lead's newest coordinator report (with ``unread``), a pending
  ``kind=report`` project message to a lead, and a Worker's completed
  assignment result until its lead reviews it;
- ``decision``: an owner question a lead asked (``ask_human``) until answered;
- ``document``: a Worker result its lead accepted;
- ``board``: the revision of a lead's task board;
- ``alarm``: an open Router Watchdog signal.

Entry points: snapshot(owner), stats(), stop(); OfficeFacts (injectable ports)
for tests.
Dependencies: conversation_sync.broker (scope listener), agent_core.push and
events (frames, subscriber check, publication listener), tasks_pkg.manager
(running attempts), turn_lifecycle (attempts and Turns), project_dispatch
(assignments and history), conversations (bindings, reports, mailboxes).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

CONTRACT = 'tofu.office-facts/v1'
CHANNEL = 'office'
TASK = '__office__'
#: Coverage names of every fact kind in the contract, in wire order.
SOURCES = ('busy', 'reports', 'decisions', 'documents', 'boards', 'alarms')
_ATTEMPT_TASK = 'conversation-attempt:'
_ADMISSION = '_projectDispatchAdmission'
_RESULT_PLACEMENT = ('conversationId', 'attemptId', 'projectPath', 'assignmentId', 'coordinatorId')
#: ``conversation.list``'s declared page maximum (lib/conversations/repository.py, storage contract).
CONVERSATION_LIST_MAXIMUM = 10_000


class OfficeFactsClosed(RuntimeError):
    """The feed stopped with its application; no snapshot is served."""


def _client():
    from lib.storage import get_storage_client
    return get_storage_client()


class StoragePorts:
    """Production reads and delivery; every read names its owner explicitly."""

    def running(self, owner: int) -> dict[str, str]:
        from lib.tasks_pkg.manager import running_conversation_tasks
        return running_conversation_tasks(user_id=owner)

    def placement(self, owner: int, conversation_id: str, task_id: str) -> dict:
        """Project, assignment and lead of a running attempt, from its accepted config."""
        if not task_id.startswith(_ATTEMPT_TASK):
            return {}
        attempt_id = task_id[len(_ATTEMPT_TASK):]
        from lib.turn_lifecycle import get_attempt
        attempt = get_attempt(attempt_id, user_id=owner, include_execution_config=True)
        placement = {'attemptId': attempt_id}
        if attempt.get('conversationId') != conversation_id:
            return placement
        config = attempt.get('config') or {}
        if isinstance(config.get('projectPath'), str) and config['projectPath']:
            placement['projectPath'] = config['projectPath']
        admission = config.get(_ADMISSION)
        if (isinstance(admission, dict) and admission.get('owner') == owner
                and admission.get('recipientId') == conversation_id):
            from lib.project_dispatch.repository import ProjectDispatchRepository
            row = ProjectDispatchRepository(owner, admission['projectPath']).read_point(
                admission['assignmentId'])
            placement.update(projectPath=admission['projectPath'],
                             assignmentId=admission['assignmentId'],
                             coordinatorId=row['request']['coordinatorId'])
        return placement

    def attempt_status(self, owner: int, attempt_id: str) -> str:
        from lib.turn_lifecycle import get_attempt
        return get_attempt(attempt_id, user_id=owner)['status']

    def changed_conversations(self, owner: int, since_ms: int | None) -> tuple[list[tuple[str, int]], bool]:
        """``([(conversationId, updatedAtMs)], complete)`` updated at or after ``since_ms``.

        ``since_ms`` None reads only the newest row (the starting watermark).
        ``complete`` is False when the listing filled the storage contract's page maximum.
        """
        from lib.conversations.repository import ConversationRepository
        limit = 1 if since_ms is None else CONVERSATION_LIST_MAXIMUM
        rows = ConversationRepository().list(
            user_id=owner, include_messages=False, settings_keys=[], limit=limit,
            **({} if since_ms is None else {'updated_at_gte': since_ms}))
        changed = [(row.metadata['id'], int(row.metadata['updated_at'])) for row in rows]
        return changed, since_ms is None or len(changed) < limit

    def leads(self, owner: int) -> list[dict]:
        """Every bound lead: ``{conversationId, projectPath, revision, watchdog}``, in key order."""
        from lib.conversations.project_coordination import NAMESPACE
        from lib.project_dispatch.history import PAGE_ROWS
        from lib.router.watchdog.binding import is_watchdog_location
        prefix, after, leads = f'{owner}:', '', []
        while True:
            rows = _client().query('record.list', {'namespace': NAMESPACE, 'prefix': prefix,
                                                   'after_key': after, 'limit': PAGE_ROWS})
            for row in rows:
                value = row['value']
                if value.get('user_id') == owner and value.get('conversationId'):
                    leads.append({'conversationId': value['conversationId'],
                                  'projectPath': value['projectPath'], 'revision': int(row['version']),
                                  'watchdog': is_watchdog_location(owner, value['projectPath'])})
            if len(rows) < PAGE_ROWS:
                return leads
            after = rows[-1]['key']

    def questions(self, owner: int, lead: dict) -> list[dict]:
        """The lead's open owner questions with their Turn ordinal, from the owner-question index.

        Answers are recorded on the index when they happen (docs/ROUTER.md R10), so
        an answered question is never read and an open non-blocking one needs no
        Turn read; an open blocking question's card is read once (``open_questions``).
        """
        from lib.router.proactive.questions import open_questions
        found = []
        for question in open_questions(owner, coordinator_id=lead['conversationId']):
            fact = {'turnId': question['turnId'], 'guidanceId': question['guidanceId'],
                    'blocking': bool(question.get('blocking'))}
            if type(question.get('ordinal')) is int:
                fact['ordinal'] = question['ordinal']
            found.append(fact)
        return found

    def board(self, owner: int, lead: dict) -> int | None:
        """The revision of the lead's task board, or None when it keeps none."""
        from lib.conversations.coordinator_progress import CoordinatorTaskProgress
        return CoordinatorTaskProgress(owner, lead['projectPath']).revision(lead)

    def alarms(self, owner: int) -> list[dict]:
        """Every open Watchdog signal: ``{signalId, conversationId}`` (subject, '' when none)."""
        from lib.router.watchdog.signals import FaultSignals
        signals, cursor, found = FaultSignals(owner), None, []
        while True:
            page = signals.open_page(cursor)
            found += [{'signalId': signal['signalId'], 'conversationId': signal['subject'].get('conversationId') or ''}
                      for signal in page['signals']]
            cursor = page['next_cursor']
            if not cursor:
                return found

    def report(self, owner: int, lead: dict) -> dict | None:
        """The lead's newest coordinator report pointer with ``unread``, or None."""
        from lib.conversations.coordinator_reports import CoordinatorReports
        return CoordinatorReports(owner, lead['projectPath']).latest(lead)

    def report_messages(self, owner: int, lead: dict, known_version: int | None):
        """``(version, pending kind=report messages)``; messages are None when unchanged."""
        from lib.conversations.project_messages import ProjectMailbox
        mailbox = ProjectMailbox(owner, lead['projectPath'], lead['conversationId'])
        head, version = mailbox.head()
        if version == known_version:
            return version, None
        return version, [message for _position, message in mailbox.pending(head, lambda _sender: True, set())
                         if message['kind'] == 'report']

    def result(self, owner: int, placement: dict) -> dict | None:
        """The unreviewed completed result of an attempt bound to its assignment, or None."""
        from lib.project_dispatch.repository import ProjectDispatchRepository
        row = ProjectDispatchRepository(owner, placement['projectPath']).read_point(placement['assignmentId'])
        if row['request']['recipientId'] != placement['conversationId']:
            return None
        return self._unreviewed(owner, row, placement['attemptId'])

    def review(self, owner: int, fact: dict) -> dict | None:
        """None while a result fact awaits its lead; else the review that ended the wait.

        A revoked or rebound assignment ends it as ``{'decision': 'withdrawn'}``.
        """
        from lib.project_dispatch.repository import ProjectDispatchRepository
        row = ProjectDispatchRepository(owner, fact['projectPath']).read_point(fact['assignmentId'])
        association = row['execution']
        if row['grant']['revoked'] or association is None or association['attemptId'] != fact['attemptId']:
            return {'decision': 'withdrawn'}
        if row['review'] is not None and row['review']['resultRevision'] >= fact['revision']:
            return {'decision': row['review']['decision'], 'resultRevision': row['review']['resultRevision']}
        return None

    def newest_results(self, owner: int, lead: dict, running: frozenset) -> tuple[list[dict], list[dict]]:
        """``(unreviewed completed results, accepted results)`` of the lead's Workers on its newest history page.

        One page read. A reviewed row is final (``_unreviewed``): its review decides it
        with no read. An unreviewed row whose attempt runs (``running``: the attempt ids
        of this pass's running set) has no result yet. Any other row is decided by
        ``_unreviewed``, the rule the busy-to-idle edge (``result``) uses too.
        """
        from lib.project_dispatch.repository import ProjectDispatchRepository
        unreviewed, accepted = [], []
        for row in ProjectDispatchRepository(owner, lead['projectPath']).history_page(None)['rows']:
            request, association, review = row['request'], row['execution'], row['review']
            if (row['grant']['revoked'] or request['coordinatorId'] != lead['conversationId']
                    or request.get('profile', {}).get('mode') == 'coordinator'):
                continue
            if review is not None:
                if review['decision'] == 'accept':
                    accepted.append({'conversationId': request['recipientId'], 'projectPath': request['projectPath'],
                                     'assignmentId': row['id'], 'coordinatorId': request['coordinatorId'],
                                     'revision': review['resultRevision']})
                continue
            if association is None or association['attemptId'] in running:
                continue
            result = self._unreviewed(owner, row, association['attemptId'])
            if result is not None:
                unreviewed.append(result)
        return unreviewed, accepted

    @staticmethod
    def _unreviewed(owner: int, row: dict, attempt_id: str) -> dict | None:
        """The row's completed result at ``attempt_id`` while it awaits its review, or None.

        A reviewed row is final: ``record_review`` is immutable and ``continue_execution``
        refuses a reviewed association, so it never yields another result. Otherwise one
        Turn metadata read decides: the Turn's own status while its current attempt is
        the association's, the association's attempt only once the Turn moved on.
        """
        from lib.turn_lifecycle import get_attempt, get_turn
        association, request = row['execution'], row['request']
        if (row['grant']['revoked'] or row['review'] is not None or association is None
                or association['attemptId'] != attempt_id):
            return None
        turn = get_turn(request['recipientId'], association['turnId'], user_id=owner, include_projection=False)
        status = (turn.get('status') if turn.get('currentAttemptId') == attempt_id
                  else get_attempt(attempt_id, user_id=owner).get('status'))
        if status != 'completed':
            return None
        return {'conversationId': request['recipientId'], 'attemptId': attempt_id,
                'projectPath': request['projectPath'], 'assignmentId': row['id'],
                'coordinatorId': request['coordinatorId'], 'turnId': association['turnId'],
                'ordinal': turn['ordinal'], 'revision': turn['projectionRevision']}

    def publish(self, owner: int, frame: dict) -> None:
        from lib.agent_core.push import push_event
        push_event(CHANNEL, TASK, frame, user_id=owner)

    def subscribed(self, owner: int) -> bool:
        from lib.agent_core.push import hub
        return hub.has_subscriber(CHANNEL, owner)


@dataclass
class _Dirty:
    """What changed for one owner since its last pass.

    ``gap``: the commit watch could not name its scopes (it lists only commits made while it
    waits), so the catalog's recency index names them (``_committed``).
    ``catch_up``: the first pass after the baseline was parked. A Worker whose attempt began
    and ended while it was parked left no busy fact to end, so this pass also reads each
    lead's newest history page, as the baseline does.
    """
    gap: bool = False
    conversations: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    catch_up: bool = False


@dataclass
class _Owner:
    owner: int
    stream_id: str
    seq: int = 0
    built: bool = False
    # A snapshot read keeps the owner through its next pass before a socket subscribes.
    held: bool = True
    facts: dict[str, dict] = field(default_factory=dict)
    groups: dict[str, frozenset] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=lambda: dict.fromkeys(SOURCES, 'unsupported'))
    # Running task -> its placement; an attempt's accepted config never changes.
    placements: dict[str, dict] = field(default_factory=dict)
    leads: list[dict] = field(default_factory=list)
    mailbox_versions: dict[str, int] = field(default_factory=dict)
    # Nobody watches (no office subscription, no snapshot read pending): no pass runs. The
    # baseline stays; the next pass catches it up as one watch gap (_take_catch_up_locked).
    parked: bool = False
    # ``project`` hints that arrived while parked: they name changes no conversation commit names.
    missed_projects: set[str] = field(default_factory=set)
    # Running jobs whose attempt already settled: its job completes without a commit.
    terminal_tasks: set[str] = field(default_factory=set)
    # Running attempts whose conversation committed since their attempt was last read:
    # a commit may be the attempt's terminal one. Checked while the feed has no pass to run.
    suspects: dict[str, str] = field(default_factory=dict)
    # The latest conversation updatedAt seen and the ids seen at it: names the commits of a gap.
    watermark: int | None = None
    watermark_ids: frozenset = frozenset()
    pass_lock: threading.Lock = field(default_factory=threading.Lock)


def _report_fact(lead: dict, pointer: dict) -> dict:
    key = f"report:{lead['conversationId']}:{pointer['turnId']}"
    return {'fact': 'report', 'key': key, 'conversationId': lead['conversationId'],
            'source': 'coordinator_report', 'projectPath': lead['projectPath'], 'turnId': pointer['turnId'],
            'ordinal': pointer['ordinal'], 'reportKind': pointer['kind'], 'unread': pointer['unread']}


def _message_fact(lead: dict, message: dict) -> dict:
    return {'fact': 'report', 'key': 'message:' + message['id'], 'conversationId': lead['conversationId'],
            'source': 'project_message', 'projectPath': lead['projectPath'], 'messageId': message['id'],
            'senderId': message['sender']}


def _result_fact(result: dict) -> dict:
    return {'fact': 'report', 'key': f"result:{result['assignmentId']}:{result['revision']}",
            'source': 'assignment_result', **result}


def _document_fact(result: dict) -> dict:
    fields = ('conversationId', 'projectPath', 'assignmentId', 'coordinatorId', 'revision', 'turnId')
    return {'fact': 'document', 'key': f"document:{result['assignmentId']}:{result['revision']}",
            'source': 'accepted_result', **{name: result[name] for name in fields if name in result}}


def _question_fact(lead: dict, question: dict) -> dict:
    return {'fact': 'decision', 'key': f"question:{lead['conversationId']}:{question['guidanceId']}",
            'conversationId': lead['conversationId'], 'source': 'owner_question',
            'projectPath': lead['projectPath'], **question}


def _board_fact(lead: dict, revision: int) -> dict:
    return {'fact': 'board', 'key': 'board:' + lead['projectPath'], 'conversationId': lead['conversationId'],
            'source': 'task_board', 'projectPath': lead['projectPath'], 'revision': revision}


class OfficeFacts:
    """Per-process owner baselines, one pass thread and the ordered frame publisher."""

    def __init__(self, ports=None):
        self._ports = ports if ports is not None else StoragePorts()
        self._lock = threading.Condition()
        self._owners: dict[int, _Owner] = {}
        self._dirty: dict[int, _Dirty] = {}
        # One process lifetime: a restarted server can never repeat a streamId.
        self._boot = uuid.uuid4().hex[:16]
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._passing = False
        self._closed = False
        self._stats = {'passes': 0, 'frames': 0, 'frameBytes': 0, 'passSeconds': 0.0,
                       'passCpuSeconds': 0.0}

    def snapshot(self, owner: int) -> dict:
        """The owner's facts with the streamId and seq that order the frames after them."""
        owner = require_user_id(owner, context='office facts')
        with self._lock:
            if self._closed:
                raise OfficeFactsClosed('Office facts are stopping')
            state = self._activate_locked(owner)
            state.held = True
        if not state.built or state.parked:
            with state.pass_lock:
                if self._current(state):
                    with self._lock:
                        catch_up = self._take_catch_up_locked(state)
                    if not state.built:
                        self._pass(state, _Dirty(gap=True))
                    elif catch_up is not None:
                        self._catch_up(state, catch_up)
        with self._lock:
            self._schedule_locked()  # the baseline's suspected attempts are read off this request
            return {'contract': CONTRACT, 'streamId': state.stream_id, 'seq': state.seq,
                    'facts': [dict(state.facts[key]) for key in sorted(state.facts)],
                    'coverage': dict(state.coverage)}

    def observe_scopes(self, scopes: list[dict] | None) -> None:
        """Broker scope listener: record which owners need a pass; never reads here."""
        with self._lock:
            if self._closed:
                return
            if scopes is None:
                for owner, state in self._owners.items():
                    if not state.parked:
                        self._dirty.setdefault(owner, _Dirty()).gap = True
            else:
                for scope in scopes:
                    owner = scope.get('userId')
                    state = self._owners.get(owner)
                    if state is None:
                        if not (type(owner) is int and owner > 0 and self._ports.subscribed(owner)):
                            continue
                        self._activate_locked(owner).held = False
                    elif state.parked and not self._ports.subscribed(owner):
                        continue  # its catch-up pass finds this commit through the recency index
                    self._dirty.setdefault(owner, _Dirty()).conversations.add(scope.get('conversationId'))
            self._schedule_locked()

    def observe_publication(self, channel: str, _task_id: str, payload: dict) -> None:
        """Push Hub publication listener: an API-host ``project`` hint (e.g. a report read)."""
        if channel != 'project':
            return
        try:
            owner = int(payload.get('_ownerUserId'))
        except (TypeError, ValueError):
            return
        project = payload.get('projectKey') or payload.get('projectPath')
        with self._lock:
            state = self._owners.get(owner)
            if self._closed or state is None or not isinstance(project, str):
                return
            if state.parked:
                state.missed_projects.add(project)
                return
            self._dirty.setdefault(owner, _Dirty()).projects.add(project)
            self._schedule_locked()

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no pass or attempt check runs or waits; False when ``timeout`` seconds pass first."""
        with self._lock:
            return self._lock.wait_for(
                lambda: self._closed or not (self._dirty or self._passing or self._suspected_locked()), timeout)

    def _suspected_locked(self) -> bool:
        return any(self._settling(state) for state in self._owners.values())

    @staticmethod
    def _settling(state: _Owner) -> bool:
        """Whether idle attempt checks run for this owner: it has suspects and someone watches it."""
        return bool(state.suspects) and not state.parked

    def stats(self) -> dict:
        """Cumulative pass and frame counters (passes, frames, encoded frame bytes, pass time), and the
        baselines held: ``owners`` (one per owner) of which ``parked`` run no pass, and their ``facts``."""
        with self._lock:
            return {**self._stats, 'owners': len(self._owners),
                    'parked': sum(state.parked for state in self._owners.values()),
                    'facts': sum(len(state.facts) for state in self._owners.values())}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._dirty.clear()
            self._lock.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _schedule_locked(self) -> None:
        if not (self._dirty or self._suspected_locked()):
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='office-facts', daemon=True)
            self._thread.start()
        self._lock.notify_all()

    def _activate_locked(self, owner: int) -> _Owner:
        state = self._owners.get(owner)
        if state is None:
            self._generation += 1
            state = _Owner(owner, f'{self._boot}:{self._generation}')
            self._owners[owner] = state
        return state

    def _park_locked(self, state: _Owner, dirty: _Dirty) -> None:
        """Keep the baseline of an owner nobody watches, and run no pass or attempt check for it until
        someone does. Its suspects stay: a suspect's settling commit can predate the kept watermark,
        so the catch-up pass would not suspect it again."""
        state.parked = True
        state.missed_projects |= dirty.projects

    def _take_catch_up_locked(self, state: _Owner) -> _Dirty | None:
        """Unpark: the watch gap that names everything the owner missed, or None when not parked."""
        if not state.parked:
            return None
        state.parked = False
        dirty = _Dirty(gap=True, projects=state.missed_projects, catch_up=True)
        state.missed_projects = set()
        return dirty

    def _catch_up(self, state: _Owner, dirty: _Dirty) -> None:
        """The catch-up pass; one that fails parks the baseline again, so the next read retries the gap."""
        try:
            self._pass(state, dirty)
        except BaseException:
            with self._lock:
                self._park_locked(state, dirty)
            raise

    def _current(self, state: _Owner) -> bool:
        with self._lock:
            return not self._closed and self._owners.get(state.owner) is state

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._dirty and not self._closed and not self._suspected_locked():
                    self._lock.wait()
                if self._closed:
                    return
                if self._dirty:
                    owner = next(iter(self._dirty))
                    dirty = self._dirty.pop(owner)
                    state = self._owners.get(owner)
                    if state is None:
                        continue
                    if not state.held and not self._ports.subscribed(owner):
                        self._park_locked(state, dirty)
                        continue
                    catch_up = self._take_catch_up_locked(state)
                    if catch_up is not None:
                        catch_up.conversations |= dirty.conversations
                        catch_up.projects |= dirty.projects
                        dirty = catch_up
                    state.held = False
                else:
                    # Nothing to pass: read the suspected attempts (_settle yields to the next trigger).
                    dirty, state = None, next(state for state in self._owners.values() if self._settling(state))
                self._passing = True
            try:
                with state.pass_lock:
                    if self._current(state):
                        if dirty is None:
                            self._settle(state)
                        else:
                            (self._catch_up if dirty.catch_up else self._pass)(state, dirty)
            except Exception:
                # A failed pass keeps the last baseline; the next trigger retries it.
                logger.warning('[OfficeFacts] pass failed for owner %s', owner, exc_info=True)
            finally:
                with self._lock:
                    self._passing = False
                    self._lock.notify_all()

    def _settle(self, state: _Owner) -> None:
        """Read suspected attempts until a trigger arrives; a settled one ends its busy fact now.

        The attempt's terminal commit precedes its job's completion, which commits nothing.
        Under load the next pass re-reads the running jobs anyway, so these reads run only
        while no pass waits, and stop as soon as one does.
        """
        settled = False
        while True:
            with self._lock:
                if self._dirty or self._closed or not state.suspects:
                    break
                conversation_id = next(iter(state.suspects))
                task_id = state.suspects.pop(conversation_id)
            ok, status = self._read(state.owner, 'attempt', self._ports.attempt_status,
                                    task_id[len(_ATTEMPT_TASK):])
            if ok and status not in ('pending', 'running'):
                state.terminal_tasks.add(task_id)
                settled = True
        if settled:
            self._pass(state, _Dirty())

    def _committed(self, state: _Owner, dirty: _Dirty) -> tuple[set[str], bool]:
        """The conversations that committed, and whether that set is every conversation.

        A gap names nothing, so the catalog's recency index names its commits: every
        conversation updated at or after the watermark (taken from the rows' own clock).
        """
        committed = set(dirty.conversations)
        if state.built and not dirty.gap:
            return committed, False
        ok, answer = self._read(state.owner, 'changed conversations', self._ports.changed_conversations,
                                state.watermark)
        if not ok:
            return committed, True
        rows, complete = answer
        if state.watermark is not None:
            committed |= {conversation_id for conversation_id, updated in rows
                          if not (updated == state.watermark and conversation_id in state.watermark_ids)}
        if rows:
            newest = max(updated for _conversation_id, updated in rows)
            at_newest = {conversation_id for conversation_id, updated in rows if updated == newest}
            state.watermark_ids = frozenset(at_newest | (state.watermark_ids if newest == state.watermark else set()))
            state.watermark = newest
        elif state.watermark is None:
            state.watermark = 0
        # The baseline suspects every running attempt once: it has seen none of their commits.
        return committed, not state.built or not complete

    def _pass(self, state: _Owner, dirty: _Dirty) -> None:
        started, cpu = time.perf_counter(), time.thread_time()
        groups: dict[str, dict[str, dict]] = {}
        coverage: dict[str, str] = {}
        committed, everything = self._committed(state, dirty)
        ended, running = self._busy_pass(state, committed, everything, groups, coverage)
        self._lead_pass(state, dirty, committed, everything, ended, running, groups, coverage)
        self._commit(state, groups, coverage)
        with self._lock:
            self._stats['passes'] += 1
            self._stats['passSeconds'] += time.perf_counter() - started
            self._stats['passCpuSeconds'] += time.thread_time() - cpu

    def _read(self, owner: int, what: str, read, *args):
        try:
            return True, read(owner, *args)
        except Exception:
            logger.warning('[OfficeFacts] %s unreadable for owner %s', what, owner, exc_info=True)
            return False, None

    def _busy_pass(self, state: _Owner, committed: set[str], everything: bool, groups: dict,
                   coverage: dict) -> tuple[list[dict], frozenset]:
        """Busy facts; returns the busy facts of attempts that ended since the last pass, and the
        attempt ids running now (empty when unreadable)."""
        ok, running = self._read(state.owner, 'running attempts', self._ports.running)
        coverage['busy'] = 'complete' if ok else 'unavailable'
        if not ok:
            return [], frozenset()
        state.terminal_tasks &= set(running.values())
        running = {conversation_id: task_id for conversation_id, task_id in running.items()
                   if task_id not in state.terminal_tasks}
        suspects = {conversation_id: task_id for conversation_id, task_id in state.suspects.items()
                    if running.get(conversation_id) == task_id}
        suspects.update((conversation_id, task_id) for conversation_id, task_id in running.items()
                        if task_id.startswith(_ATTEMPT_TASK) and (everything or conversation_id in committed))
        with self._lock:
            state.suspects = suspects
        previous = [state.facts[key] for key in sorted(state.groups.get('busy', ()))]
        groups['busy'] = busy = self._busy(state, running)
        return ([fact for fact in previous if busy.get(fact['key'], {}).get('attemptId') != fact.get('attemptId')],
                frozenset(task_id[len(_ATTEMPT_TASK):] for task_id in running.values()
                          if task_id.startswith(_ATTEMPT_TASK)))

    def _busy(self, state: _Owner, running: dict[str, str]) -> dict[str, dict]:
        facts = {}
        for conversation_id, task_id in running.items():
            placement = state.placements.get(task_id)
            if placement is None:
                try:
                    placement = self._ports.placement(state.owner, conversation_id, task_id)
                except Exception:
                    # Unplaced until a later pass reads it; the fact itself stays exact.
                    logger.debug('[OfficeFacts] placement unreadable for %s', task_id, exc_info=True)
                else:
                    state.placements[task_id] = placement
            key = 'busy:' + conversation_id
            fact = {'fact': 'busy', 'key': key, 'conversationId': conversation_id, 'source': 'attempt'}
            if task_id.startswith(_ATTEMPT_TASK):
                # Another attempt of the same conversation is a changed fact, never silence.
                fact['attemptId'] = task_id[len(_ATTEMPT_TASK):]
            facts[key] = {**fact, **(placement or {})}
        live = set(running.values())
        for task_id in [task_id for task_id in state.placements if task_id not in live]:
            del state.placements[task_id]
        return facts

    def _lead_pass(self, state: _Owner, dirty: _Dirty, committed: set[str], everything: bool,
                   ended: list[dict], running: frozenset, groups: dict, coverage: dict) -> None:
        """Facts owned by the owner's leads: reports, owner questions, boards, documents, alarms."""
        baseline = not state.built
        owner = state.owner
        ok, leads = self._read(owner, 'leads', self._ports.leads)
        if not ok:
            for name in ('reports', 'decisions', 'documents', 'boards', 'alarms'):
                coverage[name] = 'unavailable'
            return
        state.leads = leads
        ids = {lead['conversationId'] for lead in leads}
        for group in state.groups:
            kind, _, lead_id = group.partition(':')
            if kind in ('report', 'message', 'question', 'board') and lead_id not in ids:
                groups[group] = {}
        touched = [lead for lead in leads if everything or lead['conversationId'] in committed
                   or lead['projectPath'] in dirty.projects]
        reports_ok = self._report_heads(state, touched, leads, baseline, groups)
        decisions_ok, boards_ok = self._questions_and_boards(state, touched, groups)
        results, results_ok, documents = self._results(state, leads, touched, baseline, dirty.catch_up, ended,
                                                       running)
        groups['results'] = results
        groups['documents'] = documents
        coverage['reports'] = 'newest_page' if reports_ok and results_ok else 'unavailable'
        coverage['documents'] = 'newest_page' if results_ok else 'unavailable'
        coverage['decisions'] = 'complete' if decisions_ok else 'unavailable'
        coverage['boards'] = 'complete' if boards_ok else 'unavailable'
        watchdog = next((lead for lead in leads if lead.get('watchdog')), None)
        if everything or (watchdog and watchdog['conversationId'] in committed):
            ok, alarms = self._read(owner, 'Watchdog signals', self._ports.alarms)
            coverage['alarms'] = 'complete' if ok else 'unavailable'
            if ok:
                fallback = watchdog['conversationId'] if watchdog else ''
                facts = [{'fact': 'alarm', 'key': 'alarm:' + alarm['signalId'],
                          'conversationId': alarm['conversationId'] or fallback, 'source': 'watchdog_signal',
                          'signalId': alarm['signalId']} for alarm in alarms
                         if alarm['conversationId'] or fallback]
                groups['alarms'] = {fact['key']: fact for fact in facts}

    def _questions_and_boards(self, state: _Owner, touched: list[dict], groups: dict) -> tuple[bool, bool]:
        decisions_ok = boards_ok = True
        for lead in touched:
            lead_id = lead['conversationId']
            ok, questions = self._read(state.owner, 'owner questions', self._ports.questions, lead)
            decisions_ok &= ok
            if ok:
                facts = [_question_fact(lead, question) for question in questions]
                groups['question:' + lead_id] = {fact['key']: fact for fact in facts}
            ok, revision = self._read(state.owner, 'task board', self._ports.board, lead)
            boards_ok &= ok
            if ok:
                fact = _board_fact(lead, revision) if revision is not None else None
                groups['board:' + lead_id] = {fact['key']: fact} if fact else {}
        return decisions_ok, boards_ok

    def _report_heads(self, state: _Owner, touched: list[dict], leads: list[dict], baseline: bool,
                      groups: dict) -> bool:
        owner, complete = state.owner, True
        for lead in touched:
            ok, pointer = self._read(owner, 'report head', self._ports.report, lead)
            complete &= ok
            if ok:
                fact = _report_fact(lead, pointer) if pointer else None
                groups['report:' + lead['conversationId']] = {fact['key']: fact} if fact else {}
        for lead in leads:
            ok, answer = self._read(owner, 'mailbox', self._ports.report_messages, lead,
                                    None if baseline else state.mailbox_versions.get(lead['conversationId']))
            complete &= ok
            if ok and answer[1] is not None:
                state.mailbox_versions[lead['conversationId']] = answer[0]
                facts = [_message_fact(lead, message) for message in answer[1]]
                groups['message:' + lead['conversationId']] = {fact['key']: fact for fact in facts}
        return complete

    def _results(self, state: _Owner, leads: list[dict], touched: list[dict], baseline: bool, catch_up: bool,
                 ended: list[dict], running: frozenset) -> tuple[dict[str, dict], bool, dict[str, dict]]:
        """Unreviewed results and accepted documents; documents are never withdrawn once set.

        The baseline and a catch-up pass read each lead's newest history page once. A catch-up
        pass only adds what the page shows: its kept results may lie beyond that page, and the
        reviews of its touched leads' results are read below as in any pass.
        """
        owner, ok_all = state.owner, True
        results = {key: state.facts[key] for key in state.groups.get('results', ())}
        documents = {key: state.facts[key] for key in state.groups.get('documents', ())}
        if baseline or catch_up:
            for lead in leads:
                ok, found = self._read(owner, 'newest results', self._ports.newest_results, lead, running)
                ok_all &= ok
                if not ok:
                    continue
                unreviewed, accepted = found
                if baseline:
                    for key in [key for key, fact in results.items() if fact['coordinatorId'] == lead['conversationId']]:
                        del results[key]
                results.update((fact['key'], fact) for fact in map(_result_fact, unreviewed))
                documents.update((fact['key'], fact) for fact in map(_document_fact, accepted))
        for fact in ended:
            if not fact.get('assignmentId') or not fact.get('coordinatorId'):
                continue
            placement = {name: fact[name] for name in _RESULT_PLACEMENT}
            ok, found = self._read(owner, 'ended attempt', self._ports.result, placement)
            ok_all &= ok
            if ok and found is not None:
                result = _result_fact(found)
                results[result['key']] = result
        lead_ids = {lead['conversationId'] for lead in touched}
        for key, fact in list(results.items()):
            if fact['coordinatorId'] in lead_ids and not baseline:
                ok, review = self._read(owner, 'review', self._ports.review, fact)
                ok_all &= ok
                if ok and review is not None:
                    del results[key]
                    if review['decision'] == 'accept':
                        document = _document_fact({**fact, 'revision': review['resultRevision']})
                        documents[document['key']] = document
        return results, ok_all, documents

    def _commit(self, state: _Owner, groups: dict[str, dict[str, dict]], coverage: dict[str, str]) -> None:
        from lib.agent_core.events import build_push_frame
        with self._lock:
            if self._closed or self._owners.get(state.owner) is not state:
                return
            changes = []
            for group, facts in groups.items():
                previous = state.groups.get(group, frozenset())
                changes += [('cleared', state.facts.pop(key)) for key in sorted(previous - facts.keys())]
                changes += [('set', facts[key]) for key in sorted(facts) if state.facts.get(key) != facts[key]]
                state.facts.update(facts)
                if facts:
                    state.groups[group] = frozenset(facts)
                else:
                    state.groups.pop(group, None)
            state.coverage.update(coverage)
            if not state.built:
                # The first pass is the baseline: readers receive it as the snapshot.
                state.built = True
                return
            for status, fact in changes:
                state.seq += 1
                frame = build_push_frame('office.fact', userId=state.owner, streamId=state.stream_id,
                                         seq=state.seq, state=status, **fact)
                self._stats['frames'] += 1
                self._stats['frameBytes'] += len(json.dumps(frame, ensure_ascii=False, separators=(',', ':')))
                try:
                    self._ports.publish(state.owner, frame)
                except Exception:
                    # A lost frame is a seq gap its reader repairs from the snapshot.
                    logger.warning('[OfficeFacts] frame publish failed for owner %s',
                                   state.owner, exc_info=True)


_feed: OfficeFacts | None = None
_feed_lock = threading.Lock()


def _instance() -> OfficeFacts:
    global _feed
    with _feed_lock:
        if _feed is None:
            from lib.agent_core.push import hub
            from lib.conversation_sync.broker import broker
            _feed = OfficeFacts()
            broker.add_scope_listener(_feed.observe_scopes)
            hub.add_listener(_feed.observe_publication)
        return _feed


def snapshot(owner: int) -> dict:
    """The office facts snapshot of ``owner`` (starts the process feed on first use)."""
    return _instance().snapshot(owner)


def stats() -> dict | None:
    """Counters of the process feed, or None before its first snapshot."""
    with _feed_lock:
        feed = _feed
    return feed.stats() if feed is not None else None


def stop() -> None:
    """Detach from the broker and Push Hub and join the pass thread; runs before storage shutdown."""
    global _feed
    with _feed_lock:
        feed, _feed = _feed, None
    if feed is not None:
        from lib.agent_core.push import hub
        from lib.conversation_sync.broker import broker
        broker.remove_scope_listener(feed.observe_scopes)
        hub.remove_listener(feed.observe_publication)
        feed.close()


__all__ = ['CHANNEL', 'CONTRACT', 'SOURCES', 'TASK', 'OfficeFacts', 'OfficeFactsClosed',
           'StoragePorts', 'snapshot', 'stats', 'stop']
