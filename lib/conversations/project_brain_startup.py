"""Fail-closed Project Brain cutover and active-work restart reconciliation."""

from __future__ import annotations

import os
import time

from lib.log import get_logger
from lib.storage import get_storage_client
from lib.storage.port import query_many
from runtime_guards import storage_backup_timeout_seconds


logger = get_logger(__name__)


def ensure_project_brain_cutover() -> dict:
    """Back up the authority, migrate once, and verify the cutover receipt."""
    client = get_storage_client(write=True)
    status = client.query('project_brain.cutover.status', {})
    if status.get('complete'):
        return {'complete': True, 'alreadyComplete': True}
    health = client.health(deadline=5.0)
    backend = str(health.get('backend') or '')
    if backend == 'sqlite':
        backup = client.maintenance(
            'system.backup',
            deadline=float(storage_backup_timeout_seconds()),
        )
        if not isinstance(backup, dict) or not backup.get('ok', True):
            raise RuntimeError('Project Brain pre-cutover backup failed')
    elif backend == 'postgres':
        # External PostgreSQL backup is deliberately not faked inside the app.
        # Operators provide the receipt only after the platform snapshot exists.
        if os.environ.get(
                'TOFU_PROJECT_BRAIN_CUTOVER_BACKUP_CONFIRMED', '').strip() != '1':
            raise RuntimeError(
                'Set TOFU_PROJECT_BRAIN_CUTOVER_BACKUP_CONFIRMED=1 only after '
                'the platform PostgreSQL backup has completed')
    else:
        raise RuntimeError(
            f'Unsupported Project Brain cutover backup backend: {backend}')
    result = client.command(
        'project_brain.cutover', {}, 'project-brain-cutover-v1',
        deadline=120.0)
    verified = client.query('project_brain.cutover.status', {})
    if not result.get('verified') or not verified.get('complete'):
        raise RuntimeError('Project Brain cutover verification failed')
    return {'complete': True, **result}


_REPAIR_NAMESPACE = 'project_brain.repairs.v1'
_RESTART_CANCEL_REPAIR = 'restart-drain-cancelled-to-interrupted'


def _replayed_task(task) -> dict:
    """A task result as ``workspace_outcome`` reads it.

    replay_get stores terminal evidence in metadata, unlike the in-memory task
    carrier. Copy only verdict fields; never infer a finish reason from a
    nominal terminal status.
    """
    task = dict(task or {})
    metadata = task.get('metadata')
    if isinstance(metadata, dict):
        for key in ('finishReason', 'streamState', 'aborted', '_abort_reason'):
            if key not in task and key in metadata:
                task[key] = metadata[key]
    return task


def repair_restart_cancelled_work_items() -> int:
    """Relabel work a restart drain recorded ``cancelled`` as ``interrupted``.

    Before ``settle_work_item`` read the restart-aware ``workspace_outcome``, a
    Worker drained by ``server_shutdown`` finished its work item ``cancelled``.
    Each work item belongs to one physical task, whose stored result keeps the
    drain's ``_abort_reason``; only that evidence relabels an item, so a person's
    Stop stays cancelled. One complete walk records a receipt and later boots
    skip it; each relabel is an idempotent command, so an interrupted walk
    simply runs again.
    """
    from lib.tasks_pkg.workspace_delivery import RESTART_INTERRUPTED, workspace_outcome

    client = get_storage_client(write=True)
    if client.query('record.get', {'namespace': _REPAIR_NAMESPACE,
                                   'key': _RESTART_CANCEL_REPAIR}):
        return 0
    repaired = 0
    cursor = None
    while True:
        request = {'include_cancelled': True}
        if cursor is not None:
            request['cursor'] = cursor
        snapshot = client.maintenance(
            'project_brain.recovery.snapshot', request, deadline=30.0)
        if not snapshot.get('includesCancelled'):
            # An authority that predates the request returns active work
            # only; recording the receipt would skip the repair for good.
            logger.warning('[ProjectBrain] storage cannot list cancelled work; '
                           'restart-cancel repair waits for a newer authority')
            return repaired
        candidates = [
            (int(project.get('ownerUserId') or 0), str(project.get('projectKey') or ''), work)
            for project in snapshot.get('projects') or ()
            for work in project.get('cancelledWorkItems') or ()
        ]
        answers = query_many(client, [('task_results.replay_get', {
            'key': str(work.get('taskId') or ''), 'user_id': owner_user_id,
            'include_terminal_payload': False, 'include_metadata': True,
        }) for owner_user_id, _project_key, work in candidates])
        for (owner_user_id, project_key, work), task in zip(candidates, answers):
            if isinstance(task, BaseException):
                raise task
            if not task or workspace_outcome(_replayed_task(task)) != RESTART_INTERRUPTED:
                continue
            work_id = str(work.get('id') or '')
            result = client.command(
                'project_brain.work.finish', {
                    'owner_user_id': owner_user_id,
                    'project_key': project_key,
                    'work_id': work_id,
                    'status': RESTART_INTERRUPTED,
                    'result_summary': str(work.get('resultSummary') or ''),
                },
                f'project-work-restart-repair:{work_id}',
            )
            if (result or {}).get('event'):
                repaired += 1
        if not snapshot.get('capped'):
            break
        next_cursor = snapshot.get('nextCursor')
        if not isinstance(next_cursor, dict) or not next_cursor or next_cursor == cursor:
            raise RuntimeError(
                'Project Brain capped recovery page has no advancing cursor')
        cursor = next_cursor
    client.command('record.put', {
        'namespace': _REPAIR_NAMESPACE, 'key': _RESTART_CANCEL_REPAIR,
        'expected_version': 0,
        'value': {'version': 1, 'repaired': repaired, 'completedAt': time.time()},
    }, f'project-brain-repair:{_RESTART_CANCEL_REPAIR}')
    if repaired:
        logger.warning('[ProjectBrain] relabelled %d restart-drained work item(s) '
                       'cancelled -> interrupted', repaired)
    return repaired


