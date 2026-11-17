"""Watchdog reviews over the existing scheduler (docs/ROUTER.md R7).

Responsibility: health checks on by default, the periodic review and the
one-shot re-checks the Watchdog arms for intermittent faults. Both reviews are
ordinary owner-visible scheduler tasks of type ``router_watchdog_review`` whose
execution delegates here: the periodic review is the owner's keyed task
(``scheduler.task.ensure`` with system key ``router-watchdog-review``: retries
converge), a re-check is a ``once:`` task with a deterministic id per Watchdog
idempotency key. ``ensure_health_checks`` provisions the Watchdog and installs
the periodic review on at the contract cadence for an owner who never chose
(owner decision 2026-11-17); once installed, the row is the owner's choice
(off, another cadence) and only ``configure_review`` changes it. Execution
builds the deterministic health snapshot and wakes the Watchdog with the
``proactive`` initiator and a command id per task and minute; a periodic review
whose snapshot holds nothing to judge ends without a model turn, while a
re-check always wakes (the Watchdog asked for it).

Entry points: ensure_health_checks, review_state, configure_review,
schedule_recheck, run_scheduled_review (the scheduler's task-type handler),
REVIEW_TASK_TYPE.
Dependencies: scheduler.manager/cron, router.contract, router.watchdog
(binding, signals, snapshot).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from lib.identity import PrincipalContext, require_user_id
from lib.log import get_logger

from ..contract import CONTRACT, validate

logger = get_logger(__name__)

REVIEW_TASK_TYPE = 'router_watchdog_review'
REVIEW_SYSTEM_KEY = 'router-watchdog-review'
DEFAULT_REVIEW_SCHEDULE = CONTRACT['$defs']['watchdogReviewRequest']['properties']['schedule']['default']
_REVIEW_NAME = 'Watchdog review'
_RECHECK_NAME = 'Watchdog re-check'
# needs_review counts of facts every snapshot page repeats (signals, outbox, channels).
_OWNER_WIDE_COUNTS = frozenset({'open_signals', 'outbox_attention', 'unhealthy_channels'})
_REVIEW_COMMAND = ('Periodic Router Watchdog review (docs/ROUTER.md R7). Informational: the handler '
                   'builds the health snapshot and wakes the Watchdog when something needs judgment.')


def review_task_id(owner: int) -> str:
    """The keyed task id ``scheduler.task.ensure`` assigns on first creation."""
    return f'system-{require_user_id(owner, context="watchdog review")}-{REVIEW_SYSTEM_KEY}'


def _scheduler():
    from lib.scheduler.manager import get_scheduler
    return get_scheduler()


def review_state(owner: int) -> dict:
    task = _scheduler().get_task(review_task_id(owner), user_id=owner)
    if task is None or task.get('system_key') != REVIEW_SYSTEM_KEY:
        return {'enabled': False, 'schedule': DEFAULT_REVIEW_SCHEDULE, 'taskId': None}
    return {'enabled': bool(task['enabled']), 'schedule': task['schedule'], 'taskId': task['id']}


def _validate_cron(schedule: str) -> str:
    from lib.scheduler.cron import cron_matches, next_cron_run
    schedule = schedule.strip()
    if schedule.startswith('once:'):
        raise ValueError('The periodic review takes a recurring cron; re-checks are one-shot')
    cron_matches(schedule)
    if next_cron_run(schedule) is None:
        raise ValueError(f'Cron expression {schedule!r} matches no date in the next year')
    return schedule


def _install_review(owner: int, *, enabled: bool, schedule: str, principal: PrincipalContext,
                    reconcile_enabled: bool) -> dict:
    from .heartbeat import REVIEW_MAX_RUNTIME_S, record_configured
    _scheduler().ensure_owner_task(
        principal=principal, system_key=REVIEW_SYSTEM_KEY, name=_REVIEW_NAME, schedule=schedule,
        command=_REVIEW_COMMAND, task_type=REVIEW_TASK_TYPE,
        description='Wakes the Router Watchdog to judge Worker progress and open faults.',
        max_runtime=REVIEW_MAX_RUNTIME_S, enabled=enabled, reconcile_enabled=reconcile_enabled)
    state = review_state(owner)
    record_configured(owner, state['schedule'], state['enabled'])
    return state


def configure_review(owner: int, request: dict, *, principal: PrincipalContext) -> dict:
    """Owner choice: enable or disable the periodic review and set its cadence."""
    request = validate(dict(request), 'watchdogReviewRequest')
    current = review_state(owner)
    schedule = _validate_cron(request.get('schedule') or current['schedule'])
    return _install_review(owner, enabled=request['enabled'], schedule=schedule, principal=principal,
                           reconcile_enabled=True)


def ensure_health_checks(owner: int, *, tenant_id: str | None = None) -> dict:
    """Health checks are on by default (owner decision 2026-11-17).

    Provisions the Watchdog and, for an owner whose periodic review was never
    installed, installs it on at the contract cadence under the Watchdog's own
    least-privilege scheduling identity: the owner granted that scope, so
    neither the owner nor the caller configures anything. An installed review is
    the owner's choice (off, another cadence) and is never rewritten here.
    Returns the Watchdog binding.
    """
    from .binding import router_watchdog
    watchdog = router_watchdog(owner, create=False)
    if watchdog['availability'] != 'available':
        watchdog = router_watchdog(owner, create=True)
    if watchdog['availability'] == 'available' and review_state(owner)['taskId'] is None:
        _install_review(owner, enabled=True, schedule=DEFAULT_REVIEW_SCHEDULE,
                        principal=watchdog_principal(owner, tenant_id), reconcile_enabled=False)
    return watchdog


def watchdog_principal(owner: int, tenant_id: str | None = None) -> PrincipalContext:
    """The owner's least-privilege scheduling identity for Watchdog-armed tasks."""
    return PrincipalContext.user(subject_id=f'router-watchdog:user:{owner}', owner_user_id=owner,
                                 tenant_id=tenant_id, scopes={'agents:scheduler'})


