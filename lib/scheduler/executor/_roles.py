"""Scheduler tools for project roles: a Worker, a Coordinator, the Global Router.

Owner: docs/modules/scheduling_ops.md, "Governed scheduling: one primitive,
scoped grants" and "Agent-created schedules". The ordinary scheduler tool
names, with role projections of their schemas
(``tool_defs.project_role_scheduler_surface``). A role creates wakes, prompt
rows and, only when it holds code execution, code rows; each records its
grant and purpose. A role controls a row only under
``authority.require_control`` and never resumes or fires a row the owner
paused; the Global Router reaches the owner's own, service and system rows
only under the owner's delegation (docs/ROUTER.md R9, scope ``scheduler``).
Every refusal is a failed call that changed nothing, and every outcome writes
one audit event.

Entry points: execute_role_scheduler_tool, failed_text.
Dependencies: scheduler.authority, scheduler.manager, lib.lifecycle_approval
and lib.router.owner_delegation (delegated control only).
"""
from __future__ import annotations

from lib.log import audit_log, get_logger
from lib.scheduler.authority import (
    SchedulerActor, SchedulerRefused, agent_task_id, attribution, controls, creator_fields, encode_grant,
    owner_paused, project_grant, project_wake_task_id, read_grant, require_control, row_scope,
)
from lib.scheduler.tool_defs import (
    DELEGATED_MANAGE_ACTIONS, PROJECT_WAKE_TYPE, ROLE_CODE_TASK_TYPES, ROLE_CREATE_TASK_TYPES,
    ROLE_MANAGE_ACTIONS, ROLE_UPDATABLE_FIELDS,
)

logger = get_logger(__name__)

_DELEGATION_SCOPE = 'scheduler'


def failed_text(text: str):
    """A scheduler result carrying the failed execution verdict, so it never reads done."""
    from lib.tools.result_value import TrustedResultText, capture_result
    return TrustedResultText(text, canonical=capture_result(text, execution_status='error'))


def _manager():
    from lib.scheduler.manager import get_scheduler
    return get_scheduler()


def _text(arguments: dict, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} is required')
    return value.strip()


def _audit(event: str, actor: SchedulerActor, *, action: str, task_id: str = '', scope: str = '',
           row: dict | None = None, **fields) -> None:
    audit_log(event, action=action, role=actor.role, owner_user_id=actor.owner, tenant_id=actor.tenant,
              conversation=actor.conversation_id, turn=actor.turn_id or '',
              assignment=actor.assignment_id or '', project=actor.project_path or '',
              task_id=task_id, row_scope=scope, **creator_fields(row), **fields)


def execute_role_scheduler_tool(actor: SchedulerActor, fn_name: str, fn_args: dict, *, task: dict):
    action = str(fn_args.get('action') or ('create' if fn_name == 'schedule_create' else 'list'))
    try:
        if fn_name == 'schedule_list':
            return _list(actor, fn_args)
        if fn_name == 'schedule_create':
            return _create(actor, fn_args)
        if fn_name == 'schedule_manage':
            return _manage(actor, fn_args, task)
        raise SchedulerRefused('scheduler_tool_unavailable',
                               f'{fn_name} is not available to a {actor.role}: project roles schedule '
                               'with schedule_create.')
    except (SchedulerRefused, PermissionError, ValueError) as refusal:
        code = getattr(refusal, 'code', None)
        code = code if isinstance(code, str) else (
            'scheduler_invalid_argument' if isinstance(refusal, ValueError) else 'scheduler_refused')
        _audit('scheduled_task_refused', actor, action=action, task_id=str(fn_args.get('task_id') or ''),
               code=code, detail=str(refusal)[:300])
        return failed_text(f'Error: {refusal} Nothing changed. (code: {code})')


def _list(actor: SchedulerActor, arguments: dict) -> str:
    tasks = _manager().list_tasks(user_id=actor.owner,
                                  include_disabled=bool(arguments.get('include_disabled', False)))
    if not tasks:
        return 'No scheduled tasks found. Schedule one with schedule_create.'
    lines = [f'Scheduled Tasks ({len(tasks)}); [yours] marks the ones you control:', '-' * 60]
    for row in tasks:
        scope = row_scope(row)
        grant = read_grant(row) if scope == 'project' else None
        mine = ' [yours]' if grant is not None and controls(actor, row) else ''
        lines.append(
            f'[{"enabled" if row["enabled"] else "disabled"}] [{row["id"]}] {row["name"]} \u2014 {scope}{mine}\n'
            f'    Schedule: {row.get("schedule_human", row["schedule"])}\n'
            f'    Runs: {row.get("run_count", 0)} | Last: {row.get("last_status") or "never"} '
            f'{row.get("last_run") or ""} | Next: {row.get("next_run") or "N/A"}')
        if grant is not None:
            lines.extend(attribution_lines(row))
        else:
            lines.append(f'    Command: {str(row.get("command") or "")[:100]}')
        lines.append('')
    return '\n'.join(lines)


