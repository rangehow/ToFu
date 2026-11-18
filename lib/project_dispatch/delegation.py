"""Human-request delegation through the existing ProjectDispatch grant owner.

The ordinary Coordinator tool assembly issues a worker capability ceiling. A
tool call may narrow it, never manufacture it from arguments. Durable source
Turn/Attempt checks and the repository CAS own authorization and frozen policy.
Requests have no spend or concurrency ceiling. Worker execution remains the
ordinary command/Turn/task pipeline. Workers run on the Coordinator
conversation's ``workerModel`` setting, else the owner's default model, never
implicitly on the Coordinator's own model (docs/ROUTER.md R5): resolve_worker_model
and admit_worker_model are that resolver and its route admission.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from lib.identity import PrincipalContext
from lib.agent_core.model_context_revision import model_context_revision
from .repository import CONTRACT, ProjectDispatchError, _validate_schema

from lib.log import get_logger

logger = get_logger(__name__)

# ``((repository key, request budget id), first row)`` a Coordinator task keeps.
_FIRST_DELEGATION_KEY = '_coordinatorFirstDelegation'


@dataclass(frozen=True)
class CoordinatorWorkerPolicy:
    """Issued Worker ceiling. ``model`` is the Workers' model, None until the first
    delegation of a request resolves it (resolve_worker_model reads storage, so
    per-round tool assembly never does); continuations of the Coordinator
    itself keep ``coordinator_model``."""
    tools: tuple[str, ...]
    model: str | None
    config: dict
    coordinator_tools: tuple[str, ...]
    coordinator_model: str
    # MCP servers (owner configuration names) the Workers are granted: their
    # tools are admitted whenever they connect, so a Coordinator assembled
    # before MCP auto-connect finished still grants the servers the owner enabled.
    mcp_servers: tuple[str, ...] = ()


class CoordinatorModelMissing(PermissionError):
    """This Coordinator request runs without a model, so it can issue no Worker.

    Continuations record the Coordinator's own model; a request that started
    without one is refused identically on every delegation, so the refusal
    carries its own effect and recovery instead of the generic retry advice.
    """
    execution_started = False
    recovery = ('No worker was created. Delegating again in this request fails the same way; '
                'report this blocker and its remedy to the human instead of retrying.')


def owner_default_model(owner: int) -> str:
    """The model an owner's new conversations default to."""
    from lib.identity import require_user_id
    require_user_id(owner, context='owner default model')
    # TODO(enterprise): the default model is server configuration
    # (model_defaults.default_model); a per-owner default belongs in that
    # owner's model-routing settings.
    import lib
    return str(getattr(lib, 'LLM_MODEL', '') or '')


def admit_worker_model(owner: int, model: str, *, tenant_id: str | None = None,
                       field: str = 'workerModel') -> str:
    """Admit an owner's model choice (``field`` names it) through the ordinary route resolver."""
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ValueError(f'{field} must contain 1–256 characters')
    from lib.conv_config import canonicalise_model_id
    from lib.model_routing import ModelRoutingError, ModelRoutingRepository, OwnerBoundary, \
        resolve_compatible_model
    model = canonicalise_model_id(model.strip())
    document = ModelRoutingRepository().get(OwnerBoundary.create(owner, tenant_id)).document
    try:
        resolve_compatible_model(document, model)
    except ModelRoutingError as error:
        raise ValueError(f'{field} {model!r} is not routable for this owner: {error}') from error
    return model


def resolve_worker_model(task) -> str:
    """The Workers' model for this Coordinator request, pinned on the task.

    The bound Coordinator conversation's ``workerModel`` wins; otherwise the
    owner's default model. Models never choose it: only the owner's setting
    (written through ProjectCoordinationRepository.set_worker_model) does.
    A Coordinator continuation re-resolves the owner's CURRENT choice at each
    tool assembly (the setting may have changed after a provider refusal);
    a resumed request's earlier delegation no longer re-pins the old value.
    """
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    from lib.tasks_pkg.manager import task_user_id
    scope = coordinator_scope(task)
    owner = scope['user_id'] if scope else task_user_id(task)
    model = ''
    if scope:
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        model = ProjectCoordinationRepository(owner).worker_model(scope['project_path'])
    model = model or owner_default_model(owner)
    if not model:
        raise PermissionError('No Worker model: configure a default model or this Coordinator’s '
                              'Worker model, then delegate again')
    task['_coordinatorWorkerModel'] = model
    return model


def _live_worker_model(task, fallback: str) -> str:
    """A continuation's Workers follow the owner's current setting (session_model).

    Without a Coordinator scope there is no owner setting to resolve, so the
    model this request was issued with stands (unchanged behavior for every
    non-Coordinator delegation).
    """
    try:
        return resolve_worker_model(task)
    except (PermissionError, ValueError):
        return fallback
    except AttributeError:
        # A repository port without the coordination binding reader (test doubles)
        # cannot resolve the live setting; it keeps the issued model.
        return fallback


def continuation_worker_model(task, controls: dict) -> str:
    """The Workers' model of a Coordinator continuation: live, never the frozen replay."""
    return _live_worker_model(task, controls.get('workerModel', controls['model']))


def request_delegation(repository, task):
    """Find the first issued policy for this exact, physically bound human input.

    The request's first row never changes once it exists, so the task keeps it
    for the rest of its attempt instead of reading it at every derivation.
    """
    from .request_budget import source_policy
    shared = source_policy(repository, task)
    if shared is None:
        return None, None
    identity = (repository.key, shared['id'])
    kept = task.get(_FIRST_DELEGATION_KEY)
    if kept is not None and kept[0] == identity:
        return shared, deepcopy(kept[1])
    prior = repository.first_request_assignment(shared['id'])
    if prior is not None:
        task[_FIRST_DELEGATION_KEY] = (identity, deepcopy(prior))
    return shared, prior


