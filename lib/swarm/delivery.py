"""Result acknowledgement at the shared verified-provider boundary.

Swarm checkpoints remain the durable outbox. In-memory inbox entries are wake
hints: failed acknowledgement retains pending receipts; finalization restores
unconsumed entries. Delivery is at least once after an uncertain ACK, never
claimed merely because a prompt was assembled. Existing result IDs deduplicate
repeated completion callbacks and acknowledgements.
"""
from __future__ import annotations


def _writer_epoch(session) -> int:
    # Read only a captured instance field, not a delegated/dynamic attribute.
    return vars(session).get('_checkpoint_writer_epoch', 0)


def _matches_writer(item: dict, session) -> bool:
    return ((not item.get('swarm_run_id') or item['swarm_run_id'] == session.task_id)
            and item.get('checkpoint_writer_epoch', 0) == _writer_epoch(session))


def acknowledge_presented_results(task: dict) -> bool:
    pending = task.get('_swarm_inject_pending') or []
    if not pending:
        return True
    from lib.swarm.integration._config import swarm_key_for
    from lib.swarm import persistence
    key = swarm_key_for(task)
    from lib.swarm.integration import get_active_session
    session = get_active_session(key)
    if session is not None and session.user_id != task.get('_userId'):
        raise ValueError('result acknowledgement belongs to another owner')
    if session is not None:
        # Confirmed consumption may retire an older session's local receipt,
        # but it must never mark that ID delivered in the replacement session.
        pending = [item for item in pending if _matches_writer(item, session)]
        if not pending:
            task.pop('_swarm_inject_pending', None)
            return True
        task['_swarm_inject_pending'] = pending
    elif any(item.get('swarm_run_id') for item in pending):
        return False  # No current identity witness: retain for reconciliation.
    ids = list(dict.fromkeys(str(item['agent_id']) for item in pending if item.get('agent_id')))
    if ids and not (task.get('_transientRuntime') or task.get('_invocationStorageFree')):
        epoch = pending[0].get('checkpoint_writer_epoch', 0)
        if session is None or not persistence.mark_delivered(key, ids, task_id=session.task_id,
                **({'checkpoint_writer_epoch': epoch} if epoch else {})):
            return False
    if session is not None:
        session.mark_results_delivered(ids)
    task.pop('_swarm_inject_pending', None)
    return True


def restore_unconsumed_results(task: dict) -> None:
    pending = task.get('_swarm_inject_pending') or []
    if not pending:
        return
    from lib import agent_inbox
    from lib.swarm.integration._config import swarm_key_for
    key = swarm_key_for(task)
    from lib.swarm.integration import get_active_session
    session = get_active_session(key)
    if session is not None and session.user_id != task.get('_userId'):
        raise ValueError('result restoration belongs to another owner')
    if session is not None:
        session.restore_unconsumed_results([item['agent_id'] for item in pending
            if item.get('agent_id') and _matches_writer(item, session)])
    restored = 0
    try:
        for item in tuple(pending):
            if session is not None and not _matches_writer(item, session):
                restored += 1  # A new writer owns its own durable recovery hints.
                continue
            agent_inbox.enqueue(key, item.get('value') or '', mode='swarm-update',
                                priority=item.get('priority') or 'later',
                                agent_id=item.get('agent_id') or '',
                                extra={'status': item.get('status') or '',
                                       'swarm_run_id': item.get('swarm_run_id') or '',
                                       'checkpoint_writer_epoch': item.get('checkpoint_writer_epoch', 0)})
            restored += 1
    finally:
        # Retire only the restored prefix, once: large batches stay linear.
        # Stop tombstones and duplicate hints need no resurrection.
        del pending[:restored]
        if task.get('_swarm_inject_pending') is pending and not pending:
            task.pop('_swarm_inject_pending', None)


def stage_tool_results(task: dict, session, payloads: list[dict]) -> None:
    """A tool return reserves live delivery; only provider consumption ACKs storage."""
    if session.user_id != task.get('_userId'):
        raise ValueError('result presentation belongs to another owner')
    from lib.agent_inbox import format_swarm_update
    pending = task.setdefault('_swarm_inject_pending', [])
    known = {item.get('agent_id') for item in pending if _matches_writer(item, session)}
    epoch = _writer_epoch(session)
    for row in payloads:
        identity = row['agent_id']
        if identity in known:
            continue
        known.add(identity)
        value = format_swarm_update(
            agent_id=row['agent_id'], swarm_run_id=session.task_id,
            role=row.get('role') or 'general',
            status=row.get('status') or 'completed', elapsed_seconds=row.get('elapsed') or 0,
            tokens=row.get('tokens') or 0, result=row.get('result') or row.get('final_answer') or '',
            output_file=row.get('output_file') or '', error=row.get('error') or '')
        pending.append({'agent_id': row['agent_id'], 'swarm_run_id': session.task_id,
                        'checkpoint_writer_epoch': epoch,
                        'status': row.get('status') or 'completed', 'value': value, 'priority': 'later'})


def current_result_hints(key, items):
    """Only the current writer's hints may drive prompt input or automatic wake."""
    if not items:
        return items
    from lib.swarm.integration import get_active_session
    session = get_active_session(key)
    if session is None:
        from lib.swarm import persistence
        header = persistence.load_session(key, include_agents=False) if any(
            item.get('swarm_run_id') for item in items) else None
        return [item for item in items if not item.get('swarm_run_id') or (
            header is not None and item['swarm_run_id'] == header.get('task_id')
            and item.get('checkpoint_writer_epoch', 0) == header.get('checkpoint_writer_epoch', 0))]
    identity = vars(session).get('_sql_run_identity')
    if identity is not None:
        from lib.swarm import persistence
        row = persistence.load_session(key, identity=identity, include_agents=False)
        if row is None or row.get('checkpoint_writer_epoch', 0) != _writer_epoch(session):
            return []
    return [item for item in items if _matches_writer(item, session)]