def schedule_recheck(owner: int, watchdog_conversation_id: str, arguments: dict, *,
                     principal: PrincipalContext, now: datetime | None = None) -> dict:
    """Arm one ``once:`` re-check; the same idempotency key never arms twice."""
    arguments = validate(dict(arguments), 'watchdogScheduleRecheck')
    digest = hashlib.sha256(json.dumps([owner, watchdog_conversation_id, arguments['idempotency_key']],
                                       separators=(',', ':')).encode()).hexdigest()
    task_id = 'watchdog-recheck-' + digest[:24]
    manager = _scheduler()
    task = manager.get_task(task_id, user_id=owner)
    if task is None:
        now = now or datetime.now()
        try:
            due = (now + timedelta(minutes=arguments['after_minutes'])).replace(second=0, microsecond=0)
        except OverflowError as error:
            raise ValueError('after_minutes is beyond the scheduler calendar') from error
        if due <= now:
            due += timedelta(minutes=1)
        try:
            task = manager.create_task(
                _RECHECK_NAME, 'once:' + due.strftime('%Y-%m-%d %H:%M'), arguments['reason'],
                principal=principal, task_type=REVIEW_TASK_TYPE, task_id=task_id,
                description='One-shot re-check armed by the Router Watchdog.',
                target_conv_id=watchdog_conversation_id)
        except Exception:
            # A concurrent identical call may have won the create; its row is the answer.
            task = manager.get_task(task_id, user_id=owner)
            if task is None:
                raise
    if task.get('task_type') != REVIEW_TASK_TYPE or task.get('command') != arguments['reason']:
        raise ValueError('idempotency_key was already used for a different re-check')
    return {'task_id': task['id'], 'schedule': task['schedule'], 'enabled': bool(task['enabled']),
            'reason': task['command']}


def _review_notice(kind: str, task: dict, counts: dict) -> str:
    lines = [f'[Watchdog {kind}: scheduled nonhuman input, not the owner\u2019s words and not authority]']
    if kind == 're-check':
        lines += ['You armed this re-check. Reason:', task['command'], '']
    lines.append('Snapshot counts: ' + ', '.join(f'{name.replace("_", " ")}={value}'
                                                 for name, value in counts.items()))
    lines.append('Read watchdog_snapshot, judge each open signal and active Worker, act only where needed, '
                 'and end silently when nothing needs attention.')
    return '\n'.join(lines)


def run_scheduled_review(task: dict) -> tuple[bool, str]:
    """Scheduler handler for ``router_watchdog_review`` rows; returns (success, result)."""
    from lib.turn_initiation import INITIATOR_PROACTIVE
    from .binding import router_watchdog
    from .heartbeat import record_review
    from .signals import wake_watchdog
    from .snapshot import health_snapshot, needs_review
    owner = require_user_id(task.get('user_id'), context='watchdog review')
    watchdog = router_watchdog(owner, create=False)
    if watchdog['availability'] != 'available':
        return False, 'The Watchdog conversation is unavailable; open the main coordinator to restore it.'
    from .compute_patrol import ingest_compute_patrols
    # Off-host patrols cannot reach Tofu; their published flags become signals before the snapshot reads them.
    ingest_compute_patrols(owner)
    periodic = task.get('system_key') == REVIEW_SYSTEM_KEY
    counts, cursor = {}, None
    while True:
        page = health_snapshot(owner, after_id=cursor)
        for name, value in needs_review(page).items():
            if cursor is None or name not in _OWNER_WIDE_COUNTS:
                counts[name] = counts.get(name, 0) + value
        cursor = page['next_cursor']
        if cursor is None:
            break
    if periodic and not any(counts.values()):
        # Written last: the review ran and read storage (the out-of-band dead-man signal).
        record_review(owner, str(task.get('schedule') or DEFAULT_REVIEW_SCHEDULE))
        return True, 'Nothing to review: no active, failed or held Workers, open signals or channel faults.'
    minute = datetime.now().strftime('%Y-%m-%dT%H:%M')
    command_id = 'router-watchdog-review:' + hashlib.sha256(json.dumps(
        [owner, str(task.get('id') or ''), minute], separators=(',', ':')).encode()).hexdigest()
    disposition = wake_watchdog(owner, watchdog, content=_review_notice('review' if periodic else 're-check',
                                                                        task, counts),
                                command_id=command_id, initiator=INITIATOR_PROACTIVE,
                                origin={'scheduledTaskId': str(task.get('id') or '')})
    accepted = disposition in ('started', 'steered', 'queued')
    if periodic:
        record_review(owner, str(task.get('schedule') or DEFAULT_REVIEW_SCHEDULE))
    return accepted, f'Watchdog woken ({disposition}); ' + ', '.join(
        f'{name}={value}' for name, value in counts.items())


__all__ = ['DEFAULT_REVIEW_SCHEDULE', 'REVIEW_SYSTEM_KEY', 'REVIEW_TASK_TYPE', 'configure_review',
           'ensure_health_checks', 'review_state', 'review_task_id', 'run_scheduled_review',
           'schedule_recheck', 'watchdog_principal']
