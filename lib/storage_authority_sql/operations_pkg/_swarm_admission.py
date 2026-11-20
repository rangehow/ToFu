"""Linearize a live Swarm handoff with durable Turn Stop and exact teardown."""
from lib.storage.errors import StorageError
from lib.swarm.persistence_contract import SWARM_SESSION_STATUS_RUNNING

from ._common import _dump, _integer, _load, _required_text
from ._swarm_teardown import (
    _identity, _matches, _session_header, lifecycle_row, lock_run,
    parent_identity, session_identity, update_lifecycle,
)
from lib.conversations.project_identity import normalize_project_path
from lib.swarm.restart_recovery import _RESTART_REASONS


def admit(session, payload):
    identity = _identity(payload)
    caller = _required_text(payload, 'caller_task_id', 4096)
    stage = payload.get('stage')
    if stage not in ('restore', 'enqueue', 'worker_start') or (
            stage == 'restore' and payload.get('continue_binding') is not True):
        raise StorageError('database_protocol_error', 'Invalid Swarm admission stage')
    turn_id, attempt_id = payload.get('turn_id'), payload.get('attempt_id')
    owner, conv = identity['owner_id'], identity['conversation_id']
    continuing = payload.get('continue_binding') is True
    if continuing:
        parent = _caller_parent(payload)
        _continue_authority(session, identity, parent, allow_completed=stage == 'worker_start')
    elif turn_id is not None or attempt_id is not None or caller.startswith('conversation-attempt:'):
        turn_id = _required_text(payload, 'turn_id', 128)
        attempt_id = _required_text(payload, 'attempt_id', 128)
        # Match terminal Turn event ordering. Conversation ownership/current
        # attempt can also change through Turn commands, not just events.
        session.lock_key('turn_conversation', f'{owner}:{conv}')
        session.lock_key('attempt_events', attempt_id)
        row = session.fetch_one(
            'SELECT t.status AS turn_status, a.status AS attempt_status '
            'FROM storage_conversation_turns t JOIN storage_generation_attempts a '
            'ON a.attempt_id=t.current_attempt_id AND a.turn_id=t.turn_id '
            'AND a.conversation_id=t.conversation_id '
            'WHERE t.user_id=? AND t.conversation_id=? AND t.turn_id=? '
            'AND a.attempt_id=? AND a.task_id=?', (owner, conv, turn_id, attempt_id, caller))
        allowed = ('pending', 'running', 'completed') if stage == 'worker_start' else ('pending', 'running')
        if row is None or row['turn_status'] not in allowed or row['attempt_status'] not in allowed:
            raise StorageError('database_conflict', 'Swarm caller is not the current accepted attempt')

    header = _session_header(session, identity['swarm_key'])
    lock_run(session, identity['run_id'])
    receipt = lifecycle_row(session, identity['run_id'], owner=owner)
    if (not _matches(header, identity) or header['status'] != SWARM_SESSION_STATUS_RUNNING
            or receipt is None or receipt['identity'] != identity or receipt['disposition'] is not None):
        raise StorageError('database_conflict', 'Swarm run is not current and runnable')
    epoch = _integer(payload, 'checkpoint_writer_epoch', minimum=0, default=0)
    if epoch != receipt.get('checkpoint_writer_epoch', 0):
        raise StorageError('database_conflict', 'Swarm handoff writer epoch changed')
    if receipt.get('continue_parent') and (not turn_id or not attempt_id):
        raise StorageError('database_conflict', 'Rebound Swarm run requires a current accepted caller')
    if continuing and (receipt.get('continue_parent') != parent
                       or parent_identity(_load(header['config_json'])) != parent):
        raise StorageError('database_conflict', 'Swarm Continue binding is no longer current')
    if receipt.get('activation_failed'):
        raise StorageError('database_conflict', 'Swarm Continue activation already failed')
    if continuing and stage == 'restore':
        if receipt.get('restore_claimed'):
            raise StorageError('database_conflict', 'Swarm checkpoint restore was already admitted')
        update_lifecycle(session, {**receipt, 'restore_claimed': True})
    elif continuing and not receipt.get('restore_claimed'):
        raise StorageError('database_conflict', 'Swarm checkpoint restore has not been admitted')
    if receipt.get('continue_parent') and stage == 'worker_start' and not receipt.get('worker_admitted'):
        update_lifecycle(session, {**receipt, 'worker_admitted': True})
    # The command receipt commits under both authorities. This is the handoff
    # decision, not evidence of physical submission, worker exit or replay authority.
    return {'admitted': True}