def issued_worker_policy(repository, task):
    """Resumed human attempts retain the first worker capability ceiling.

    The Workers' model is the owner's current choice: after a provider refusal
    the owner changes it once and the same request delegates on the new route.
    The Coordinator's own model stays the one its request was issued with.
    """
    _, prior = request_delegation(repository, task)
    if prior is None:
        issued = task['_coordinatorWorkerPolicy']
        return replace(issued, model=_live_worker_model(task, issued.model or ''))
    request = prior['request']; policy = request['delegation']['policy']
    controls = policy.get('continuation') or {}
    servers = tuple(controls.get('workerMcpServers', request['profile'].get('mcpServers', ())))
    # The Workers' model is the owner's current setting, not a replay of the
    # value this request's earlier delegation pinned (the owner may have changed
    # it after a provider refusal). Without a Coordinator scope there is no
    # setting to resolve and the issued model stands.
    model = _live_worker_model(task, request['model'])
    # The first Worker's request records the Workers' model; continuation
    # controls record the Coordinator's own model.
    return CoordinatorWorkerPolicy(tuple(controls.get('workerTools', request['profile']['tools'])),
        model, deepcopy(policy['config']), tuple(controls.get('tools', ())),
        controls.get('model', request['model']), servers)


def worker_profile(issued: CoordinatorWorkerPolicy) -> dict:
    """The ``project`` profile a Worker of this issued policy is granted."""
    profile = {'mode': 'project', 'tools': list(issued.tools), 'readPaths': ['.'], 'writePaths': ['.']}
    if issued.mcp_servers:
        profile['mcpServers'] = list(issued.mcp_servers)
    return profile


def worker_control(repository, task, row, *, session=False):
    """Whether this Coordinator Turn may act on the Worker ``row``.

    ``session``: the Coordinator's own controls (control_conversation,
    follow_up, review_project_work, cancel_project_work) are scoped by the
    Coordinator binding alone, for every Turn of the Coordinator, so an
    automatic continuation can resume, stop, review or cancel a Worker an
    earlier request of the same binding dispatched (docs/PROJECT_COORDINATION.md,
    "Worker control is scoped by the Coordinator binding"). Without it (the
    Router's delegated control of a nonhuman principal) a continuation acts only
    on its original request's Workers, unless an owner message, his own or a
    Global Router forward of it, was steered into this reply and reached its model.
    """
    principal, source, _ = source_for_task(repository, task, delegated=True)
    from .continuation import original_delegation, current_continuation
    origin = original_delegation(task, {}, repository=repository)
    request = row['request']; delegation = request.get('delegation') or {}
    if (request['coordinatorId'] != source['conversationId'] or request['profile']['mode'] != 'project'
            or row['grant']['owner'] != repository.owner or row['grant']['tenant'] != principal.tenant_id):
        raise PermissionError('Worker belongs to another Coordinator or owner')
    repository.validate_grant_binding(row)
    if not session and current_continuation(task, require_account=True) is not None and (
            delegation.get('attemptId') != origin['attemptId'] or delegation.get('taskId') != origin['taskId']
            or request['authorizationSource'] != source) and 'ownerDelegation' not in source and _steered_owner_source(
                repository, task, {'conversationId': source['conversationId'], 'turnId': task.get('_turnId')}) is None:
        raise PermissionError('Automatic continuation may control only its original request workers')
    return origin


def coordinator_control_record(task, scope, action, *, delegated=False, **target):
    """The ``coordinator_session_control`` audit fields of one Coordinator control call."""
    return {'user_id': scope.get('user_id'), 'project_path': scope.get('project_path'),
            'coordinator_conversation_id': scope.get('conversation_id'), 'coordinator_task_id': task.get('id'),
            'coordinator_turn_id': task.get('_turnId'),
            'principal_kind': (task.get('_principalContext') or {}).get('kind'),
            'delegated': delegated, 'action': action, **target}


def audited_coordinator_control(record, act, accepted=lambda result: {}):
    """Run ``act`` and write one ``coordinator_session_control`` record of its outcome.

    ``accepted`` names the extra fields an accepted result records.
    """
    from lib.log import audit_log
    try:
        result = act()
    except Exception as error:
        audit_log('coordinator_session_control', **record, outcome='refused',
                  reason=f'{type(error).__name__}: {error}')
        raise
    audit_log('coordinator_session_control', **record, outcome='accepted', **accepted(result))
    return result