def attribution_lines(row: dict) -> list[str]:
    """How a listing shows an agent-created row: type, creator, purpose, creator state."""
    described = attribution(row)
    if described is None:
        return []
    created_by = described['created_by']
    assignment = f' (assignment {created_by["assignmentId"]})' if created_by['assignmentId'] else ''
    lines = [f'    Type: {row.get("task_type")} | Project: {created_by["projectPath"]} | lead '
             f'{created_by["leadConversationId"]}',
             f'    Created by: {created_by["role"]} {created_by["conversationId"]}{assignment}',
             f'    Purpose: {described["purpose"]}']
    state = row.get('creator_state')
    if isinstance(state, dict):
        lines.append(f'    Creator: {state["state"]}' + (f' ({state["reason"]})' if state.get('reason') else ''))
    if owner_paused(row):
        lines.append('    Paused by the owner: only the owner resumes it.')
    return lines


def _same_task(row: dict, stored: dict | None, *, name, schedule, grant, command) -> bool:
    return (stored is not None and row.get('name') == name and row.get('schedule') == schedule
            and (command is None or row.get('command') == command)
            and all(stored.get(key) == grant[key] for key in ('reason', 'projectPath', 'coordinatorId'))
            and all(stored['creator'].get(key) == grant['creator'][key]
                    for key in ('role', 'conversationId', 'assignmentId')))


_AGENT_TURN_REMEDY = (
    'An agent task runs a full-tool Turn with its target conversation\u2019s capabilities, which your '
    'grant does not hold. Schedule a project_wake: its Turn runs in your Coordinator under that '
    'Coordinator\u2019s own grant.')


def _max_runtime(arguments: dict) -> int:
    value = arguments.get('max_runtime', 300)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('max_runtime must be a whole number of seconds')
    return int(value)


def _create(actor: SchedulerActor, arguments: dict) -> str:
    name, schedule = _text(arguments, 'name'), _text(arguments, 'schedule')
    purpose, key = _text(arguments, 'purpose'), _text(arguments, 'idempotency_key')
    task_type = arguments.get('task_type') or PROJECT_WAKE_TYPE
    if task_type not in ROLE_CREATE_TASK_TYPES:
        raise SchedulerRefused('scheduler_task_type',
                               f'task_type must be one of {", ".join(ROLE_CREATE_TASK_TYPES)}. '
                               + (_AGENT_TURN_REMEDY if task_type == 'agent' else ''))
    if task_type in ROLE_CODE_TASK_TYPES and not actor.code_execution:
        raise SchedulerRefused('scheduler_capability_code_execution',
                               f'A {task_type} task runs code, and your grant holds no code execution '
                               '(run_command). Delegate a Worker that holds run_command to schedule it, or '
                               'schedule a prompt or project_wake.')
    manager = _manager()
    if task_type == PROJECT_WAKE_TYPE:
        if arguments.get('command'):
            raise ValueError('a project_wake runs nothing, so it takes no command; its purpose is its note')
        if actor.role == 'worker' and not schedule.startswith('once:'):
            raise SchedulerRefused('scheduler_worker_one_shot',
                                   'A Worker wake is one-shot: use once:YYYY-MM-DD HH:MM. Recurring check-ins '
                                   'belong to your Coordinator.')
        grant, command = project_grant(actor, purpose), None
        task_id = project_wake_task_id(actor.owner, actor.conversation_id, key)

        def create():
            return manager.create_task(
                name, schedule, encode_grant(grant), principal=actor.project_row_creation_principal(),
                task_type=PROJECT_WAKE_TYPE, description=purpose, target_conv_id=actor.coordinator_id,
                source_conv_id=actor.conversation_id, task_id=task_id)
    else:
        command = _text(arguments, 'command')
        workspace = actor.project_path if task_type in ROLE_CODE_TASK_TYPES else None
        grant = project_grant(actor, purpose, workspace=workspace)
        task_id = agent_task_id(actor.owner, actor.conversation_id, key)
        max_runtime = _max_runtime(arguments)

        def create():
            return manager.create_task(
                name, schedule, command, principal=actor.project_row_creation_principal(), task_type=task_type,
                description=purpose, max_runtime=max_runtime, source_conv_id=actor.conversation_id,
                creator_grant=grant, task_id=task_id)
    row = manager.get_task(task_id, user_id=actor.owner)
    created = row is None
    if created:
        try:
            row = create()
        except ValueError:
            raise
        except Exception:
            # A concurrent identical call may have won the create; its row is the answer.
            row = manager.get_task(task_id, user_id=actor.owner)
            if row is None:
                raise
            created = False
    if not created and not _same_task(row, read_grant(row), name=name, schedule=schedule, grant=grant,
                                      command=command):
        raise SchedulerRefused('scheduler_idempotency_conflict',
                               f'idempotency_key {key!r} already names task {task_id} with a different '
                               'name, schedule, command or purpose. Use a new idempotency_key for a different '
                               f'task, or change this one with schedule_manage(action="update", '
                               f'task_id="{task_id}").')
    _audit('scheduled_task_controlled', actor, action='create', task_id=task_id, scope='project', row=row,
           outcome='created' if created else 'replayed')
    header = ('Wake scheduled' if task_type == PROJECT_WAKE_TYPE else 'Task scheduled') if created else (
        'Already scheduled with this idempotency_key; nothing changed')
    lines = [f'{header}.', f'  ID: {task_id}', f'  Name: {row["name"]}', f'  Schedule: {row["schedule"]}',
             f'  Type: {task_type}']
    if task_type == PROJECT_WAKE_TYPE:
        lines += [f'  Wakes: {actor.coordinator_id} (project {actor.project_path})',
                  '  It delivers one nonhuman notice with your purpose and grants no authority.']
    else:
        lines += [f'  Runs in: {grant["workspace"]}' if task_type in ROLE_CODE_TASK_TYPES else
                  '  Runs: one model call without tools',
                  '  It is in the owner\u2019s scheduled-task list under your name and purpose; the owner, '
                  f'your lead ({actor.coordinator_id if actor.role == "worker" else "the Global Router"}) '
                  'and you can pause or delete it with schedule_manage. Its runs: '
                  f'schedule_manage(action="log", task_id="{task_id}").']
    return '\n'.join(lines)