def _continue_authority(session, identity, parent, *, allow_completed=False):
    owner, conv = identity['owner_id'], identity['conversation_id']
    turn, attempt, task = (parent[name] for name in ('turn_id', 'attempt_id', 'task_id'))
    session.lock_key('conversation', f'{owner}:{conv}')
    session.lock_key('turn_conversation', f'{owner}:{conv}')
    session.lock_key('turn_attempt', turn)
    session.lock_key('attempt_events', attempt)
    session.lock_key('worker_job', task)
    row = session.fetch_one(
        'SELECT t.status AS turn_status, a.status, a.operation, a.config_json, t.ordinal '
        'FROM storage_conversation_turns t JOIN storage_generation_attempts a '
        'ON a.attempt_id=t.current_attempt_id AND a.conversation_id=t.conversation_id '
        'AND a.turn_id=t.turn_id JOIN storage_conversations c '
        'ON c.id=t.conversation_id AND c.user_id=t.user_id '
        "WHERE t.user_id=? AND t.conversation_id=? AND t.turn_id=? AND t.lane_id='main' "
        'AND a.attempt_id=? AND a.task_id=?', (owner, conv, turn, attempt, task))
    allowed = ('pending', 'running', 'completed') if allow_completed else ('pending', 'running')
    if (row is None or row['turn_status'] not in allowed or row['status'] not in allowed
            or row['operation'] not in ('continue', 'checkpoint_resume')):
        raise StorageError('database_conflict', 'Swarm Continue is not the current accepted attempt')
    job = session.fetch_one('SELECT tenant_id, cancel_requested_at_ms, status FROM storage_worker_jobs '
                            'WHERE user_id=? AND task_id=?', (owner, task))
    if ((task.startswith('conversation-attempt:') and job is None) or (job is not None and (
            job['tenant_id'] != identity['tenant_id'] or job['cancel_requested_at_ms']
            or job['status'] in ('completed', 'failed', 'cancelled')))):
        raise StorageError('database_conflict', 'Swarm Continue worker job is cancelled or unavailable')
    config = _load(row['config_json'])
    if (config.get('_turnPrincipalTenantId', '') != identity['tenant_id']
            or normalize_project_path(config.get('canonicalProjectPath') or config.get('projectPath') or '')
            != identity['project_path']):
        raise StorageError('database_conflict', 'Swarm Continue scope changed')
    if row['status'] != 'completed' and session.fetch_one(
            'SELECT 1 AS present FROM storage_conversation_turns '
            "WHERE user_id=? AND conversation_id=? AND lane_id='main' AND ordinal>? LIMIT 1",
            (owner, conv, row['ordinal'])):
        raise StorageError('database_conflict', 'Swarm Continue Turn is not the current main head')
    return config.get('_resumeSource')


def _caller_parent(payload):
    return dict(turn_id=_required_text(payload, 'turn_id', 128),
                attempt_id=_required_text(payload, 'attempt_id', 128),
                task_id=_required_text(payload, 'caller_task_id', 4096))