def capture_worker_policy(task, config, catalog, model, *, worker_model=None, enabled_mcp_servers=()):
    """Called before the Coordinator's own planning-only tool projection.

    ``model`` is the Coordinator's own model. ``worker_model`` pins the Workers'
    model; omitted, the first delegation resolves it with resolve_worker_model.
    ``enabled_mcp_servers`` are the servers the owner enabled for this Turn's
    assembly; with the servers of the catalog's MCP tools they are the servers
    the Workers are granted, connected yet or not.
    """
    from .continuation import current_continuation
    continuation = current_continuation(task)
    if continuation is not None:
        policy = continuation['request']['delegation']['policy']
        controls = policy['continuation']
        # Grants issued before separate Worker models recorded one shared model.
        task['_coordinatorWorkerPolicy'] = CoordinatorWorkerPolicy(
            tuple(controls['workerTools']), continuation_worker_model(task, controls),
            deepcopy(policy['config']), tuple(controls['tools']), controls['model'],
            tuple(controls.get('workerMcpServers', ())))
        return
    denied = set(CONTRACT['workerDeniedTools'])
    # A Worker has the capabilities of an ordinary conversation of this human:
    # the gateway's program action is admitted like any other tool, and its
    # tool settings are the human's own, not the Coordinator role's.
    names = tuple(sorted({'search_tools', 'execute_tools', 'execute_program'} | {
        item['function']['name'] for item in catalog if item['function']['name'] not in denied}))
    keys = CONTRACT['$defs']['workerPolicy']['properties']['config']['properties']
    selected = {key: deepcopy(config[key]) for key in keys if config.get(key) is not None}
    from lib.tasks_pkg.coordinator_policy import submitted_tool_settings
    selected['tools'] = submitted_tool_settings(config)
    selected.update(schedulerEnabled=False, humanGuidanceEnabled=False)
    visible = {item['function']['name'] for item in catalog} | {'search_tools', 'execute_tools'}
    # The record keeps the shape every release validates; it never decides a catalog. A
    # continuation holds its request's catalog: scope.granted_tools derives it from the
    # role its grant is bound to (docs/ROUTER.md R15).
    controls = tuple(name for name in CONTRACT['continuationTools'] if name in visible)
    if worker_model is not None:
        task['_coordinatorWorkerModel'] = worker_model
    from lib.mcp.types import parse_namespaced_name
    servers = tuple(sorted(set(enabled_mcp_servers) | {
        parsed[0] for parsed in map(parse_namespaced_name, names) if parsed is not None}))
    task['_coordinatorWorkerPolicy'] = CoordinatorWorkerPolicy(
        names, worker_model, selected, controls, model, servers)


def _active_scope(repository, task, *, for_budget):
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    scope = coordinator_scope(task)
    if (not scope or scope['user_id'] != repository.owner or scope['project_path'] != repository.project
            or task.get('aborted') or task.get('abort_event') and task['abort_event'].is_set()
            or not for_budget and (task.get('config') or {}).get('planMode')):
        raise PermissionError('Delegation requires the current executing Coordinator request')
    return scope


def _refuse_synthetic(repository, task, principal):
    if principal.kind != 'user' or principal.require_owner() != repository.owner or task.get('_attended') is False:
        raise PermissionError('Synthetic input cannot authorize new workers')


def _refuse_read_only(repository, task):
    for path in (task.get('config') or {}).get('readOnlyPaths', []):
        if Path(repository.project).resolve().is_relative_to(Path(path).resolve()):
            raise PermissionError('Read-only project cannot authorize a writing worker')


def _issued_policy(task):
    policy = task.get('_coordinatorWorkerPolicy')
    if not isinstance(policy, CoordinatorWorkerPolicy) or not policy.tools:
        raise PermissionError('Coordinator worker capabilities have not been issued by tool assembly')
    if not policy.coordinator_model:
        raise CoordinatorModelMissing(
            'This Coordinator request has no model, so it cannot issue Workers: the human selects '
            'a model in this conversation (or configures a default model), then sends the request again')
    return policy


def admit_in_memory(repository, task):
    """The refusals of a delegation that need no storage, checked before it returns.

    Its birth repeats them with every stored fact (``source_for_task``,
    ``policy_for_task``). A continuation's principal and policy come from its
    grant, so only its activity is checked here.
    """
    _active_scope(repository, task, for_budget=False)
    from .admission import is_dispatch_task
    if is_dispatch_task(task):
        return
    _refuse_synthetic(repository, task, PrincipalContext.from_payload(task.get('_principalContext') or {}))
    _refuse_read_only(repository, task)
    _issued_policy(task)


_ORIGIN_KEY = '_coordinatorDelegationOrigin'


def origin_attempt(repository, task):
    """The attempt every delegation of this request is keyed by, resolved once per task.

    It is the original delegation's attempt (``original_delegation``): a
    continuation's grant names it, and a human request's account holds it as
    its first account (``SourceBudgetBinding.origin_attempt``); a request with
    no account delegates from its own attempt. The birth recomputes it from
    storage and refuses a mismatch.
    """
    identity = (repository.key, task.get('id'), task.get('_attemptId'))
    kept = task.get(_ORIGIN_KEY)
    if kept is not None and kept[0] == identity:
        return kept[1]
    from .admission import KEY, is_dispatch_task
    if is_dispatch_task(task):
        binding = (task.get('config') or {}).get(KEY) or {}
        attempt = repository.read(binding['assignmentId'])['request']['delegation']['attemptId']
    else:
        marker = task.get('_coordinatorRequestBudget')
        attempt = getattr(marker, 'origin_attempt', '') or task['_attemptId']
    task[_ORIGIN_KEY] = (identity, attempt)
    return attempt


def source_for_task(repository, task, *, for_budget=False, issued=False, delegated=False):
    """Resolve the current physical Coordinator, never a model-supplied Turn ID.

    ``issued``: a delegation this attempt issued while it ran completes on its
    own thread, so the attempt may have completed since; a Stop, failure or
    supersession still refuses it.

    ``delegated``: the caller acts on Workers (delegate, follow up, control,
    review, cancel, finish), so a lead's Turn without an owner message of its
    own acts under the owner's delegation (``lead_source``). Callers that need
    the owner's own words (forwarding, standing instructions, request accounts,
    Watchdog review changes) leave it False.
    """
    from .repository import one_decision
    with one_decision():
        return _source_for_task(repository, task, for_budget=for_budget, issued=issued, delegated=delegated)