def _manage(actor: SchedulerActor, arguments: dict, task: dict) -> str:
    action = arguments.get('action')
    if action not in ROLE_MANAGE_ACTIONS:
        raise SchedulerRefused('scheduler_invalid_action',
                               f'action must be one of {", ".join(ROLE_MANAGE_ACTIONS)}.')
    task_id = _text(arguments, 'task_id')
    manager = _manager()
    row = manager.get_task(task_id, user_id=actor.owner)
    if row is None:
        raise SchedulerRefused('scheduler_task_not_found', _not_found(task_id))
    if arguments.get('owner_delegation') is not None:
        return _delegated(actor, task, row, action, arguments)
    if action == 'log':
        history = manager.get_task_history(task_id, user_id=actor.owner, limit=20)
        if not history:
            return f'No execution log entries for task {task_id} in this server process.'
        return '\n'.join([f'Recent runs of {task_id}:'] + [
            f'  [{"OK" if entry["success"] else "FAIL"}] [{entry["time"]}] {entry["result"][:200]}'
            for entry in reversed(history)])
    grant = require_control(actor, row)
    scope = row_scope(row)
    if action in ('enable', 'run') and owner_paused(row):
        raise SchedulerRefused('scheduler_owner_paused',
                               f'The owner paused task {task_id}; only the owner resumes or fires it, in the '
                               'scheduled-task list. Ask the owner if it should run again.')
    if action == 'run' and row.get('task_type') in ROLE_CODE_TASK_TYPES and not actor.code_execution:
        raise SchedulerRefused('scheduler_capability_code_execution',
                               f'Task {task_id} runs code and you hold no code execution (run_command), so '
                               'you cannot fire it now; it runs at its schedule. The owner, or a Worker '
                               'holding run_command that controls it, can fire it.')
    if action in ('disable', 'enable'):
        changed = manager.update_task(task_id, user_id=actor.owner, enabled=action == 'enable')
        outcome = f'Task {task_id} {"resumed" if action == "enable" else "paused"}.'
    elif action == 'delete':
        changed = manager.delete_task(task_id, user_id=actor.owner)
        outcome = f'Task {task_id} cancelled.'
    elif action == 'run':
        success, result = manager.run_task_now(task_id, user_id=actor.owner)
        changed = success is not None
        if changed and not success:
            _audit('scheduled_task_controlled', actor, action=action, task_id=task_id, scope=scope, row=row,
                   outcome='failed')
            return failed_text(f'Error: task {task_id} ran and failed: {result[:2000]}')
        outcome = f'Task {task_id} fired now: {result[:2000]}'
    else:
        updates = arguments.get('updates')
        if not isinstance(updates, dict) or not updates:
            raise ValueError('update needs updates with ' + ', '.join(ROLE_UPDATABLE_FIELDS))
        refused = sorted(set(updates) - set(ROLE_UPDATABLE_FIELDS))
        if refused:
            raise SchedulerRefused('scheduler_update_field',
                                   f'update cannot set {", ".join(refused)}; a wake\u2019s grant never changes. '
                                   f'Updatable: {", ".join(ROLE_UPDATABLE_FIELDS)}.')
        if 'schedule' in updates:
            from lib.scheduler.manager import validate_schedule
            if (row.get('task_type') == PROJECT_WAKE_TYPE and grant['creator']['role'] == 'worker'
                    and not str(updates['schedule']).startswith('once:')):
                raise SchedulerRefused('scheduler_worker_one_shot', 'A Worker wake stays one-shot (once:).')
            validate_schedule(str(updates['schedule']))
        changed = manager.update_task(task_id, user_id=actor.owner, **updates)
        outcome = f'Task {task_id} updated: {", ".join(updates)}.'
    if not changed:
        raise SchedulerRefused('scheduler_task_not_found', _not_found(task_id))
    _audit('scheduled_task_controlled', actor, action=action, task_id=task_id, scope=scope, row=row,
           outcome='applied')
    return outcome


