"""SQL Swarm runtime adapter: exact durable evidence before scheduler admission."""
from lib.storage.errors import StorageError
from lib.swarm import persistence


def require_current_attempt(task, *, owner, allow_completion=False):
    """Check the existing Turn owner, not the tool receipt or task registry alone."""
    config = task.get('config') or {}
    attempt, turn_id = config.get('_attemptId'), config.get('_turnId')
    task_id = task['id']
    if task.get('aborted'):
        raise StorageError('database_conflict', 'Swarm caller stopped')
    if not attempt and not turn_id and not task_id.startswith('conversation-attempt:'):
        return  # Legacy callers that have no Turn/Attempt binding.
    from lib.turn_lifecycle import get_attempt, get_turn
    conv = task.get('convId')
    states = ('pending', 'running', 'completed') if allow_completion else ('pending', 'running')
    if attempt and turn_id and conv:
        accepted = get_attempt(attempt, user_id=owner)
        current = get_turn(conv, turn_id, user_id=owner, include_projection=False)
        if (accepted.get('attemptId') == attempt and accepted.get('taskId') == task_id
                and accepted.get('turnId') == turn_id and accepted.get('conversationId') == conv
                and accepted.get('status') in states and current.get('status') in states
                and current.get('currentAttemptId') == attempt and not task.get('aborted')):
            return
    raise StorageError('database_conflict', 'Swarm caller is not the current accepted attempt')


def matches_scope(identity, task, project_path, *, owner):
    from lib.conversations.project_identity import normalize_project_path
    config = task.get('config') or {}
    tenant = task.get('_tenant_id', config.get('_tenant_id'))
    return (identity is not None and identity['owner_id'] == owner
            and identity['tenant_id'] == (tenant or '')
            and identity['conversation_id'] == (task.get('convId') or '')
            and identity['project_path'] == normalize_project_path(
                config.get('canonicalProjectPath') or config.get('projectPath') or project_path))


def require_current_run(identity, *, managed=True, checkpoint_writer_epoch=None):
    """Read current scope; a successful command replay alone is never admission."""
    row = persistence.load_session(identity['swarm_key'], required=True, include_agents=False,
                                   identity=identity)
    receipt = persistence.read_lifecycle(identity)
    if (row is None or row.get('status') != 'running'
            or (receipt is None and managed)
            or (receipt is not None and (receipt.get('identity') != identity
                                         or receipt.get('disposition') is not None))
            or (checkpoint_writer_epoch is not None and
                (row.get('checkpoint_writer_epoch', 0) != checkpoint_writer_epoch or
                 (receipt or {}).get('checkpoint_writer_epoch', 0) != checkpoint_writer_epoch))):
        raise StorageError('database_conflict', 'Swarm run is not current and runnable')
    return row


def _admit_wave(session, task):
    """Bind this wave's caller once; the spawning task is not a later caller."""
    identity = session._sql_run_identity
    config = task.get('config') or {}
    caller = dict(caller_task_id=task['id'], turn_id=config.get('_turnId') or None,
                  attempt_id=config.get('_attemptId') or None,
                  checkpoint_writer_epoch=getattr(session, '_checkpoint_writer_epoch', 0))

    def admit(stage):
        if session._aborted or session.abort_check() or task.get('aborted'):
            raise StorageError('database_conflict', 'Swarm caller or run stopped')
        persistence.admit_handoff(identity, **caller, stage=stage)
        if session._aborted or session.abort_check() or task.get('aborted'):
            raise StorageError('database_conflict', 'Swarm caller or run stopped')

    def worker_start():
        try:
            admit('worker_start')
        except StorageError as exc:
            if exc.code != 'database_conflict':
                raise
            from lib.llm.llm_errors import AbortedError
            raise AbortedError(str(exc)) from exc

    admit('enqueue')
    return worker_start


def persist_launch(session, task, config, specs):
    from ._state import retire_previous_run_rows
    identity = session._sql_run_identity
    require_current_attempt(task, owner=session.user_id)
    if not retire_previous_run_rows(session.inbox_key, session.task_id, identity=identity):
        raise StorageError('database_conflict', 'Prior swarm run was not retired')
    if session._aborted or task.get('aborted'):
        raise StorageError('database_conflict', 'Swarm launch stopped before persistence')
    persistence.save_session(session.inbox_key, conv_id=session.conv_id,
        task_id=session.task_id, specs=[spec.to_dict() for spec in specs],
        config=config, required=True, save_mode='launch', task=task)
    return _admit_wave(session, task)


def persist_roster(session, task, specs):
    require_current_attempt(task, owner=session.user_id)
    row = require_current_run(session._sql_run_identity, managed=False)
    persistence.save_session(session.inbox_key, conv_id=session.conv_id,
        task_id=session.task_id, specs=[spec.to_dict() for spec in specs],
        config=row['config'], required=True, save_mode='roster', task=task,
        checkpoint_writer_epoch=getattr(session, '_checkpoint_writer_epoch', 0))
    return _admit_wave(session, task)


def continue_source_snapshot(snapshot, task):
    """Recover the same predecessor proof after an acknowledged-or-lost rebind."""
    if not isinstance(snapshot, persistence.SqlSessionSnapshot):
        return snapshot
    config = snapshot['config']
    parent = config.get('parent_cfg') or {}
    identity = persistence.run_identity(snapshot['swarm_key'], snapshot)
    receipt = persistence.read_lifecycle(identity) or {}
    if receipt.get('disposition') is not None:
        return snapshot
    attempt = (task.get('config') or {}).get('_attemptId')
    failed = receipt.get('activation_failed')
    if failed and failed['attempt_id'] == attempt:
        return {**snapshot, 'status': 'not_runnable'}
    if parent.get('_swarmParentAttemptId') == attempt and not failed:
        source, failed_source = receipt.get('continue_source'), receipt.get('continue_source_failed')
    elif failed or receipt.get('worker_admitted') is False:
        from lib.turn_lifecycle import get_attempt
        source = failed or receipt.get('continue_parent')
        if not source:
            return snapshot
        observed = get_attempt(source['attempt_id'], user_id=identity['owner_id'])
        failed_source = observed.get('status') == 'failed'
    else:
        return snapshot
    if not source:
        return snapshot
    parent = {**parent, '_swarmParentTurnId': source['turn_id'],
              '_swarmParentAttemptId': source['attempt_id'], '_swarmParentTaskId': source['task_id']}
    if failed_source:
        parent['_swarmRecoveryFailedAttempt'] = {'attemptId': source['attempt_id'], 'taskId': source['task_id']}
    return {**snapshot, 'config': {**config, 'parent_cfg': parent}}


def admit_continue(identity, task, stage, epoch):
    """A fresh SQL decision, distinct from an idempotent rebind response."""
    from lib.llm.llm_errors import AbortedError
    config = task['config']
    if task.get('aborted'):
        raise AbortedError('Swarm Continue stopped')
    persistence.admit_handoff(identity, caller_task_id=task['id'], turn_id=config['_turnId'],
        attempt_id=config['_attemptId'], stage=stage, continue_binding=True,
        checkpoint_writer_epoch=epoch)
    if task.get('aborted'):
        raise AbortedError('Swarm Continue stopped')