def recover_active_work_items() -> int:
    """Finish orphaned work only after its workspace intent is acknowledged."""
    from lib.tasks_pkg.workspace_delivery import (
        RESTART_INTERRUPTED, record_task_workspace_intents, work_result_summary,
        workspace_outcome,
    )

    try:
        repair_restart_cancelled_work_items()
    except Exception:
        # The receipt is written only after a complete walk: the next boot
        # retries. Active work recovery below must not wait for history.
        logger.warning('[ProjectBrain] restart-cancel repair deferred', exc_info=True)
    client = get_storage_client(write=True)
    recovered = 0
    # One boot pass resolves each project checkout's identity once.
    checkouts: dict = {}
    cursor = None
    page_number = 0
    while True:
        page_number += 1
        started = time.monotonic()
        snapshot = client.maintenance(
            'project_brain.recovery.snapshot',
            {'cursor': cursor} if cursor is not None else {}, deadline=30.0)
        logger.info(
            '[ProjectBrain] recovery snapshot page=%d elapsed=%.3fs projects=%d more=%s',
            page_number, time.monotonic() - started,
            len(snapshot.get('projects') or ()), bool(snapshot.get('capped')))
        next_cursor = snapshot.get('nextCursor')
        if snapshot.get('capped') and (
                not isinstance(next_cursor, dict) or not next_cursor
                or next_cursor == cursor):
            raise RuntimeError(
                'Project Brain capped recovery page has no advancing cursor')
        # Legacy index rows may repeat a project within this bounded page.
        # Acknowledge each owner/project/work identity once; failed intents
        # remain eligible, and the next page still reads fresh authority state.
        active_work: dict[tuple[int, str, str], str] = {}
        for project in snapshot.get('projects') or ():
            owner_user_id = int(project.get('ownerUserId') or 0)
            project_key = str(project.get('projectKey') or '')
            if not owner_user_id or not project_key:
                raise RuntimeError('Malformed Project Brain recovery scope')
            for work in project.get('workItems') or ():
                # Older native snapshots included terminal history. It is not
                # orphaned work: do not replay tasks or issue no-op writes for
                # it on every boot, even while a pinned binary is upgraded.
                if work.get('status') != 'active':
                    continue
                identity = (owner_user_id, project_key, str(work.get('id') or ''))
                active_work.setdefault(identity, str(work.get('taskId') or ''))
        # The page's task results are independent reads: one pipelined exchange.
        answers = query_many(client, [('task_results.replay_get', {
            'key': task_id, 'user_id': owner_user_id,
            'include_terminal_payload': False, 'include_metadata': True,
        }) for (owner_user_id, _project_key, _work_id), task_id in active_work.items()])
        for ((owner_user_id, project_key, work_id), task_id), task in zip(
                active_work.items(), answers):
            if isinstance(task, BaseException):
                raise task
            task = _replayed_task(task)
            status = workspace_outcome(task)
            if status == 'cancelled':
                summary = 'Execution was cancelled before server restart recovery.'
            elif status == 'completed':
                summary = 'Execution completed before server restart recovery.'
            else:
                error = (task or {}).get('error')
                summary = str(error or 'Execution lacked verified completion before server restart.')
            # Recover only this work item's existing registration. Missing
            # execution evidence never licenses adoption of sibling trees.
            recovery_task = dict(task or {}, id=task_id, _userId=owner_user_id)
            recovery_task['config'] = dict(
                (task or {}).get('config') or {}, projectPath=project_key)
            try:
                receipt = record_task_workspace_intents(
                    recovery_task, project_key, checkouts=checkouts)
                if receipt and (receipt.get('errors') or receipt.get('status') == 'error'):
                    raise RuntimeError('Workspace terminal intent remains unacknowledged')
            except Exception as exc:
                # The active work item is the durable retry eligibility.
                # Finishing it here would remove it from the next snapshot.
                logger.warning(
                    '[ProjectBrain] recovery intent pending work=%s task=%s: %s',
                    work_id, task_id, exc)
                continue
            if status == RESTART_INTERRUPTED:
                # The restart paused this task: its non-denied workspace receipt
                # is recorded above, and its resumed attempt keeps the same task
                # identity and settles the work item. Finishing it here closed
                # resumed lanes' work as 'cancelled' (2026-11-06, 18 items).
                logger.info('[ProjectBrain] work=%s task=%s paused by the restart; '
                            'left active for its resumed attempt', work_id, task_id)
                continue
            delivery_summary = work_result_summary(recovery_task)
            if delivery_summary:
                summary = summary[:3000] + '\n' + delivery_summary
            client.command(
                'project_brain.work.finish', {
                    'owner_user_id': owner_user_id,
                    'project_key': project_key,
                    'work_id': work_id,
                    'status': status,
                    'result_summary': summary[:4000],
                },
                f'project-work-recovery:{work_id}',
            )
            recovered += 1
        if not snapshot.get('capped'):
            break
        cursor = next_cursor
    from lib.project_dispatch.recovery import recover_persisted_dispatch
    recover_persisted_dispatch(client=client)
    from lib.conversations.coordinator_reports import recover_pending_reports
    recover_pending_reports(client=client)
    if recovered:
        logger.warning(
            '[ProjectBrain] terminally reconciled %d orphaned work item(s)',
            recovered)
    return recovered


__all__ = ['ensure_project_brain_cutover', 'recover_active_work_items',
           'repair_restart_cancelled_work_items']
