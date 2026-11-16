"""Create/delegate/wait adapters for independent ordinary worker conversations.

Authority is the current accepted human Coordinator request, frozen tool policy
and ProjectDispatchRepository grant. This module has no executor, task state,
transcript store or polling model loop. A Coordinator waits on its Workers with
``poll`` (``poll_workers``), which observes existing task notifications and
yields to input waiting for the Coordinator's next round by the rule every
``poll`` wait shares (``lib.tasks_pkg.wait_interruption``).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from copy import deepcopy
from threading import RLock

from lib.project_dispatch.acceptance import own_checks
from lib.project_dispatch.admission import require_isolated_worker_root, reread_on_contention
from lib.project_dispatch.repository import (
    CommandReused, ProjectDispatchError, ProjectDispatchRepository, SyntheticSource, _validate_schema,
    one_decision)
from lib.project_dispatch.delegation import (
    admit_in_memory, issued_worker_policy, origin_attempt, policy_for_task, source_for_task,
    validate_delegation_policy)
from lib.project_dispatch.service import ProjectDispatchService
from lib.log import get_logger

from . import coordinator_births as births

logger = get_logger(__name__)

_TERMINAL = frozenset({'completed', 'failed', 'interrupted', 'truncated', 'superseded', 'cancelled'})
# Receipts that settle a Worker whose publication was requested: the
# Coordinator acts on or reports the reason.
_HELD_DELIVERY = frozenset({'blocked', 'failed', 'preserved'})
# A terminal notification can precede the attempt's durable status, so a wait
# rechecks promptly for a bounded window after one. The slow recheck
# (wait_interruption.DURABLE_RECHECK_SECONDS) covers process-local
# notification loss and queued task admission.
_PROMPT_RECHECK_SECONDS = 0.5
_PROMPT_RECHECK_WINDOW_SECONDS = 10.0
# How a wait the shared interruption ended reports it: Stop as cancelled, this
# call's own cancellation (its execution deadline) as a timeout, and input
# waiting for the next round as pending_input.
_INTERRUPTED = {'aborted': 'cancelled', 'cancelled': 'timeout', 'pending_input': 'pending_input'}


# Worker phases a wait reports (docs/PROJECT_COORDINATION.md, "Waiting for
# Workers"). Execution and delivery are the two pending phases, reported apart;
# every other phase is settled. A Coordinator's own review closes a result
# (``reviewed``), so it is never news to that Coordinator.
_PENDING = frozenset({'running', 'delivering'})
_NOT_NEWS = _PENDING | {'reviewed', 'cancelled'}
# Per Coordinator attempt: the settlement each Worker was last reported at and
# the Workers seen pending, so each settlement is reported once and a Worker
# the wait does not name still ends it when it settles.
_REPORTS_KEY = '_coordinatorWorkerReports'


def _phase(row) -> str:
    """Where a Worker is: running, needs_review, reviewed, delivering, delivered, held or ended.

    A completed execution needs review until its Coordinator reviews it; any
    other end is final. Once an accepted result's publication was requested,
    the Worker is delivering until the delivery is canonical or held (blocked,
    failed or preserved), which the Coordinator acts on.
    """
    if row['status'] == 'cancelled':
        return 'cancelled'  # Revoked by its Coordinator or owner: their own act, never news.
    if row['status'] not in _TERMINAL:
        return 'running'
    if row['status'] != 'completed':
        return 'ended'
    if row['publication_requested']:
        if row['canonical_delivery']:
            return 'delivered'
        return 'held' if (row['delivery'] or {}).get('status') in _HELD_DELIVERY else 'delivering'
    return 'reviewed' if row.get('review') is not None else 'needs_review'


def _settlement(row) -> tuple:
    """The identity of one settlement: a new phase, attempt or delivery receipt is a new one.

    Not the result revision: a settled Turn's projection still changes (title,
    translation), which is no new settlement.
    """
    return (_phase(row), row['task_id'], row.get('attempt_id'), (row['delivery'] or {}).get('status'))


def _reports(task) -> dict:
    # dict.setdefault is atomic, so parallel polls of one batch share one record and lock.
    return task.setdefault(_REPORTS_KEY, {'reported': {}, 'pending': set(), 'seeded': False,
                                          'lock': RLock()})


def _interrupted_phase(repository, assignment_id, error) -> str:
    """How an interrupted Worker stands: ``needs_decision``, ``resuming`` or ``ended``.

    A restart cut off a call of ``unknown`` effect class: its Coordinator decides
    (docs/RUN_CONTINUITY.md, "A restart-cut call is classified by its declared
    effect"). A restart interruption project dispatch recovery continues in place
    is ``resuming``: the Coordinator has nothing to do.
    """
    if isinstance(error, dict) and error.get('unknownEffects'):
        return 'needs_decision'
    resume = repository.restart_resume(repository.read_point(assignment_id))
    return 'resuming' if resume is not None and resume['option'] is not None else 'ended'


def get_workers(task, scope, arguments, *, repository=None):
    """Page thin facts from this original request; no transcript or execution side effect.

    A Turn whose input is nonhuman (a project report relay, a Watchdog notice)
    has no request of its own: it reads its Coordinator binding's project
    Workers, as ``scope='project'`` does, with no current request, and still
    authorizes no Worker. Every other source refusal (another project or
    binding, a stopped or foreign attempt) stays a refusal.
    """
    _validate_schema(arguments, 'getProjectWorkers')
    from lib.project_dispatch.continuation import original_delegation, worker_summaries
    repository = repository or ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    try:
        principal, source, _ = source_for_task(repository, task)
    except SyntheticSource:
        from lib.identity import PrincipalContext
        principal, source = PrincipalContext.from_payload(task.get('_principalContext') or {}), None
    origin = value = None
    if source is not None:
        origin = original_delegation(task, {}, repository=repository)
        budget_id = origin.get('policy', {}).get('requestBudgetId') or getattr(task.get('_coordinatorRequestBudget'), 'budget_id', None)
        value, _ = repository._load(repository._selection(sources=[origin['attemptId']],
            budgets=[budget_id] if budget_id else []))
    project_history = source is None or arguments.get('scope', 'request') == 'project'
    page = repository.history_page(arguments.get('after_id')) if project_history else None
    candidates = page['rows'] if page else value['assignments']
    continuations = dict(zip((row['id'] for row in candidates),
                             page['rowOlderCursors'] if page else (row['id'] for row in candidates)))
    rows = [row for row in candidates
            if row['request']['coordinatorId'] == scope['conversation_id']
            and row['grant'].get('bindingRevision') == scope['binding_revision']
            and row['grant']['tenant'] == principal.tenant_id
            and (project_history or row['request'].get('delegation', {}).get('attemptId') == origin['attemptId'])
            and row['request']['profile']['mode'] != 'coordinator']
    cursor = arguments.get('after_id')
    if cursor is not None and not project_history:
        indices = [i for i, row in enumerate(rows) if row['id'] == cursor]
        if not indices:
            raise ValueError('Worker cursor is not in this request')
        rows = rows[indices[0] + 1:]
    limit = len(rows) if project_history else arguments.get('limit', 16)
    result = {'source_attempt_id': origin['attemptId'] if origin else None,
              'workers': worker_summaries(repository, rows[:limit]),
              'scope': 'project' if project_history else 'request',
              'next_cursor': page['olderCursor'] if page else rows[limit - 1]['id'] if len(rows) > limit else None}
    for item, row in zip(result['workers'], rows):
        item['source_turn_id'] = row['request']['authorizationSource']['turnId']
        item['current_request'] = row['request']['authorizationSource'] == source
    if source is None:
        return _readable_page(task, result, 'workers', lambda shown: continuations[rows[shown - 1]['id']])
    from lib.project_dispatch.request_budget import request_view, source_policy
    budget_id = origin['policy'].get('requestBudgetId')
    if not budget_id:
        source = source_policy(repository, task)
        budget_id = source['id'] if source is not None else None
    if budget_id:
        result['request_usage'] = request_view(repository, value, budget_id)
    starting = births.rows(task, exclude={row['id'] for row in candidates})
    if starting and not project_history:
        # Delegations whose Worker storage does not show yet: starting, or refused before birth.
        result['starting_workers'] = starting
    return _readable_page(task, result, 'workers', lambda shown: continuations[rows[shown - 1]['id']])


def _readable_page(task, result, key, cursor_after):
    """Keep the leading rows the model reads inline; the cursor resumes after the last one.

    A page that spills over the single tool-result budget reaches the model as
    an artifact pointer, so a shorter page plus an exact cursor costs less.
    """
    from lib.tasks_pkg.compaction.api import fits_tool_result_budget
    rows = result[key]
    shown = len(rows)
    while shown > 1 and not fits_tool_result_budget(
            json.dumps({**result, key: rows[:shown]}, ensure_ascii=False), model=task.get('model', '')):
        shown -= 1
    if shown == len(rows):
        return result
    return {**result, key: rows[:shown], 'next_cursor': cursor_after(shown)}


class DelegationCancelled(PermissionError):
    """A replay of a delegation whose Worker was cancelled; a replay never revives a revoked grant."""
    code = 'delegation_cancelled'
    execution_started = False
    recovery = ('This idempotency_key names a cancelled Worker (cancel_project_work, or the response that '
                'started it was discarded before its dispatch). To delegate this work now, call '
                'delegate_project_work with a new idempotency_key.')


def _delegation_key(origin_attempt_id, idempotency_key):
    return hashlib.sha256(json.dumps([origin_attempt_id, idempotency_key],
                                     separators=(',', ':')).encode()).hexdigest()


def delegate(task, scope, arguments, *, repository=None, service=None, create=None, early=False):
    """Admit a delegation from memory and return its Worker's identity at once.

    Owner decision 2026-11-11: delegating is as immediate as a human sending a
    message. The assignment and conversation IDs are deterministic, so the
    birth (``_born``) runs on its own thread (``coordinator_births``); its
    refusal or its Worker reaches the Coordinator through
    poll, get_project_workers or a replay of this call.
    Refusals that need no storage still return here.

    ``early``: the call's arguments finished streaming and its response has
    not ended; the attempt's ordinary dispatch of the same call adopts this
    birth, and ``settle_streamed_delegations`` withdraws it otherwise.
    """
    _validate_schema(arguments, 'delegateRequest')
    own_checks(arguments)  # Refused here: it needs no storage.
    if arguments.get('attachments'):
        # Admitted before any birth, so a refusal returns here and the Worker
        # request stores only retained attachment records.
        from .lead_attachments import admit
        arguments = {**arguments, 'attachments': admit(task, scope['user_id'], arguments['attachments'])}
    repository = repository or ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    admit_in_memory(repository, task)
    key = _delegation_key(origin_attempt(repository, task), arguments['idempotency_key'])
    command = 'coordinator-delegate:' + key
    from .coordinator_tools import refusal_payload, worker_identity
    birth = births.start(
        task, assignment_id=repository.command_identity(command),
        conversation_id=worker_identity(scope, key)[0], title=arguments['title'],
        digest=hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        # Unchanged arguments replay the same Worker, so contention reruns the
        # birth instead of leaving an uncertain outcome.
        run=lambda: reread_on_contention(task, lambda: _born(task, scope, arguments, repository, service,
                                                             create, command),
                                         label='coordinator delegation'),
        refusal=lambda error: refusal_payload('delegate_project_work', error),
        conflict=lambda: CommandReused('Command identity reused with changed request/granter',
                                       repository.command_identity(command)),
        early=early, withdraw=lambda birth: _withdraw_unadopted(task, repository, service, birth))
    if birth.done.is_set():
        if birth.failure is not None:
            raise births.BirthFailed(birth.failure)
        return birth.result
    return {'assignment_id': birth.assignment_id, 'conversation_id': birth.conversation_id,
            'task_id': None, 'status': 'starting', 'dispatch': 'starting',
            'next_step': 'The Worker is starting. Delegate other independent work now, then poll the '
                         'assignment IDs with wait_s for results; poll also reports a Worker that could not '
                         'start. Review the actual evidence before reporting delivery.'}


def _withdraw_unadopted(task, repository, service, birth):
    """Cancel a Worker born early from a call no dispatch of this attempt adopted."""
    service = service or ProjectDispatchService(repository)
    reread_on_contention(task, lambda: service.withdraw_unadopted_birth(
        birth.assignment_id,
        f"Withdrawn by Coordinator conversation {task.get('convId') or '?'} (task "
        f"{task.get('id') or '?'}): delegate_project_work started while its response streamed, "
        f"and no dispatched call of that attempt adopted assignment {birth.assignment_id}."),
        label='coordinator early delegation withdrawal', cancellable=False)


def settle_streamed_delegations(task):
    """Withdraw delegations started while their response streamed that no dispatch adopted.

    Runs after each tool batch and when the attempt's model loop ends, on every
    path (docs/PROJECT_COORDINATION.md, "A delegation starts when its arguments
    are complete").
    """
    withdrawn = births.withdraw_unadopted(task)
    if withdrawn:
        logger.warning('[Coordinator] task=%s withdrew %d delegation(s) started while a response '
                       'streamed that no dispatched call adopted: %s', task.get('id'), len(withdrawn),
                       ', '.join(birth.assignment_id for birth in withdrawn))


def _coordinator_folder(task, scope):
    """The Coordinator's folder, read once per task."""
    kept = task.get('_coordinatorFolder')
    if kept is None or kept[0] != scope['conversation_id']:
        from .coordinator_tools import coordinator_folder
        kept = task['_coordinatorFolder'] = (scope['conversation_id'], coordinator_folder(scope))
    return kept[1]


def _born(task, scope, arguments, repository, service, create, command):
    """One delegation's Worker birth: validate, prepare the root, create, grant, dispatch.

    It reads each authority fact once (one decision); the Worker's own
    admission validates its grant live again before its first model request.
    """
    with one_decision():
        request, key, actual, original = _delegation_request(task, scope, arguments, repository)
        if actual != command:
            raise RuntimeError('This delegation changed identity before its Worker was born; delegate again')
        if original is not None and original['grant']['revoked']:
            raise DelegationCancelled(f"Worker {original['id']} of this idempotency_key was cancelled")
        if original is None:
            # Refuse before any conversation or grant exists: every worker would fail binding.
            # Preparing a plain folder walks and commits its whole tree (6,931 s for
            # 1.4M files on 2026-11-09). Each observed advance proves this waiter
            # live, as a completed status poll does; Stop ends the preparation.
            def advanced():
                task['_dispatch_heartbeat'] = time.time()

            from lib.project_mod.tool_admission import Cancellation
            require_isolated_worker_root(repository.project, cancel=Cancellation(task),
                                         progress=advanced)
        if create is None:
            from functools import partial
            from .coordinator_tools import _create
            create = partial(_create, folder=_coordinator_folder(task, scope))
        worker = create(scope, {'title': arguments['title'], 'idempotency_key': key})
        request['recipientId'] = worker['conversation_id']
        assignment = repository.authorize_from_coordinator(request, task=task, command_id=command, issued=True)
        service = service or ProjectDispatchService(repository)
        outcome = service.dispatch_authorized(repository._admission(assignment))
    execution = outcome.get('execution') or {}
    return {'assignment_id': assignment['id'], 'conversation_id': request['recipientId'],
            'task_id': execution.get('taskId'), 'status': execution.get('status', outcome['dispatch']),
            'dispatch': outcome['dispatch'],
            'next_step': 'Delegate independent work, then poll the assignment IDs with wait_s for results. Review the actual evidence before reporting delivery.'}


def _delegation_request(task, scope, arguments, repository):
    """The Worker request this Coordinator attempt may delegate, refused before any effect."""
    _, source, attempt = source_for_task(repository, task, issued=True)
    policy = policy_for_task(repository, task, attempt)
    from lib.project_dispatch.continuation import original_delegation, ensure_open
    origin = original_delegation(task, policy, repository=repository)
    budget_id = policy.get('requestBudgetId')
    # The request's first row stands for its cohort and brings the source
    # settlement and budget, as in the grant CAS (_authorize_request).
    current, _ = repository._load(
        repository._selection(budgets=[budget_id], first_of_budgets=[budget_id], cohort=False)
        if budget_id else repository._selection(sources=[origin['attemptId']]))
    ensure_open(current, origin['attemptId'])
    issued = issued_worker_policy(repository, task)
    key = _delegation_key(origin['attemptId'], arguments['idempotency_key'])
    command = 'coordinator-delegate:' + key
    original = repository.find_command(command)
    # Stable identities cover both conversation creation and grant admission.
    # Nothing is relaunched just because the tool result acknowledgement was lost.
    request = {'projectPath': repository.project, 'coordinatorId': scope['conversation_id'],
        'recipientId': original['request']['recipientId'] if original else 'pending-worker',
        'authorizationSource': source, 'brief': arguments['brief'], 'model': issued.model,
        'budget': {},
        'profile': {'mode': 'project', 'tools': list(issued.tools), 'readPaths': ['.'], 'writePaths': ['.']},
        'delegation': origin}
    if arguments.get('dependencies'):
        request['dependencies'] = deepcopy(arguments['dependencies'])
    if arguments.get('rework_of'):
        request['reworkOf'] = deepcopy(arguments['rework_of'])
    if arguments.get('attachments'):
        request['attachments'] = deepcopy(arguments['attachments'])
    checks = own_checks(arguments)
    if not checks and arguments.get('rework_of'):
        # A repair is judged by the checks of the work it repairs unless it names its own.
        checks = repository.read_point(arguments['rework_of']['assignmentId'])['request'].get('acceptance')
    if checks:
        request['acceptance'] = list(checks)
    if policy.get('requestBudgetId'):
        request['delegationInputHash'] = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    _validate_schema(request, 'request')
    if original is None:
        validate_delegation_policy(repository, current, request)
    from lib.project_dispatch.delegation import validate_source
    validate_source(repository, request)
    return request, key, command, original


_USER_ACTION_FIELDS = ('scope', 'tool', 'errorCode', 'requiredAction', 'modelAction', 'message')


def _settled_failure(service, conversation_id, owner, turn_id):
    """Bounded cause from the settled worker Turn; the dispatch view carries none."""
    from lib.conversations.restart_resume import unknown_effects_to_review
    turn = service.turn_get(conversation_id, turn_id, user_id=owner) or {}
    error = (turn.get('settlement') or {}).get('error') or (turn.get('projection') or {}).get('error')
    if not isinstance(error, dict):
        return None
    failure = {key: str(error[key])[:1000] for key in ('kind', 'detail', 'hint') if error.get(key)}
    # A restart hold's hint asks for a check of the cut-off calls; only these name them.
    effects = unknown_effects_to_review(turn)
    if effects:
        failure['unknownEffects'] = effects
    # The user-action guard's stop names what only the owner can do.
    user_action = error.get('userAction')
    if isinstance(user_action, dict):
        failure['userAction'] = {
            key: user_action[key][:1000] for key in _USER_ACTION_FIELDS
            if isinstance(user_action.get(key), str)}
    return failure or None


def poll_workers(task, scope, arguments, *, runtime_context=None):
    """A Coordinator's ``poll``: its Workers by assignment ID, or without IDs the running ones."""
    ids = list(arguments.get('ids') or ())
    mode = arguments.get('mode') or 'any'
    if mode not in ('any', 'all'):
        raise ValueError("mode must be 'any' or 'all'")
    if not ids:
        return _running_workers(task, scope)
    # The Worker wait request bounds and shapes the IDs one wait observes.
    _validate_schema({'assignment_ids': ids}, 'awaitWorkersRequest')
    return wait_for_workers(task, scope, ids, wait_s=float(arguments.get('wait_s') or 0), mode=mode,
                            runtime_context=runtime_context)


def await_workers(task, scope, arguments, *, repository=None, service=None, runtime_context=None):
    """The retired await_project_workers: the same wait as a Coordinator's ``poll``.

    Calls recorded before poll absorbed it still dispatch. ``wait_for`` is
    accepted and selects nothing: every Worker settles when it needs its
    Coordinator or is final (``_settled``).
    """
    _validate_schema(arguments, 'awaitWorkersRequest')
    return wait_for_workers(task, scope, arguments['assignment_ids'],
                            wait_s=arguments.get('timeout_seconds', 1800), repository=repository,
                            service=service, runtime_context=runtime_context)


def _running_workers(task, scope):
    """This request's Workers that are running or starting: a ``poll`` without IDs."""
    page = reread_on_contention(task, lambda: get_workers(task, scope, {}),
                                label='coordinator worker page', effect_free=True)
    running = ([row for row in page['workers'] if row['status'] not in _TERMINAL]
               + page.get('starting_workers', []))
    more = (' This page does not hold every Worker: page get_project_workers from next_cursor.'
            if page.get('next_cursor') else '')
    return {'workers': running, 'instruction': (
        'This request\'s Workers that are running or starting (or could not start). Poll their assignment '
        'IDs with wait_s to wait for results; get_project_workers lists every Worker, settled ones included.'
        if running else 'No Worker of this request is running or starting; get_project_workers lists '
        'every Worker, settled ones included.') + more}


def wait_for_workers(task, scope, ids, *, wait_s, mode='any', repository=None, service=None,
                     runtime_context=None):
    """Read (``wait_s`` 0) or wait on existing terminal notifications; no repeated LLM status calls.

    Settlement is news reported once per Coordinator attempt (``_phase``,
    ``_settlement``): ``workers`` holds only settlements not reported before,
    with excerpts and causes; ``pending`` names the Workers still running and
    the accepted ones still delivering; ``unchanged`` the named ones already
    reported. It returns at once with news that is already there (a Worker that
    settled while the Coordinator was busy), when any Worker settles, the
    request's unnamed pending Workers included (mode='all' waits for every named
    one), at the deadline (wait=timeout), on Stop, or when input waits for the
    Coordinator's next round (wait=pending_input) by the rule every poll shares
    (``lib.tasks_pkg.wait_interruption``). Polls read settlement evidence only.
    """
    if len(set(ids)) != len(ids):
        raise ValueError('Worker assignment IDs must be distinct')
    from lib.tasks_pkg.wait_interruption import WaitInterruption
    interruption = WaitInterruption(task, runtime_context=runtime_context)
    ended = ['']

    def interrupted():
        ended[0] = interruption()
        return bool(ended[0])
    # Workers this task's delegations are still starting exist in storage only
    # once born: wait for those births first, under the same Stop, input and
    # deadline as the wait itself.
    deadline = time.monotonic() + wait_s
    starting = births.wait(task, ids, deadline=deadline, interrupted=interrupted)
    if starting:
        outcome = _INTERRUPTED.get(ended[0], 'timeout')
        result = {'workers': [births.row(birth) for birth in starting], 'wait': outcome,
                  'instruction': 'These Workers are still starting; poll the same assignment IDs again. '
                                 'Starting is not failure.'}
        if outcome == 'pending_input':
            result['waiting'] = interruption.waiting()
        return result
    refused = [births.row(birth) for birth in births.failures(task, ids).values()]
    repository = repository or ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    recorded = {birth.assignment_id for birth in births.recorded(task, ids)}
    for identity in ids:
        if identity in recorded:
            continue
        try:
            reread_on_contention(task, lambda identity=identity: repository.read(identity),
                                 label='coordinator worker lookup', effect_free=True)
        except ProjectDispatchError as error:
            if 'not found' not in str(error):
                raise
            # A birth this task no longer records (a restart ended it before
            # its grant) left no Worker; replaying the delegation starts it.
            # Any other unknown ID is not one of this Coordinator's Workers.
            refused.append({'assignment_id': identity, 'status': 'failed_to_start', 'error': {
                'error': 'No Worker has this assignment ID: its delegation ended before the grant existed '
                         '(for example at a server restart), or it is not one of your assignment IDs.',
                'execution_started': False,
                'recovery': 'For a delegation of yours, call delegate_project_work again with the same '
                            'arguments; it starts the same Worker. get_project_workers lists your Workers.'}})
    live = [identity for identity in ids if identity not in {row['assignment_id'] for row in refused}]
    refusal = ('' if not refused else ' Delegations ' + ', '.join(row['assignment_id'] for row in refused)
               + ' could not start a Worker (status failed_to_start): act on each error and its recovery.')
    if not live:
        return {'workers': refused, 'wait': 'settled', 'instruction': refusal.strip()}
    result = _await_born(task, scope, live, deadline=deadline, mode=mode, interruption=interruption,
                         repository=repository, service=service, runtime_context=runtime_context)
    if refused:
        result['workers'] = result['workers'] + refused
        result['instruction'] = result.get('instruction', '') + refusal
    return result


def _runs_here(row, owner) -> bool:
    """Whether the Worker's task runs in this process, so its end fires here (``on_terminal``)."""
    if not row.get('task_id') or row['status'] in _TERMINAL:
        return False
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    worker = chat_task_runtime.get_owned(row['task_id'], user_id=owner)
    return worker is not None and worker.get('status') in ('pending', 'running') and not worker.get('aborted')


def _unread_row(identity):
    """A Worker a wait ended before reading: pending, never news."""
    return {'assignment_id': identity, 'task_id': None, 'attempt_id': None, 'status': 'unread',
            'review': None, 'delivery': None, 'canonical_delivery': False, 'publication_requested': False}


# Per Coordinator task: the last settlement row of each Worker whose task ran in
# this process when read, so a later poll reads none that still runs here.
_WAIT_ROWS_KEY = '_coordinatorWaitRows'


def _await_born(task, scope, ids, *, deadline, mode, interruption, repository, service, runtime_context):
    """Wait on born Workers, reading each again only when it can have changed.

    A Worker whose task runs in this process is read once its end fires here or
    it no longer runs here; any other is read at each recheck. Input or Stop
    that arrives during the wait ends a pass between two Worker reads.
    """
    repository = repository or ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    service = service or ProjectDispatchService(repository)
    owner = repository.owner
    # Input already waiting at the start still gets one full read of the Workers,
    # so settled ones return their evidence; later input ends a pass between reads.
    waiting_at_start = bool(interruption())
    def read_snapshots(identities):
        rows = []
        # One observation of the coordination binding serves every Worker view.
        with one_decision():
            for identity in identities:
                # The view checks the Coordinator, its binding epoch and the grant's.
                view = service.view_for_coordinator(identity, scope=scope)
                execution = view.get('execution') or {}
                result = execution.get('result') or {}
                content = result.get('content') or ''
                recipient = view['request']['recipientId']
                row = {'assignment_id': identity, 'conversation_id': recipient,
                       'task_id': execution.get('taskId'), 'turn_id': execution.get('turnId'),
                       'attempt_id': execution.get('attemptId'),
                       'result_revision': execution.get('resultRevision'), 'delivery': view.get('delivery'),
                       'canonical_delivery': view.get('canonicalDelivery', False),
                       'publication_requested': bool(view.get('publication')), 'review': view.get('review'),
                       'status': 'cancelled' if view['revoked'] else execution.get('status', 'queued'),
                       'result_excerpt': content[:4000] if isinstance(content, str) else '',
                       'result_truncated': isinstance(content, str) and len(content) > 4000}
                row['phase'] = _phase(row)
                if row['status'] in ('failed', 'interrupted', 'truncated') and row['turn_id']:
                    failure = _settled_failure(service, recipient, repository.owner, row['turn_id'])
                    if failure:
                        row['error'] = failure
                if row['status'] == 'interrupted' and row['turn_id']:
                    row['phase'] = _interrupted_phase(repository, identity, row.get('error'))
                rows.append(row)
        return rows
    def snapshots(identities):
        return reread_on_contention(task, lambda: read_snapshots(identities), label='coordinator worker results',
                                    effect_free=True)
    def read_settlements(identities):
        """Execution status and delivery receipt; no projection, excerpt or dependency reads."""
        rows = {}
        # One observation of the coordination binding serves every Worker of a pass.
        with one_decision():
            for identity in identities:
                if rows and not waiting_at_start and interruption():
                    break
                view = service.view_for_coordinator(identity, scope=scope, settlement_only=True)
                execution = view.get('execution') or {}
                rows[identity] = {
                    'assignment_id': identity, 'task_id': execution.get('taskId'),
                    'attempt_id': execution.get('attemptId'),
                    'status': 'cancelled' if view['revoked'] else execution.get('status', 'queued'),
                    'review': view.get('review'), 'delivery': view.get('delivery'),
                    'canonical_delivery': view.get('canonicalDelivery', False),
                    'publication_requested': bool(view.get('publication'))}
        # A completed observation proves this waiter is responsive even when
        # Workers have no new output. A blocked read or transport-only ticker
        # must not refresh liveness or exempt a genuinely wedged handler.
        task['_dispatch_heartbeat'] = time.time()
        return rows
    def poll(identities):
        if not identities:
            task['_dispatch_heartbeat'] = time.time()
            return {}
        return reread_on_contention(task, lambda: read_settlements(identities),
                                    label='coordinator worker settlements', effect_free=True)
    # A settlement this Coordinator attempt was not told about is news, whenever
    # it happened: one that landed while the Coordinator was busy returns at once
    # instead of waiting behind the Workers still running. A reported one, unchanged,
    # never ends a wait again, so polling it never loops a model.
    reports = _reports(task)
    named = set(ids)
    watched = list(ids) + [identity for identity in _unnamed_pending(task, scope, reports) if identity not in named]

    def news(rows):
        return [row for row in rows if row['status'] != 'unread' and _phase(row) not in _NOT_NEWS
                and reports['reported'].get(row['assignment_id']) != _settlement(row)]

    def done(rows):
        fresh = news(rows)
        if any(row['assignment_id'] not in named for row in fresh):
            return True  # A Worker this call did not name settled: never hold it back.
        if not any(_phase(row) in _PENDING for row in rows if row['assignment_id'] in named):
            # Every named Worker is settled: report its news, or (mode any) keep
            # waiting on the request's other pending Workers instead of
            # returning nothing at once in a model loop.
            return bool(fresh) or mode == 'all' or not any(_phase(row) in _PENDING for row in rows)
        return mode != 'all' and bool(fresh)
    known = task.setdefault(_WAIT_ROWS_KEY, {})
    latest = {identity: known[identity] for identity in watched
              if identity in known and _runs_here(known[identity], owner)}
    latest.update(poll([identity for identity in watched if identity not in latest]))

    def current():
        return [latest.get(identity) or _unread_row(identity) for identity in watched]

    def recheck(ended_here):
        # Durable attempt/delivery evidence alone declares settlement. A Worker
        # whose end fired is read until this wait ends: the notification can
        # precede its durable status.
        latest.update(poll([identity for identity in watched if identity not in latest
                            or latest[identity]['task_id'] in ended_here
                            or not _runs_here(latest[identity], owner)]))
    rows = current()
    outcome = 'settled'
    if not done(rows):
        from lib.agent_core.admission import on_terminal
        from lib.tasks_pkg import wait_interruption
        changed, fired, ended_here, subscriptions = threading.Event(), deque(), set(), {}

        def signal(task_id):
            fired.append(task_id)
            changed.set()
        if runtime_context is not None:
            runtime_context.register_cancel_callback(changed.set)
        prompt_until = 0.0
        try:
            while True:
                if done(rows):
                    outcome = 'progress'
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = 'timeout'
                    break
                reason = interruption()
                if reason:
                    outcome = _INTERRUPTED[reason]
                    break
                fresh = [row['task_id'] for row in rows if row['task_id'] and row['status'] not in _TERMINAL
                         and row['task_id'] not in subscriptions]
                for task_id in fresh:
                    subscriptions[task_id] = on_terminal(task_id, signal)
                # Close the subscribe-versus-settlement race for Workers whose end cannot
                # fire here; one that ended meanwhile no longer runs here, so it is read.
                racing = [row['assignment_id'] for row in rows
                          if row['task_id'] in fresh and not _runs_here(row, owner)]
                if racing:
                    latest.update(poll(racing))
                    rows = current()
                    continue
                if fresh:
                    continue
                # The wait is interruptible and allocates no thread or model request.
                # Durable attempt/delivery evidence alone declares settlement.
                now = time.monotonic()
                check_at = now + min(remaining, _PROMPT_RECHECK_SECONDS if now < prompt_until
                                     else wait_interruption.DURABLE_RECHECK_SECONDS)
                while (not interruption()
                       and not changed.wait(min(births.WAIT_SLICE_SECONDS, max(0, check_at - time.monotonic())))):
                    if time.monotonic() >= check_at:
                        break
                if changed.is_set():
                    changed.clear()
                    prompt_until = time.monotonic() + _PROMPT_RECHECK_WINDOW_SECONDS
                while fired:
                    ended_here.add(fired.popleft())
                recheck(ended_here)
                rows = current()
        finally:
            for unsubscribe in subscriptions.values():
                unsubscribe()
    for identity, row in latest.items():
        if _runs_here(row, owner):
            known[identity] = row
        else:
            known.pop(identity, None)
    # Parallel polls of one batch share this record: each settlement is claimed by
    # exactly one of them, under the record's lock, before its evidence is read.
    with reports['lock']:
        for row in rows:
            if _phase(row) in _PENDING:
                reports['pending'].add(row['assignment_id'])
            else:
                reports['pending'].discard(row['assignment_id'])
        fresh = news(rows)
        claimed = {} if outcome == 'cancelled' else {
            row['assignment_id']: reports['reported'].get(row['assignment_id']) for row in fresh}
        for row in fresh if claimed else ():
            reports['reported'][row['assignment_id']] = _settlement(row)
    try:
        # Excerpts and causes are read once, for the news only.
        settled = snapshots([row['assignment_id'] for row in fresh]) if fresh else []
    except BaseException:
        with reports['lock']:
            for identity, previous in claimed.items():
                # Unreported after all: the next poll reports it.
                if previous is None:
                    reports['reported'].pop(identity, None)
                else:
                    reports['reported'][identity] = previous
        raise
    if outcome == 'cancelled':
        # Stop: nothing counts as reported, so the next attempt reports it.
        from lib.turn_verdict import aborted_wait_text
        return {'workers': settled, 'wait': 'cancelled',
                'instruction': aborted_wait_text(task.get('_abort_reason'))}
    running = [row['assignment_id'] for row in rows if _phase(row) == 'running']
    delivering = [row['assignment_id'] for row in rows if _phase(row) == 'delivering']
    reported = {row['assignment_id'] for row in fresh}
    unchanged = [row['assignment_id'] for row in rows
                 if row['assignment_id'] in named and _phase(row) not in _PENDING
                 and row['assignment_id'] not in reported]
    result = {'workers': settled, 'pending': {'running': running, 'delivering': delivering}}
    if unchanged:
        result['unchanged'] = unchanged
    status = ('timeout' if outcome == 'timeout' and not fresh
              else 'pending_input' if outcome == 'pending_input' and (running or delivering)
              else 'progress' if running or delivering else 'settled')
    if status == 'pending_input':
        result['waiting'] = interruption.waiting()
    return {**result, 'wait': status, 'instruction': _wait_instruction(status, settled, running, delivering,
                                                                       unchanged, named)}


def _wait_instruction(status, settled, running, delivering, unchanged, named):
    """What the Coordinator does next with a wait's news and what is still pending."""
    parts = []
    if settled:
        unnamed = [row['assignment_id'] for row in settled if row['assignment_id'] not in named]
        parts.append('New since your last poll (each settlement is reported once): '
                     + ', '.join(row['assignment_id'] for row in settled) + '. Review the settled evidence and '
                     'arrange follow-up work now; read full evidence through the conversation/Turn links where '
                     'needed. Failed/cancelled work is not delivery.'
                     + (' Of these, ' + ', '.join(unnamed) + ' were not in this poll: they are your Workers too.'
                        if unnamed else ''))
        held = [row['assignment_id'] for row in settled if row['status'] == 'completed'
                and not row['canonical_delivery'] and (row['delivery'] or {}).get('status') in _HELD_DELIVERY]
        if held:
            parts.append('Delivery of ' + ', '.join(held) + ' is blocked, failed or preserved, not canonical: '
                         'act on its delivery.reason (review_project_work with the exact checkpoint_sha if '
                         'publication was never requested, rework for a conflict or failed gate) or report a '
                         'cause you cannot resolve.')
    if status == 'pending_input':
        parts.insert(0, 'New input is waiting for you and arrives right after this result: a human message or '
                        'steer, a project message, or a background completion. The waiting list names its kind, '
                        'source and preview. Answer or act on that input first; Workers keep running. Ending a '
                        'wait for input is not failure.')
    elif status == 'timeout':
        parts.append('Nothing new settled; a timeout is not failure.')
    elif not settled:
        parts.append('Nothing new: every Worker you named was already reported.')
    decide = [row['assignment_id'] for row in settled if row.get('phase') == 'needs_decision']
    if decide:
        parts.append('A restart cut off a call of unknown effect in ' + ', '.join(decide) + ' (error.unknownEffects; '
                     'phase needs_decision): check what each named call changed, then control_conversation '
                     'checkpoint_resume, stop or follow_up that Worker. Nothing resumes it until you decide.')
    resuming = [row['assignment_id'] for row in settled if row.get('phase') == 'resuming']
    if resuming:
        parts.append('Interrupted by a restart and resuming from its checkpoint by itself: ' + ', '.join(resuming)
                     + '. Do not resume it; its next settlement is reported here.')
    if running:
        parts.append('Still running: ' + ', '.join(running) + '.')
    if delivering:
        parts.append('Accepted and publishing without you, not yet canonical: ' + ', '.join(delivering)
                     + '. Its canonical or held delivery is reported here once.')
    if unchanged:
        parts.append('Already reported and unchanged, leave them out of later polls: ' + ', '.join(unchanged) + '.')
    if running or delivering:
        parts.append('Poll the pending IDs again with wait_s to wait for the next settlement; a settlement '
                     'of any Worker of yours ends that wait.')
    return ' '.join(parts)


def _unnamed_pending(task, scope, reports):
    """This request's Workers a wait watches unnamed: any one's settlement is news.

    The Workers this attempt saw pending or started, plus, once per attempt,
    those of the request running when it first waited.
    """
    with reports['lock']:
        # A parallel poll waits for the one seeding read instead of repeating it.
        if not reports['seeded']:
            try:
                page = reread_on_contention(task, lambda: get_workers(task, scope, {}),
                                            label='coordinator worker page', effect_free=True)
            except (PermissionError, ProjectDispatchError) as error:
                # An attempt that no longer owns the request (it settled) still polls
                # the IDs it names; it only loses the request-wide watch.
                logger.info('[Coordinator] task=%s poll watches only named Workers: %s', task.get('id'), error)
                page = {'workers': []}
            reports['pending'].update(row['assignment_id'] for row in page['workers']
                                      if row['status'] not in _TERMINAL)
            reports['seeded'] = True
        pending = set(reports['pending'])
    born = [birth.assignment_id for birth in births.recorded(task)
            if birth.done.is_set() and birth.failure is None and not birth.withdrawn]
    return list(dict.fromkeys(born + sorted(pending - set(born))))
