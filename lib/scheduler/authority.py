"""Scheduler authority: row scopes, role actors and project-row grants.

Owner: docs/modules/scheduling_ops.md, "Governed scheduling: one primitive,
scoped grants". The scheduler is the one timer primitive; this module decides
who may reach which of its rows through the scheduler tools.

* A row's scope is derived from the row, never from a tool argument:
  ``system`` (server built-ins), ``service`` (rows a feature owns for the
  owner), ``owner`` (the schedulable types an owner creates) and ``project``
  (rows a Coordinator, the Global Router or a Worker created: ``project_wake``
  rows and prompt/code rows carrying a creator grant).
* A role actor is resolved from its live task: a project Worker from its
  validated dispatch grant, a Coordinator or the Global Router from its
  coordinator scope. Everything else is the owner's own conversation, which
  keeps the ordinary scheduler surface.
* A project row carries its grant in the row, so the row and its authority
  commit in one storage command: a wake's ``command`` is the canonical JSON
  grant; a prompt or code row's ``tools_config[CREATOR_GRANT_KEY]`` is.
* A row's attribution (creator, purpose) and its creator's current state are
  read from that grant for the owner's views; the creator's end never stops it.

Entry points: row_scope, resolve_actor, project_grant, encode_grant,
read_grant, project_wake_task_id, agent_task_id, require_control,
attribution, creator_fields, creator_state, owner_paused, SchedulerRefused,
PROJECT_WAKE_TYPE.
Dependencies: lib.identity, lib.project_dispatch.admission (Worker grants),
lib.tasks_pkg.coordinator_policy (Coordinator scope), lib.router.policy and
lib.router.watchdog.binding (role kinds).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from lib.identity import PrincipalContext, require_user_id
from lib.log import get_logger
from lib.scheduler.tool_defs import (
    CREATOR_GRANT_KEY, OWNER_PAUSE_KEY, PROJECT_WAKE_TYPE, ROLE_CREATE_TASK_TYPES,
    SCHEDULABLE_TASK_TYPES,
)

logger = get_logger(__name__)

# Built-in types run system-scoped or deployment-wide work (manager._execute_task).
BUILTIN_TASK_TYPES = frozenset({
    'optimizer', 'storage_backup', 'provider_probe_daily', 'model_intelligence_daily',
    'daily_report_backfill', 'reserve_reclaim', 'proactive_digest', 'proactive_quiet_release',
})
# Rows a feature creates and owns for the owner: the Router Watchdog review and
# re-checks (docs/ROUTER.md R7) and Coordinator commitment wakes (R10).
SERVICE_TASK_TYPES = frozenset({'router_watchdog_review', 'coordinator_commitment_due'})
ROLES = ('owner', 'worker', 'coordinator', 'router')
_GRANT_VERSION = 1
_CREATOR_ROLES = frozenset({'worker', 'coordinator', 'router'})


class SchedulerRefused(PermissionError):
    """A scheduler tool call the caller's grant does not cover; nothing changed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SchedulerActor:
    """Who calls a scheduler tool, with the grant facts a project row records."""

    role: str
    owner: int
    tenant: str | None
    conversation_id: str
    task_id: str
    turn_id: str | None = None
    attempt_id: str | None = None
    project_path: str | None = None
    coordinator_id: str | None = None
    binding_revision: int | None = None
    assignment_id: str | None = None
    scope: dict | None = None
    # Whether the actor's admitted tools run code (``run_command``): only then
    # may it create or fire a code task.
    code_execution: bool = False
    # A Worker's delegating request: the Coordinator Attempt that issued its grant.
    request_attempt_id: str | None = None

    def principal(self) -> PrincipalContext:
        """The least-privilege scheduling identity rows of this actor are created with."""
        return PrincipalContext.user(
            subject_id=f'scheduler-{self.role}:{self.conversation_id or self.owner}',
            owner_user_id=self.owner, tenant_id=self.tenant, scopes={'agents:scheduler'})