def _source_for_task(repository, task, *, for_budget, issued, delegated=False):
    scope = _active_scope(repository, task, for_budget=for_budget)
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    from .continuation import current_continuation
    continuation = current_continuation(task, require_account=True)
    if continuation is not None:
        original = continuation['request']
        if repository.owner != continuation['grant']['owner'] or repository.project != original['projectPath']:
            raise PermissionError('Coordinator continuation belongs to another scope')
        return principal, deepcopy(original['authorizationSource']), repository.get_attempt(original['delegation']['attemptId'])
    _refuse_synthetic(repository, task, principal)
    repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
    if not for_budget:
        _refuse_read_only(repository, task)
    turn = repository.read_turn(scope['conversation_id'], task.get('_turnId'), projection=False)
    attempt = repository.get_attempt(task.get('_attemptId'))
    live = ('pending', 'running', 'completed') if issued else ('pending', 'running')
    if (not isinstance(turn, dict) or turn.get('actor') != 'assistant'
            or turn.get('conversationId') != scope['conversation_id']
            or turn.get('turnId') != task.get('_turnId')
            or not isinstance(attempt, dict) or attempt.get('status') not in live
            or attempt.get('attemptId') != task.get('_attemptId') or attempt.get('taskId') != task.get('id')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != turn['conversationId']):
        raise PermissionError('Delegation source is not the current owned Coordinator attempt')
    # The human-input fence (validate_human_source) checks this same read, projection included.
    human = repository.read_turn(scope['conversation_id'], turn.get('parentTurnId'))
    source = {'conversationId': scope['conversation_id'], 'turnId': human.get('turnId') if human else None,
              'projectionRevision': human.get('projectionRevision') if human else None}
    from .repository import SyntheticSource
    try:
        repository.validate_human_source(source)
    except SyntheticSource as refusal:
        source = _steered_owner_source(repository, task, turn)
        if source is None:
            source = _answered_owner_source(repository, task, turn, attempt)
        delegation_refusal = None
        from lib.lifecycle_approval import DelegationRefused
        if source is None and delegated:
            try:
                source = lead_source(repository, task, scope, turn, attempt, human=human)
            except DelegationRefused as error:
                delegation_refusal = error
        if source is None:
            raise _nonhuman_request(human, refusal, delegation_refusal) from refusal
    from .repository import stopped_after
    if stopped_after(repository.owner, scope['conversation_id'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('Original Coordinator request was stopped')
    return principal, source, attempt


def _steered_owner_source(repository, task, turn):
    """The first owner message steered into this reply and delivered to its model, as a source.

    A Turn whose own input is nonhuman (a notice, a relayed report, a channel
    event) has no request of its own; the owner's message that reached its model
    is one, as if the owner had sent it as the Turn's input. Delivery is this
    executor's confirmed model consumption or one an earlier attempt of the Turn
    recorded; the steer's committed author decides humanity, never its text.
    """
    from lib.tasks_pkg.committed_steers import live_turn_steer_records
    from lib.turn_initiation import STEER_BLOCK_PREFIX, require_human_turn, steer_author_turn
    local = [record for record in task.get('_userSteerInjects') or () if isinstance(record, Mapping)]
    stored = [record for record in live_turn_steer_records(task) if isinstance(record, Mapping)]
    states = {}
    for record in stored + local:
        states.setdefault(record.get('blockId'), set()).add(record.get('deliveryState'))
    for record in stored + local:
        steer_id, author = record.get('commandId'), record.get('author')
        if (not isinstance(steer_id, str) or record.get('blockId') != STEER_BLOCK_PREFIX + steer_id
                or (states[record['blockId']] & {'delivered', 'queued'}) != {'delivered'}
                or not isinstance(author, Mapping)):
            continue
        try:
            require_human_turn(steer_author_turn(author), user_id=repository.owner)
        except ValueError:
            continue
        return {'conversationId': turn['conversationId'], 'turnId': turn['turnId'],
                'projectionRevision': 1, 'steerId': steer_id}
    return None


# A lead's task records here each ask_human answer its model received (the handler's result).
OWNER_ANSWERS_KEY = '_ownerGuidanceAnswers'


def answered_question(projection, guidance_id):
    """The owner's durable answer receipt of ask_human question ``guidance_id`` in a reply projection."""
    for question in (projection or {}).get('toolRounds') or ():
        if (isinstance(question, Mapping) and question.get('guidanceId') == guidance_id
                and question.get('toolName') == 'ask_human' and question.get('guidanceAnswerCommandId')
                and isinstance(question.get('guidanceResponse'), str) and question['guidanceResponse'].strip()):
            return question
    return None


def _answered_owner_source(repository, task, turn, attempt):
    """The owner's answer to this reply's own question, delivered to its model, as a source.

    Audit 2026-11-18 #5 (A6): a lead whose Turn has no owner message of its own
    asked the owner, received the answer on its question card, and still could
    not act on it. The answer is the owner's own words for this reply: the card
    is answered only through the owner's answer command (an agent never answers a
    question, docs/ROUTER.md R9), and its durable receipt is immutable. Delivery
    is this executor's own ask_human result, or an answer_guidance resume, whose
    model replays every answered question of the Turn.
    """
    delivered = list(task.get(OWNER_ANSWERS_KEY) or ())
    resumed = isinstance(attempt, Mapping) and attempt.get('operation') == 'answer_guidance'
    if not delivered and not resumed:
        return None
    reply = repository.read_turn(turn['conversationId'], turn['turnId'])
    projection = (reply or {}).get('projection') if isinstance(reply, Mapping) else None
    candidates = delivered or [question.get('guidanceId') for question in (projection or {}).get('toolRounds') or ()
                               if isinstance(question, Mapping)]
    for guidance_id in candidates:
        if isinstance(guidance_id, str) and answered_question(projection, guidance_id) is not None:
            return {'conversationId': turn['conversationId'], 'turnId': turn['turnId'],
                    'projectionRevision': 1, 'guidanceId': guidance_id}
    return None


LEAD_AUTHORITY = 'lead_authority'
# The lead_authority record each thread's current lead act resolved its source from.
_LEAD_ACT = threading.local()


def lead_source(repository, task, scope, turn, attempt, *, human=None):
    """The owner's delegation as the source of a lead Turn without an owner message of its own.

    Owner decision 2026-11-18 (docs/ROUTER.md R9): 「我的权限要全部下放给负责人们」.
    Under the owner's full grant (``lifecycle_approval``, scope ``lead_authority``)
    a wake, relay or notice Turn of the Global Router or a Project Router acts on
    Workers on its own judgment. The gate re-checks the live grant on every call
    (revocation refuses at once) and records one audit action per lead Attempt
    (the idempotency key), before any effect. Without a grant it raises the gate's
    ``DelegationRefused`` (the caller's nonhuman refusal names its code;
    ``audit.log`` records it); it returns ``None`` for the Watchdog, which is not
    a lead. The act that used it settles the record with its outcome (``lead_act``).
    """
    from lib import lifecycle_approval as gate
    from lib.router.watchdog.binding import is_watchdog_scope
    from lib.router.owner_delegation import LEAD_DELEGATION_WORDS, router_requester
    if is_watchdog_scope(scope):
        return None
    requester = router_requester(task, scope)
    gate.check_delegation(repository.owner, requester, LEAD_AUTHORITY)
    target = {'kind': 'project_request', 'conversationId': turn['conversationId'], 'turnId': turn['turnId'],
              'attemptId': attempt['attemptId'], 'path': repository.project}
    record, _ = gate.begin_delegated_action(
        repository.owner, requester, scope=LEAD_AUTHORITY, key='lead-turn:' + attempt['attemptId'],
        reason='A lead Turn without an owner message of its own acts on Workers under the owner\u2019s '
               'delegation to every lead.',
        trigger={'kind': 'owner_delegation', 'turnId': turn['turnId'], 'text': LEAD_DELEGATION_WORDS,
                 **({'triggerTurnId': human['turnId']} if isinstance(human, Mapping)
                    and isinstance(human.get('turnId'), str) else {})},
        target=target, trigger_source=_input_source(human))
    if getattr(_LEAD_ACT, 'used', None) is not None:
        _LEAD_ACT.used = (repository.owner, record['id'])
    return {'conversationId': turn['conversationId'], 'turnId': turn['turnId'], 'projectionRevision': 1,
            'ownerDelegation': record['id']}


def _input_source(human):
    from lib.turn_initiation import resolve_turn_initiator
    return resolve_turn_initiator(human) if isinstance(human, Mapping) else None


@contextmanager
def lead_act(action):
    """Settle the ``lead_authority`` record a lead act used with that act's outcome.

    Audit 2026-11-18 (Global Router verification efb9d9dd): lead records stayed
    ``accepted`` forever while every other delegated action settles
    ``completed``/``failed``. A lead act is one acting tool call (or one Worker
    birth); it settles the record only when its source came from
    ``lead_source`` in this thread. The record is per lead Attempt, so it holds
    the latest act's outcome; the lead-audit stream keeps every act. The body
    calls ``failed(code, detail)`` when the act returns a refusal instead of raising.
    """
    previous = getattr(_LEAD_ACT, 'used', None)
    _LEAD_ACT.used = ()
    outcome = {}

    def failed(code, detail):
        outcome.update(code=code, detail=detail)

    try:
        yield failed
    except BaseException as error:
        outcome.setdefault('code', getattr(error, 'code', None) or type(error).__name__)
        outcome.setdefault('detail', str(error) or type(error).__name__)
        raise
    finally:
        used, _LEAD_ACT.used = _LEAD_ACT.used, previous
        if used:
            from lib import lifecycle_approval as gate
            try:
                gate.settle_delegated_action(
                    used[0], used[1], status='failed' if outcome else 'completed',
                    code=outcome.get('code'), detail=outcome.get('detail'), action=action)
            except Exception as error:
                # The act already happened; its audit settlement never changes its result.
                logger.warning('[LeadAuthority] settlement of %s after %s failed: %s', used[1], action, error)


def _nonhuman_request(human, refusal, delegation_refusal=None):
    from lib.turn_initiation import INITIATOR_HUMAN, INITIATOR_UNKNOWN, resolve_turn_initiator
    from .repository import NonhumanRequest
    initiator = resolve_turn_initiator(human)
    kind = ('nonhuman ' + initiator + ' input' if initiator not in (INITIATOR_HUMAN, INITIATOR_UNKNOWN)
            else 'not the owner\u2019s own message')
    code = getattr(delegation_refusal, 'code', None)
    if code is None or code == 'owner_delegation_not_granted':
        delegation = 'the owner\u2019s delegation to every lead ("Act for me") is not granted'
    else:
        # The grant exists but could not be used now: name the gate's own cause.
        delegation = (f'the owner\u2019s delegation to every lead ("Act for me") could not be used: '
                      f'{code}: {delegation_refusal}')
    return NonhumanRequest(
        f'This turn has no request of its own: its input is {kind} ({refusal}), no owner message '
        f'has reached it, and {delegation}. It can read Workers, report, ask and update the board, but '
        'it cannot delegate, continue, stop or cancel Workers.')


def policy_for_task(repository, task, attempt):
    from .continuation import current_continuation
    continuation = current_continuation(task, require_account=True)
    if continuation is not None:
        return deepcopy(continuation['request']['delegation']['policy'])
    policy = _issued_policy(task)
    shared, prior = request_delegation(repository, task)
    if prior is not None:
        return deepcopy(prior['request']['delegation']['policy'])
    result = {'config': deepcopy(policy.config)}
    if shared is not None:
        result['requestBudgetId'] = shared['id']
    if {'delegate_project_work', 'get_project_workers', 'finish_project_request', 'report_to_user'} <= set(policy.coordinator_tools):
        result['continuation'] = {'version': 1, 'model': policy.coordinator_model,
            'workerModel': policy.model or resolve_worker_model(task), 'workerTools': list(policy.tools),
            **({'workerMcpServers': list(policy.mcp_servers)} if policy.mcp_servers else {}),
            'tools': list(policy.coordinator_tools),
            'maxStalledTurns': CONTRACT['delegationDefaults']['maxStalledTurns']}
    _validate_schema(result, 'workerPolicy')
    return result


def validate_source(repository, request, *, value=None):
    """Live policy/birth validation shared by admission, tools and model sends.

    ``value`` is the caller's loaded dispatch snapshot. When it holds the request
    budget row, that row decides: its authority fields are immutable and its
    accounts append-only, so rereading it within the same check cannot differ.
    """
    delegation = request.get('delegation')
    if request.get('profile', {}).get('mode') not in ('project', 'coordinator'):
        if delegation is not None:
            raise ProjectDispatchError('Delegation policy requires the project worker profile')
        return
    _validate_schema(delegation, 'workerDelegation')
    if request.get('profile', {}).get('mode') == 'coordinator':
        _validate_schema(delegation['policy'].get('continuation'), 'coordinatorContinuationPolicy')
        _validate_schema(request.get('coordinatorWake'), 'coordinatorWake')
        controls = delegation['policy']['continuation']
        if (request['coordinatorId'] != request['recipientId'] or request['model'] != controls['model']
                or request['profile']['tools'] != controls['tools']):
            raise PermissionError('Coordinator continuation differs from its original issued policy')
    elif 'coordinatorWake' in request:
        raise PermissionError('Worker cannot carry a Coordinator continuation')
    source = repository.read_turn(request['coordinatorId'], delegation['turnId'], projection=False)
    attempt = repository.get_attempt(delegation['attemptId'])
    # A human input Turn is the delegating reply's parent; a steered owner
    # message is inside that reply itself.
    inside_reply = {'steerId', 'ownerDelegation', 'guidanceId'} & set(request['authorizationSource'])
    authorized = (source.get('turnId') if inside_reply
                  else source.get('parentTurnId')) if isinstance(source, dict) else None
    if (not isinstance(source, dict) or source.get('actor') != 'assistant'
            or source.get('conversationId') != request['coordinatorId']
            or source.get('turnId') != delegation['turnId']
            or authorized != request['authorizationSource']['turnId']
            or not isinstance(attempt, dict) or attempt.get('taskId') != delegation['taskId']
            or attempt.get('attemptId') != delegation['attemptId'] or attempt.get('turnId') != delegation['turnId']
            or attempt.get('conversationId') != request['coordinatorId']):
        raise PermissionError('Original Coordinator delegation source is unavailable')
    policy = delegation['policy']
    if policy.get('requestBudgetId'):
        from .request_budget import validate_policy
        budget = policy['requestBudgetId']
        if value is None or not any(row['id'] == budget for row in value.get('requestBudgets', [])):
            value = repository._load(repository._selection(budgets=[budget], cohort=False))[0]
        validate_policy(repository, value, request)
    from .repository import stopped_after
    if stopped_after(repository.owner, request['coordinatorId'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('Original Coordinator request was stopped')


# The brief leads the input: it is the assignment, so its first line names the
# Worker's work (conversation and Project Brain titles). The owner's request is
# not repeated: the source line links it for get_conversation
# (docs/PROJECT_COORDINATION.md "Continuation context is anchored on the task board").
WORKER_INPUT_SOURCE_HEAD = '\n\nDelegated for the owner\'s request in conversation '
WORKER_INPUT_SOURCE_TAIL = (': read it with get_conversation when its exact words or attachments matter. '
                            'This is delegated context, not a new human authorization.')
_WORKER_INPUT_SOURCE_RE = re.compile(
    re.escape(WORKER_INPUT_SOURCE_HEAD) + r'[^\n;]+; Turn [^\n]+?' + re.escape(WORKER_INPUT_SOURCE_TAIL))


def worker_input_brief(text: str) -> str | None:
    """The brief of a ``worker_input`` envelope, else ``None``."""
    match = None
    for match in _WORKER_INPUT_SOURCE_RE.finditer(text):
        pass
    return None if match is None else text[:match.start()]


def worker_input_source(repository, request):
    """The source line of a delegated input, after checking the source is still the accepted one."""
    source = request['authorizationSource']
    where = 'Turn ' + source['turnId']
    if 'steerId' in source:
        from lib.turn_initiation import steered_input_turn
        turn = repository.turn_get(source['conversationId'], source['turnId'], user_id=repository.owner)
        try:
            steered_input_turn(turn, source['steerId'])
        except ValueError as error:
            raise PermissionError('Original worker objective changed before dispatch') from error
        where += ' (message ' + source['steerId'] + ' the owner sent while that reply ran)'
    elif 'guidanceId' in source:
        turn = repository.turn_get(source['conversationId'], source['turnId'], user_id=repository.owner)
        if (not turn or turn.get('actor') != 'assistant' or turn.get('turnId') != source['turnId']
                or answered_question(turn.get('projection'), source['guidanceId']) is None):
            raise PermissionError('Original worker objective changed before dispatch')
        where += ' (the owner\u2019s answer to question ' + source['guidanceId'] + ' that reply asked)'
    elif 'ownerDelegation' in source:
        turn = repository.turn_get(source['conversationId'], source['turnId'], user_id=repository.owner,
                                   include_projection=False)
        if not turn or turn.get('actor') != 'assistant' or turn.get('turnId') != source['turnId']:
            raise PermissionError('Original worker objective changed before dispatch')
        where += (' (the lead acted under the owner\u2019s delegation to every lead, audit record '
                  + source['ownerDelegation'] + ')')
    else:
        turn = repository.turn_get(source['conversationId'], source['turnId'], user_id=repository.owner)
        if (not turn or turn.get('actor') != 'human' or turn.get('turnId') != source['turnId']
                or turn.get('projectionRevision') != source['projectionRevision']):
            raise PermissionError('Original worker objective changed before dispatch')
    return WORKER_INPUT_SOURCE_HEAD + source['conversationId'] + '; ' + where + WORKER_INPUT_SOURCE_TAIL


def worker_input(repository, request):
    """The assigned brief plus a link to its source; the accepted grant owns authority."""
    return request['brief'] + worker_input_source(repository, request)


FOLLOW_UP_INPUT_HEADER = (
    'Follow-up instruction from your Coordinator for this same assignment (delegated '
    'context, not a new human authorization). Continue in your existing checkout. Your '
    'reply becomes the assignment result the Coordinator reviews, so state the complete '
    'outcome.\n\n')


def follow_up_input(instruction):
    """A Coordinator follow-up within the same assignment: delegated context, not authority."""
    return FOLLOW_UP_INPUT_HEADER + instruction


def follow_up_instruction(text: str) -> str | None:
    """The instruction of a ``follow_up_input`` envelope, else ``None``."""
    return text[len(FOLLOW_UP_INPUT_HEADER):] if text.startswith(FOLLOW_UP_INPUT_HEADER) else None


def successor_brief(predecessor, instruction, *, held_checkpoint=None):
    """The brief of a new assignment that continues a final result in its conversation.

    Only immutable facts enter it, so a retried follow-up replays the same delegation.
    ``held_checkpoint`` is the accepted, pinned checkpoint whose delivery a conflict
    holds or a superseding rework stopped: the successor merges it first, and once that lands the held delivery
    settles as delivered by itself (``task_delta_delivery.checkpoint_contained``).
    """
    held = '' if not held_checkpoint else (
        'Its accepted checkpoint ' + held_checkpoint + ' did not land (a conflict with the shared '
        'branch holds its delivery, or a rework review stopped it). First merge commit '
        + held_checkpoint + ' into your '
        'checkout, resolve each conflicted file keeping both sides\' intent (regenerate generated '
        'outputs from the merged sources, never hand-merge them) and rerun the checks covering those '
        'files; once your work lands, the held delivery settles as delivered by itself.\n\n')
    return ('Follow-up in this same conversation: a new assignment continuing assignment '
            + predecessor['id'] + ', whose result is final (reviewed or delivered). You work in a '
            'fresh isolated checkout of the current project branch, your working directory now: it '
            'holds earlier work only where that work was delivered, and checkouts named earlier in '
            'this conversation are no longer yours.\n\n' + held + instruction)


class ReviewResultChanged(ProjectDispatchError):
    """The reviewed result revision is not the completed Worker Turn's current one; nothing was recorded.

    The result revision is the Turn's ``modelContextRevision``: display-only
    enrichment (translation overlays, usage, timing) never advances it, but a
    genuine change such as late background-command output does, after which the
    reviewer must judge the result again.
    """

    code = 'result_revision_changed'
    execution_started = False

    def __init__(self, requested, current, checkpoint_sha):
        super().__init__(f'Review requires the exact current completed worker result: result_revision '
                         f'{requested} is stale, the current result_revision is {current}')
        self.details = {'requested_result_revision': requested, 'current_result_revision': current,
                        'checkpoint_sha': checkpoint_sha}
        self.recovery = (f'Nothing was recorded. The completed Worker result advanced to revision {current} '
                         '(e.g. late background-command output). Re-read it with '
                         'get_project_assignment if it may have changed, then repeat review_project_work '
                         f'with result_revision={current}'
                         + (f' and checkpoint_sha={checkpoint_sha}' if checkpoint_sha else '') + '.')


def _registered_checkpoint(repository, row):
    """The checkpoint a repeated review would pin: the pinned one, else the registered workspace's."""
    if row.get('publication'):
        return row['publication']['checkpointSha']
    if row['request'].get('profile', {}).get('mode') not in ('files', 'project'):
        return None
    workspace = repository.get_workspace(str(Path(repository.project).expanduser().resolve()),
                                         repository.workspace_task_id(row), user_id=repository.owner)
    return (workspace or {}).get('checkpoint_sha') or None


def _superseding_rework(repository, row, attempt, recorded, review):
    """A rework that supersedes the recorded accept or reject of the same result, else ``review``.

    Audit 2026-11-18 #6: an immutable review forced a new, unlinked repair
    assignment for every conflict or second thought. A rework of the same
    result revision may replace an accept or reject whose result was not
    delivered; it keeps the superseded judgment (``supersedes``) so the history
    stays append-only, and ``rework_of`` (or a follow_up) then links the repair to
    it. A rework stops a pinned publication: ``validate_publication`` admits only
    an accept. A delivered result is final and keeps its review. Repeating the
    same superseding rework returns the recorded one.
    """
    if review['decision'] != 'rework':
        return review
    if recorded.get('decision') == 'rework' and 'supersedes' in recorded:
        return recorded if {key: value for key, value in recorded.items() if key != 'supersedes'} == review else review
    if recorded.get('decision') not in ('accept', 'reject') or recorded.get('resultRevision') != review['resultRevision']:
        return review
    from .publication import delivery_final
    if delivery_final(row, attempt, repository.get_workspace):
        raise ValueError('This result was delivered, so its review is final; delegate a new repair '
                         'assignment on the current branch')
    return {**review, 'supersedes': deepcopy(recorded)}


def review_worker(repository, task, arguments):
    """Pin a Coordinator review/publication without representing human acceptance."""
    _validate_schema(arguments, 'coordinatorReviewRequest')
    if arguments.get('checkpoint_sha') and arguments['decision'] != 'accept':
        raise ValueError('Only accepted work may request checkpoint publication')
    def record(value):
        row = repository._find(value, arguments['assignment_id'])
        worker_control(repository, task, row, session=True)
        repository.validate_own(repository._admission(row), value=value)
        execution = row['execution'] or {}
        attempt = repository.get_attempt(execution.get('attemptId'))
        turn = repository.turn_get(row['request']['recipientId'], execution.get('turnId'),
                                   user_id=repository.owner, include_projection=False)
        if (not attempt or not turn or attempt.get('status') != 'completed'
                or attempt.get('attemptId') != execution.get('attemptId')
                or attempt.get('turnId') != execution.get('turnId')
                or attempt.get('conversationId') != row['request']['recipientId']
                or turn.get('turnId') != execution.get('turnId') or turn.get('actor') != 'assistant'
                or turn.get('conversationId') != row['request']['recipientId']):
            raise ValueError('Review requires the exact current completed worker result')
        if model_context_revision(turn) != arguments['result_revision']:
            raise ReviewResultChanged(arguments['result_revision'], model_context_revision(turn),
                                      _registered_checkpoint(repository, row))
        review = {'actor': 'coordinator', 'decision': arguments['decision'],
                  'note': arguments['note'], 'resultRevision': arguments['result_revision']}
        from .acceptance import judged
        verdicts = judged(row['request'].get('acceptance') or [], arguments['decision'],
                          arguments.get('acceptance_results'))
        if verdicts:
            review['acceptanceResults'] = verdicts
        checkpoint = arguments.get('checkpoint_sha')
        old, recorded = row.get('publication'), row['review']
        accepts = (bool(checkpoint) and recorded is not None and recorded.get('actor') == 'coordinator'
                   and recorded.get('decision') == 'accept' and review['decision'] == 'accept')
        if (accepts and old is not None and checkpoint == old['checkpointSha']
                and review['resultRevision'] == recorded['resultRevision'] == old['resultRevision']):
            # The pinned publication asked for again, e.g. by a restart-resumed
            # attempt whose cut call had committed: the recorded judgment stands.
            review = recorded
        # The same execution can gain a later result revision after review
        # (late background-command output). An accept is re-affirmed with its
        # checkpoint at the current revision, which was verified above: a pinned
        # one keeps its checkpoint, and one recorded without a pin keeps its
        # note and gains the pin. Reviews recorded before result revisions
        # excluded display-only updates hold a higher projection revision, so
        # currency, not ordering, decides.
        reaffirm = accepts and recorded != review and (
            checkpoint == old['checkpointSha'] if old is not None else review['note'] == recorded['note'])
        if recorded is not None and recorded != review and not reaffirm:
            review = _superseding_rework(repository, row, attempt, recorded, review)
        if recorded is not None and recorded != review and not reaffirm and 'supersedes' not in review:
            if accepts and old is None:
                from .publication import PublicationRefused
                raise PublicationRefused(
                    'the accepted review is immutable and this call changes its note',
                    'Repeat review_project_work with decision=accept, the recorded note '
                    '(get_project_assignment review.note), the current result_revision and checkpoint_sha.')
            raise ValueError('Worker review is immutable: only decision=rework may supersede an accept or '
                             'reject of this same result_revision before it is delivered; otherwise delegate '
                             'a new repair assignment')
        if checkpoint:
            project_root = str(Path(repository.project).expanduser().resolve())
            work_id = repository.workspace_task_id(row)
            workspace = repository.get_workspace(project_root, work_id, user_id=repository.owner)
            if (not workspace or workspace.get('checkpoint_sha') != checkpoint
                    or workspace.get('task_id') != work_id
                    or workspace.get('user_id') != repository.owner or workspace.get('state') == 'discarded'
                    or workspace.get('project_root') != project_root
                    or (workspace.get('origin') or {}).get('_projectDispatchAdmission') != repository._admission(row)):
                raise ValueError('Publication requires the exact registered worker checkpoint')
            # Raised inside this CAS: an accept that cannot publish is never recorded.
            from .publication import require_publishable
            require_publishable(workspace, attempt.get('taskId') or '')
            origin = row['request']['delegation']
            publication = {'resultRevision': arguments['result_revision'], 'grantRevision': row['grant']['revision'],
                'checkpointSha': checkpoint, 'subject': 'coordinator:' + origin['taskId'], 'requestedAt': repository.clock()}
            if reaffirm:
                old = None
            if old is not None and any(old[key] != publication[key]
                                      for key in ('resultRevision', 'grantRevision', 'checkpointSha')):
                raise ValueError('Worker publication is immutable')
            # Human steering may review earlier work. Keep publication anchored
            # to that Worker's original grant, including when an authorized
            # repeat review repairs an older receipt attributed to the reviewer.
            row['publication'] = {**(old or publication), 'subject': publication['subject']}
        row['review'] = review
        return row
    return repository._change(record, selected=repository._assignment_selection(arguments['assignment_id']))


def validate_delegation_policy(repository, value, request):
    """Fence request identity and issued capabilities inside the grant CAS.

    Legacy maxConcurrent values remain immutable receipt data, never admission
    limits. Neither active Worker count nor local memory can refuse a grant.
    """
    delegation = request['delegation']
    from .request_budget import validate_policy
    validate_policy(repository, value, request)
    for row in value['assignments']:
        prior = row['request'].get('delegation')
        if not prior or prior['attemptId'] != delegation['attemptId']:
            continue
        if prior != delegation:
            raise ProjectDispatchError('Delegation source policy changed within one request')
