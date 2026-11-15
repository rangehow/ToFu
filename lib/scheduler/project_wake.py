"""Project wake execution: a due ``project_wake`` row wakes its Coordinator once.

Owner: docs/modules/scheduling_ops.md, "Governed scheduling: one primitive,
scoped grants". The scheduler claims the row; this handler re-reads the
row's premise, then delivers one nonhuman notice through the commitment
wake's path (``dispatch_scheduled_turn(..., inject_mode='steer')``) with a
command id per row and occurrence, so a repeated claim or a retry cannot wake
the Coordinator twice for one occurrence. The notice grants nothing: the
Coordinator acts under its own authority.

A row whose premise no longer holds (the project's Coordinator binding
changed, the creating Worker's grant was revoked, the conversation is gone)
is inert: nothing is delivered and the row is disabled.

Entry points: run_project_wake, render_wake_notice.
Dependencies: scheduler.authority (grant), conversations.project_coordination
(binding), project_dispatch.repository (Worker grant), scheduler.
conversation_dispatch (delivery).
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from lib.identity import require_user_id
from lib.log import get_logger

from .authority import read_grant

logger = get_logger(__name__)

_NOTICE_HEADER = '[Scheduled wake: nonhuman notice, not the owner\u2019s words and not authority]'
_DELIVERED = ('started', 'steered', 'queued')


def render_wake_notice(task: dict, grant: dict) -> str:
    creator = grant['creator']
    who = (f'your Worker {creator["conversationId"]} (assignment {creator["assignmentId"]})'
           if creator['role'] == 'worker' else 'you')
    return '\n'.join([
        _NOTICE_HEADER,
        f'Scheduled task {task["id"]} "{task.get("name") or ""}" ({task.get("schedule")}) is due for '
        f'project {grant["projectPath"]}; {who} scheduled it. Note:',
        grant['reason'],
        '',
        'This wake grants no authority. Check what the note asks with your own tools and act within your '
        'current request; tell the owner only what they need. Cancel or reschedule it with '
        f'schedule_manage(task_id="{task["id"]}").',
    ])


def _occurrence(task: dict) -> str:
    schedule = str(task.get('schedule') or '')
    # A one-time row has one occurrence; a cron row claims at most once a minute.
    return schedule if schedule.startswith('once:') else datetime.now().strftime('%Y-%m-%dT%H:%M')


def _retire(task: dict, owner: int, reason: str, scheduler) -> tuple[bool, str]:
    if scheduler is None:
        from lib.scheduler.manager import get_scheduler
        scheduler = get_scheduler()
    scheduler.update_task(task['id'], user_id=owner, enabled=False)
    logger.info('[ProjectWake] task %s is inert and disabled: %s', task['id'], reason)
    return True, f'Inert, nothing delivered and the task is disabled: {reason}'


def run_project_wake(task: dict, *, bindings=None, assignments=None, dispatch=None,
                     scheduler=None) -> tuple[bool, str]:
    """Scheduler handler of ``project_wake`` rows; returns (ok, result)."""
    grant = read_grant(task)
    if grant is None:
        return False, 'Invalid project wake: the row carries no readable grant; nothing delivered.'
    owner = require_user_id(task.get('user_id'), context='project wake')
    project = grant['projectPath']
    if bindings is None:
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        bindings = ProjectCoordinationRepository(owner)
    binding = bindings.read(project)
    if (binding.get('availability') != 'available' or binding.get('conversationId') != grant['coordinatorId']
            or (grant.get('bindingRevision') is not None and binding.get('revision') != grant['bindingRevision'])):
        return _retire(task, owner, 'the project\u2019s Coordinator binding changed since it was scheduled.',
                       scheduler)
    creator = grant['creator']
    if creator['role'] == 'worker':
        from lib.project_dispatch.repository import ProjectDispatchError
        if assignments is None:
            from lib.project_dispatch.repository import ProjectDispatchRepository
            assignments = ProjectDispatchRepository(owner, project)
        try:
            row = assignments.read(creator['assignmentId'])
        except ProjectDispatchError as error:
            from lib.project_dispatch.repository import transient_dispatch_failure
            if transient_dispatch_failure(error):
                return False, f'The creating Worker\u2019s grant could not be read ({error}); nothing delivered.'
            return _retire(task, owner, f'the creating Worker\u2019s assignment is gone ({error}).', scheduler)
        if row['grant'].get('revoked'):
            return _retire(task, owner, 'the creating Worker\u2019s grant was revoked.', scheduler)
    from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    command_id = 'project-wake:' + hashlib.sha256(json.dumps(
        [owner, str(task['id']), _occurrence(task)], separators=(',', ':')).encode()).hexdigest()
    message = stamp_initiator({'role': 'user', 'content': render_wake_notice(task, grant)}, INITIATOR_PROACTIVE)
    disposition = dispatch(grant['coordinatorId'], message, {}, user_id=owner, command_id=command_id,
                           log_prefix='[ProjectWake]', inject_mode='steer').disposition
    if disposition in _DELIVERED:
        return True, f'Woke {grant["coordinatorId"]} ({disposition}).'
    if disposition == 'target_missing':
        return _retire(task, owner, 'the Coordinator conversation no longer exists.', scheduler)
    return False, f'The Coordinator was not woken ({disposition}); nothing was delivered.'


__all__ = ['render_wake_notice', 'run_project_wake']