def row_scope(row: dict) -> str:
    """``system``, ``service``, ``owner`` or ``project``; an unknown type is ``system``."""
    task_type = row.get('task_type')
    if task_type == PROJECT_WAKE_TYPE:
        return 'project'
    if task_type in SERVICE_TASK_TYPES:
        return 'service'
    if task_type in SCHEDULABLE_TASK_TYPES:
        return 'project' if read_grant(row) is not None else 'owner'
    return 'system'


def resolve_actor(task: dict) -> SchedulerActor:
    """The scheduler actor of a live task. Lookup failures propagate (fail closed)."""
    from lib.tasks_pkg.manager import task_user_id
    owner = require_user_id(task_user_id(task), context='scheduler tool')
    payload = task.get('_principalContext')
    tenant = PrincipalContext.from_payload(payload).tenant_id if payload else None
    base = dict(owner=owner, tenant=tenant, conversation_id=str(task.get('convId') or ''),
                task_id=str(task.get('id') or ''), turn_id=task.get('_turnId'),
                attempt_id=task.get('_attemptId'))
    from lib.project_dispatch.admission import is_dispatch_task, profile_for, validated_dispatch
    if is_dispatch_task(task):
        validated = validated_dispatch(task)
        if validated is None:
            raise SchedulerRefused('scheduler_grant_unavailable',
                                   'This task has no project dispatch grant to schedule under. Ask your '
                                   'Coordinator to schedule it.')
        binding, assignment = validated
        profile = profile_for(assignment)
        mode = profile['mode']
        if mode == 'project':
            from lib.project_dispatch.scope import granted_tools
            request = assignment['request']
            return SchedulerActor(role='worker', project_path=request['projectPath'],
                                  coordinator_id=request['coordinatorId'],
                                  binding_revision=assignment['grant'].get('bindingRevision'),
                                  assignment_id=binding['assignmentId'],
                                  code_execution='run_command' in granted_tools(profile),
                                  request_attempt_id=(request.get('delegation') or {}).get('attemptId'),
                                  **base)
        if mode != 'coordinator':
            raise SchedulerRefused('scheduler_role_unsupported',
                                   f'A {mode} assignment cannot use the scheduler. Ask your Coordinator to '
                                   'schedule it.')
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    scope = coordinator_scope(task)
    if not scope:
        return SchedulerActor(role='owner', **base)
    from lib.router.watchdog.binding import is_watchdog_scope
    if is_watchdog_scope(scope):
        raise SchedulerRefused('scheduler_role_unsupported',
                               'The Watchdog arms its re-checks with watchdog_schedule_recheck.')
    from lib.router.policy import is_router_scope
    from lib.tasks_pkg.coordinator_policy import coordinator_tool_ceiling
    base['conversation_id'] = scope['conversation_id']
    return SchedulerActor(role='router' if is_router_scope(scope) else 'coordinator',
                          project_path=scope['project_path'], coordinator_id=scope['conversation_id'],
                          binding_revision=scope['binding_revision'], scope=dict(scope),
                          code_execution='run_command' in coordinator_tool_ceiling(scope, task),
                          **base)


def project_grant(actor: SchedulerActor, reason: str, *, workspace: str | None = None) -> dict:
    """The grant a project row records: who created it, for which Coordinator and why.

    ``workspace`` is the directory a code task runs in: the project the
    creator's grant names, never its ephemeral checkout.
    """
    if actor.role not in _CREATOR_ROLES:
        raise SchedulerRefused('scheduler_role_unsupported', 'Only project roles create project tasks.')
    grant = {'version': _GRANT_VERSION, 'scope': 'project', 'projectPath': actor.project_path,
             'coordinatorId': actor.coordinator_id, 'bindingRevision': actor.binding_revision,
             'creator': {'role': actor.role, 'conversationId': actor.conversation_id,
                         'taskId': actor.task_id, 'turnId': actor.turn_id,
                         'attemptId': actor.attempt_id, 'assignmentId': actor.assignment_id,
                         'requestAttemptId': actor.request_attempt_id},
             'reason': reason}
    if workspace is not None:
        grant['workspace'] = workspace
    return grant