def _not_found(task_id: str) -> str:
    return f'Task {task_id} not found; schedule_list shows the tasks and their IDs.'


def _delegated(actor: SchedulerActor, task: dict, row: dict, action: str, arguments: dict) -> str:
    """Pause or resume an owner, service or system row under the owner's delegation (ROUTER.md R9)."""
    if actor.role != 'router':
        raise SchedulerRefused('scheduler_delegation_router_only',
                               'Only the owner\u2019s Global Router acts under the owner\u2019s delegation. Ask '
                               'the owner, or the Global Router, to pause or resume it.')
    scope = row_scope(row)
    if scope == 'project':
        raise SchedulerRefused('scheduler_delegation_project_row',
                               'A project wake is controlled by its project\u2019s Coordinator under its own '
                               'grant, not under the owner\u2019s delegation: call schedule_manage without '
                               'owner_delegation.')
    if action not in DELEGATED_MANAGE_ACTIONS:
        raise SchedulerRefused('scheduler_delegation_action',
                               f'Under the owner\u2019s delegation only disable and enable are available; '
                               f'{action} needs the owner.')
    key = _text(arguments, 'idempotency_key')
    from lib import lifecycle_approval as gate
    from lib.router.contract import validate
    from lib.router.owner_delegation import resolve_trigger, router_requester
    authority = validate(dict(arguments['owner_delegation']), 'ownerDelegationAuthority')
    owner, requester = actor.owner, router_requester(task, actor.scope)
    target = {'kind': 'scheduled_task', 'taskId': row['id'], 'taskScope': scope, 'action': action}
    prior = gate.delegated_action(owner, gate.delegated_action_id(owner, requester, _DELEGATION_SCOPE, key))
    if prior is not None:
        if prior['status'] != 'completed':
            raise SchedulerRefused(prior.get('code') or 'scheduler_delegation_replayed',
                                   f'This idempotency_key already ended {prior["status"]} '
                                   f'({prior.get("detail") or "no detail"}).')
        return f'Already done under the owner\u2019s delegation (record {prior["id"]}); nothing changed.'
    gate.check_delegation(owner, requester, _DELEGATION_SCOPE)
    try:
        trigger = resolve_trigger(task, actor.scope, authority)
    except gate.DelegationRefused as refusal:
        gate.record_delegated_refusal(owner, requester, scope=_DELEGATION_SCOPE, reason=authority['reason'],
                                      trigger=None, target=target, code=refusal.code, detail=str(refusal))
        raise
    record, replayed = gate.begin_delegated_action(owner, requester, scope=_DELEGATION_SCOPE, key=key,
                                                   reason=authority['reason'], trigger=trigger, target=target)
    if replayed:
        return f'Already recorded under the owner\u2019s delegation (record {record["id"]}); nothing changed.'
    try:
        changed = _manager().update_task(row['id'], user_id=owner, enabled=action == 'enable')
    except Exception as error:
        gate.settle_delegated_action(owner, record['id'], status='failed', code=type(error).__name__,
                                     detail=str(error))
        raise
    if not changed:
        gate.settle_delegated_action(owner, record['id'], status='failed', code='scheduler_task_not_found',
                                     detail=f'Task {row["id"]} was gone')
        raise SchedulerRefused('scheduler_task_not_found', _not_found(row['id']))
    gate.settle_delegated_action(owner, record['id'], status='completed')
    _audit('scheduled_task_controlled', actor, action=action, task_id=row['id'], scope=scope,
           outcome='applied', delegated_action=record['id'])
    return (f'Task {row["id"]} {"resumed" if action == "enable" else "paused"} under the owner\u2019s '
            f'delegation; audit record {record["id"]}.')


__all__ = ['attribution_lines', 'execute_role_scheduler_tool', 'failed_text']