def rebind_continue(session, payload):
    """Bind a settled predecessor's frozen run without touching any child evidence."""
    identity = _identity(payload)
    parent = _caller_parent(payload)
    source = _continue_authority(session, identity, parent)
    header = _session_header(session, identity['swarm_key'])
    lock_run(session, identity['run_id'])
    receipt = lifecycle_row(session, identity['run_id'], owner=identity['owner_id'])
    if (not _matches(header, identity) or receipt is None or receipt['identity'] != identity
            or receipt['disposition'] is not None or receipt['roster'] != _load(header['specs_json'])):
        raise StorageError('database_conflict', 'Swarm Continue requires an undisposed exact run')
    config = _load(header['config_json'])
    if config.get('native_execution_required') is not None and config['native_execution_required'] is not False:
        raise StorageError('database_conflict', 'Native actor recovery is unsupported by SQL Swarm Continue')
    bound = parent_identity(config)
    replacement = payload.get('config')
    expected_revision = _integer(payload, 'expected_updated_at', minimum=0)
    failed = payload.get('activation_failed') is True
    if failed and receipt.get('activation_failed') == parent:
        return {'failed_activation_recorded': True}
    if bound == parent and receipt.get('continue_parent') == parent:
        if failed and payload.get('unbound_only') is not True and not receipt.get('worker_admitted'):
            update_lifecycle(session, {**receipt, 'activation_failed': parent})
            return {'failed_activation_recorded': True}
        if (failed or receipt.get('activation_failed') == parent or receipt.get('worker_admitted')
                or replacement != config or header['updated_at'] != expected_revision
                or header['status'] != SWARM_SESSION_STATUS_RUNNING):
            raise StorageError('database_conflict', 'Swarm Continue binding is no longer runnable')
        return {'rebound': True, 'checkpoint_writer_epoch': receipt['checkpoint_writer_epoch']}
    predecessor_binding = receipt.get('activation_failed') or bound
    if (bound != receipt.get('continue_parent', receipt['parent'])
            or parent['turn_id'] != bound['turn_id'] or parent['attempt_id'] == bound['attempt_id']
            or not isinstance(source, dict) or source.get('attemptId') != predecessor_binding['attempt_id']
            or source.get('taskId') != predecessor_binding['task_id']
            or (not failed and header['updated_at'] != expected_revision)):
        raise StorageError('database_conflict', 'Swarm Continue predecessor or frozen revision changed')
    # A marker does not consume the checkpoint cut. Its exact predecessor and
    # accepted caller are checked under lock even if a checkpoint writer advanced
    # the revision that caused recovery to stand down.
    predecessor = session.fetch_one(
        'SELECT status FROM storage_generation_attempts WHERE attempt_id=? '
        'AND conversation_id=? AND turn_id=? AND task_id=?',
        (predecessor_binding['attempt_id'], identity['conversation_id'],
         predecessor_binding['turn_id'], predecessor_binding['task_id']))
    status, cause = source.get('status'), source.get('cause')
    unentered = (receipt.get('activation_failed') == predecessor_binding or (
        receipt.get('continue_parent') == predecessor_binding and receipt.get('worker_admitted') is False))
    if (predecessor is None or predecessor['status'] != status or not (
            (not receipt.get('activation_failed') and status == 'interrupted' and cause in _RESTART_REASONS
             and header['status'] == SWARM_SESSION_STATUS_RUNNING)
            or (status in ('interrupted', 'cancelled') and cause == 'user_abort')
            or (status == 'failed' and bool(cause) and unentered))):
        raise StorageError('database_conflict', 'Swarm Continue predecessor is not resumable')
    if failed:
        update_lifecycle(session, {**receipt, 'activation_failed': parent})
        return {'failed_activation_recorded': True}
    if (not isinstance(replacement, dict) or parent_identity(replacement) != parent
            or session_identity(identity['swarm_key'], identity['run_id'], identity['conversation_id'],
                                replacement) != identity
            or replacement.get('native_execution_required', False) is not False):
        raise StorageError('database_conflict', 'Swarm Continue replacement config changed scope')
    epoch = receipt.get('checkpoint_writer_epoch', 0) + 1
    update_lifecycle(session, {**receipt, 'continue_parent': parent, 'checkpoint_writer_epoch': epoch,
        'continue_source': predecessor_binding, 'continue_source_failed': status == 'failed',
        'activation_failed': None, 'restore_claimed': False, 'worker_admitted': False})
    session.execute('UPDATE swarm_sessions SET config_json=?, status=?, updated_at=? WHERE swarm_key=?',
                    (_dump(replacement).decode(), SWARM_SESSION_STATUS_RUNNING,
                     header['updated_at'] + 1, identity['swarm_key']))
    return {'rebound': True, 'checkpoint_writer_epoch': epoch}