def encode_grant(grant: dict) -> str:
    return json.dumps(grant, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _tools_config(row: dict) -> dict:
    """A row's ``tools_config`` mapping; the manager hands it out as JSON text."""
    config = row.get('tools_config')
    if isinstance(config, str):
        try:
            config = json.loads(config or '{}')
        except ValueError:
            return {}
    return config if isinstance(config, dict) else {}


def read_grant(row: dict) -> dict | None:
    """A project row's grant, or None when the row is not a well-formed project row."""
    task_type = row.get('task_type')
    if task_type == PROJECT_WAKE_TYPE:
        try:
            grant = json.loads(row.get('command') or '')
        except (TypeError, ValueError):
            return None
    elif task_type in ROLE_CREATE_TASK_TYPES:
        grant = _tools_config(row).get(CREATOR_GRANT_KEY)
    else:
        return None
    creator = grant.get('creator') if isinstance(grant, dict) else None
    if (not isinstance(creator, dict) or grant.get('version') != _GRANT_VERSION
            or grant.get('scope') != 'project' or creator.get('role') not in _CREATOR_ROLES
            or not all(isinstance(grant.get(key), str) and grant[key]
                       for key in ('projectPath', 'coordinatorId'))
            or not isinstance(grant.get('reason'), str)
            or (creator['role'] == 'worker' and not creator.get('assignmentId'))):
        return None
    return grant


def _creation_digest(owner: int, creator_conversation_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(json.dumps([int(owner), creator_conversation_id, idempotency_key],
                                     separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def project_wake_task_id(owner: int, creator_conversation_id: str, idempotency_key: str) -> str:
    """One wake per owner, creating conversation and idempotency key."""
    return 'project-wake-' + _creation_digest(owner, creator_conversation_id, idempotency_key)[:24]


def agent_task_id(owner: int, creator_conversation_id: str, idempotency_key: str) -> str:
    """One prompt or code task per owner, creating conversation and idempotency key."""
    return 'agent-task-' + _creation_digest(owner, creator_conversation_id, idempotency_key)[:24]


def _same_project(left: str | None, right: str | None) -> bool:
    from lib.conversations.project_identity import normalize_project_path
    return bool(left) and normalize_project_path(left) == normalize_project_path(right or '')


def controls(actor: SchedulerActor, row: dict) -> bool:
    """Whether ``actor`` may pause, resume, cancel, run or update ``row`` with its own grant."""
    try:
        require_control(actor, row)
    except SchedulerRefused:
        return False
    return True


def require_control(actor: SchedulerActor, row: dict) -> dict:
    """Return the project grant ``actor`` controls ``row`` under, else raise SchedulerRefused.

    A Worker controls the wakes created under its own assignment; a Coordinator
    and the Global Router control every project row of their project. Owner,
    service and system rows are never controlled with a role's own grant (the
    Global Router reaches them only under the owner's delegation).
    """
    scope = row_scope(row)
    if scope != 'project':
        raise SchedulerRefused(f'scheduler_{scope}_scope',
                               f'Task {row.get("id")} is a {scope} task; your grant does not cover it. '
                               + _OWNER_ROW_REMEDY[actor.role])
    grant = read_grant(row)
    if grant is None:
        raise SchedulerRefused('scheduler_grant_invalid',
                               f'Task {row.get("id")} carries no readable project grant. Only the owner '
                               'can change or delete it, in the scheduler panel.')
    # The Global Router leads every Project Coordinator, so it controls the
    # tasks of every project; a Coordinator leads only its own project's Workers.
    if actor.role != 'router' and not _same_project(grant['projectPath'], actor.project_path):
        raise SchedulerRefused('scheduler_out_of_project',
                               f'Task {row.get("id")} belongs to another project ({grant["projectPath"]}); '
                               'only that project\u2019s Coordinator, the Global Router or the owner '
                               'controls it.')
    if actor.role == 'worker' and grant['creator'].get('assignmentId') != actor.assignment_id:
        raise SchedulerRefused('scheduler_out_of_grant',
                               f'Task {row.get("id")} was created under another grant; a Worker controls '
                               'only the wakes it created. Ask your Coordinator to change it.')
    return grant


def owner_paused(row: dict) -> bool:
    """Whether the owner paused this agent-created task; then only the owner resumes it."""
    return _tools_config(row).get(OWNER_PAUSE_KEY) is True


def attribution(row: dict) -> dict | None:
    """Who created an agent-created row and why, for the owner's views; None otherwise."""
    grant = read_grant(row)
    if grant is None:
        return None
    creator = grant['creator']
    created_by = {key: creator.get(key) for key in (
        'role', 'conversationId', 'turnId', 'attemptId', 'assignmentId', 'requestAttemptId')}
    created_by.update(projectPath=grant['projectPath'], leadConversationId=grant['coordinatorId'])
    return {'created_by': created_by, 'purpose': grant['reason']}


def creator_fields(row: dict | None) -> dict:
    """The audit fields naming an agent-created row's creator and purpose."""
    described = attribution(row) if row is not None else None
    if described is None:
        return {}
    created_by = described['created_by']
    return {'created_by_role': created_by['role'], 'created_by_conversation': created_by['conversationId'],
            'created_by_assignment': created_by['assignmentId'] or '', 'purpose': described['purpose']}


def creator_state(owner: int, row: dict) -> dict:
    """Whether an agent-created row's creator is still active, from its live records.

    ``ended`` names why (a Worker's grant revoked or its result reviewed, a
    Coordinator's binding moved); the row keeps running either way. A failed
    lookup is ``unknown``, never ``ended``.
    """
    grant = read_grant(row)
    if grant is None:
        return {'state': 'unknown', 'reason': 'no_grant'}
    creator = grant['creator']
    try:
        if creator['role'] == 'worker':
            from lib.project_dispatch.repository import ProjectDispatchRepository
            assignment = ProjectDispatchRepository(owner, grant['projectPath']).read(creator['assignmentId'])
            if assignment['grant'].get('revoked'):
                return {'state': 'ended', 'reason': 'grant_revoked'}
            decision = (assignment.get('review') or {}).get('decision')
            if decision in ('accept', 'reject'):
                return {'state': 'ended', 'reason': f'reviewed_{decision}'}
            return {'state': 'active'}
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        binding = ProjectCoordinationRepository(owner).read(grant['projectPath'])
        if (binding.get('availability') != 'available'
                or binding.get('conversationId') != creator['conversationId']):
            return {'state': 'ended', 'reason': 'binding_changed'}
        return {'state': 'active'}
    except Exception as error:
        logger.debug('Creator state lookup failed for %s creator: %s',
                     creator.get('role'), type(error).__name__)
        return {'state': 'unknown', 'reason': type(error).__name__}


# Who can act on an owner, service or system row, told to each role that cannot.
_OWNER_ROW_REMEDY = {
    'worker': 'Ask your Coordinator; only the owner, or the Global Router under the owner\u2019s delegation, '
              'controls it.',
    'coordinator': 'Ask the owner, or the Global Router, which may pause or resume it under the owner\u2019s '
                   'delegation (\u201cAct for me\u201d).',
    'router': 'Pause or resume it under the owner\u2019s delegation: pass owner_delegation (the reason and the '
              'owner trigger) and an idempotency_key.',
}


__all__ = ['BUILTIN_TASK_TYPES', 'PROJECT_WAKE_TYPE', 'ROLES', 'SERVICE_TASK_TYPES', 'SchedulerActor',
           'SchedulerRefused', 'agent_task_id', 'attribution', 'controls', 'creator_fields', 'creator_state',
           'encode_grant',
           'owner_paused', 'project_grant', 'project_wake_task_id', 'read_grant', 'require_control',
           'resolve_actor', 'row_scope']
