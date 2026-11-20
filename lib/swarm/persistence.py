"""lib/swarm/persistence.py — Durable, DB-backed swarm session/agent state.

Why this exists
---------------
Before this module the entire swarm lived in process memory: the session
registry (``integration._active_sessions``), each ``SubAgent.messages``
array, and the model-facing inbox (``lib.agent_inbox._inboxes``). A server
restart wiped all of it, so an in-flight sub-agent died permanently and a
"continue" turn after a restart could not resume it — only the streamed
``.log`` transcript survived (good for reading a *finished* agent's text,
useless for *resuming* an unfinished one).

This module persists the **resumable** state through the versioned Storage
Sidecar.  Business code never opens either backend or sends SQL. Two logical
tables are owned by the Sidecar:

  * ``swarm_sessions`` — one row per conversation-scoped swarm key:
    the spec set, the config needed to rebuild the tool list on rehydrate,
    and the session status (running / terminated / explicit quarantine).
  * ``swarm_agents`` — one row per sub-agent: its full ``messages`` array
    (the resumable conversation), live status, the final result, and a
    ``delivered`` flag that replaces the in-memory inbox for crash recovery.

Both are keyed by the reusable conversation swarm key, so they are resumable
state reclaimed at TTL eviction and new-wave replacement. Each settled agent
outcome is ALSO durable user state: ``save_run_result`` records it as a
portable generic record keyed by (owner, swarm_run_id, agent_id) that outlives
those rows, and session teardown deletes the rows only after every settled
result is recorded (``archive_session_results`` when no orchestrator remains).

Write cadence is round-boundary plus pre-tool intent (never per-token).
The write-ahead phase prevents a crash from silently repeating a tool whose
outcome was not durably acknowledged. A checkpoint that changes only compact
result fields of the last full save (the pre-request ``model_in_flight``
write-ahead of an unchanged transcript) uses ``mark_agent_checkpoint``, a
constant-size overlay, instead of rewriting the transcript. A checkpoint whose
transcript only grew since the writer's last acknowledged write sends just the
new messages with ``append_agent_checkpoint``; any other change saves in full.

Design rules
------------
* Observation snapshots are best-effort. ``save_agent(required=True)`` is
  the pre-tool safety fence: an unavailable store refuses execution rather
  than permitting an external side effect with no crash-recovery evidence.
* Every write is one semantic command and one complete Sidecar transaction.
  A command receipt makes replay after an ambiguous acknowledgement safe.
"""

from __future__ import annotations

import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.swarm.persistence_contract import (
    SWARM_CONTINUE_REBIND_MODE,
    SWARM_DELETE_EXACT_OPERATION,
    SWARM_LIFECYCLE_GET_OPERATION,
    swarm_archive_result,
    swarm_run_result_key,
    SWARM_RUN_RESULT_FORMAT,
    SWARM_RUN_RESULT_NAMESPACE,
)
from lib.timeutil import now_ms

logger = get_logger(__name__)


_now_ms = now_ms


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _command(operation: str, payload: dict, *, task: dict | None = None,
             cancellable: bool = True) -> dict:
    command_id = f'{operation}:{uuid.uuid4().hex}'
    if task is None:
        return _storage(write=True).command(operation, payload, command_id)
    from lib.storage.protocol import FrozenPayload
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    # One encoding is the receipt's frozen payload: every retry resends it.
    frozen = FrozenPayload(payload)
    return wait_for_persistence(task, lambda: _storage(write=True).command(
        operation, frozen, command_id), label=operation, cancellable=cancellable)


# ═══════════════════════════════════════════════════════════
#  Session-level
# ═══════════════════════════════════════════════════════════

def uses_sql_lifecycle() -> bool:
    """Select shapes for the existing authority, never select an authority."""
    from lib.storage.client import StorageClient
    from lib.storage.errors import StorageError
    try:
        return isinstance(_storage(), StorageClient)
    except StorageError:
        from lib.storage import storage_status
        if storage_status().get('backend') == 'tofudb':
            return False  # Native's ordinary writer is still best-effort.
        raise


def exact_run_fields(run_id: str) -> dict:
    return {'run_id': run_id} if run_id and uses_sql_lifecycle() else {}


def _checkpoint_writer_fields(run_id: str, checkpoint_writer_epoch: int) -> dict:
    fields = exact_run_fields(run_id)
    if fields:
        fields['checkpoint_writer_epoch'] = checkpoint_writer_epoch
    return fields


def run_identity(swarm_key: str, row: dict) -> dict:
    from lib.conversations.project_identity import normalize_project_path
    config = row['config']
    parent = config.get('parent_cfg') or {}
    return dict(swarm_key=swarm_key, run_id=row['task_id'],
                owner_id=require_user_id(config.get('user_id'), context='swarm lifecycle'),
                conversation_id=row.get('conv_id') or '', tenant_id=parent.get('_tenant_id') or '',
                project_path=normalize_project_path(parent.get('canonicalProjectPath')
                    or parent.get('projectPath') or config.get('project_path') or ''))


def read_lifecycle(identity: dict) -> dict | None:
    return _storage().query(SWARM_LIFECYCLE_GET_OPERATION, identity)


def admit_handoff(identity: dict, *, caller_task_id: str, turn_id: str | None,
                  attempt_id: str | None, stage: str, continue_binding: bool = False,
                  checkpoint_writer_epoch: int = 0) -> None:
    """Claim one live handoff; never retry an unconfirmed admission receipt."""
    result = _command('swarm.session.save', dict(identity, save_mode='admit',
        caller_task_id=caller_task_id, turn_id=turn_id, attempt_id=attempt_id, stage=stage,
        **({'continue_binding': True} if continue_binding else {}),
        **({'checkpoint_writer_epoch': checkpoint_writer_epoch} if checkpoint_writer_epoch else {})))
    if result != {'admitted': True}:
        from lib.storage.errors import StorageError
        raise StorageError('database_protocol_error', 'Swarm handoff not acknowledged')


def rebind_continue(identity: dict, snapshot: dict, config: dict, task: dict, *,
                    activation_failed: bool = False, unbound_only: bool = False) -> int | None:
    """Retry the same frozen mutation; its ACK is not a scheduler permit."""
    from lib.storage.errors import StorageError
    caller = task['config']
    result = _command('swarm.session.save', dict(identity, save_mode=SWARM_CONTINUE_REBIND_MODE,
        expected_updated_at=snapshot['updated_at'], config=config, caller_task_id=task['id'],
        turn_id=caller['_turnId'], attempt_id=caller['_attemptId'],
        **({'activation_failed': True, 'unbound_only': unbound_only} if activation_failed else {})), task=task)
    if activation_failed and result == {'failed_activation_recorded': True}:
        return None
    epoch = result.get('checkpoint_writer_epoch') if isinstance(result, dict) else None
    if activation_failed or type(epoch) is not int or epoch <= 0 or result.get('rebound') is not True:
        raise StorageError('database_protocol_error', 'Swarm Continue rebind not acknowledged')
    return epoch


def has_committed_stop(*, run_id: str, owner_id: int) -> bool:
    """Observe only the owner's closed Stop outcome by immutable run ID."""
    if not uses_sql_lifecycle():
        return False
    receipt = _storage().query(SWARM_LIFECYCLE_GET_OPERATION,
                               {'run_id': run_id, 'owner_id': owner_id})
    return receipt == {'disposition': 'stop'}


def save_session(swarm_key: str, *, conv_id: str, task_id: str,
                 specs: list, config: dict, status: str = 'running',
                 required: bool = False, save_mode: str | None = None,
                 task: dict | None = None, checkpoint_writer_epoch: int = 0) -> None:
    """Upsert the session row. ``specs`` is a list of ``SubTaskSpec.to_dict()``.

    ``config`` carries everything needed to rebuild the sub-agent tool list
    and model on rehydrate (see ``integration._persist_config_for``).
    """
    if not swarm_key:
        return
    now = _now_ms()
    try:
        response = _command('swarm.session.save', {
            'swarm_key': swarm_key,
            'conv_id': conv_id,
            'task_id': task_id,
            'status': status,
            'specs': specs,
            'config': config,
            'now_ms': now,
            **({'save_mode': save_mode} if save_mode is not None else {}),
            **({'checkpoint_writer_epoch': checkpoint_writer_epoch} if checkpoint_writer_epoch else {}),
        }, task=task)
        if required and (not isinstance(response, dict) or response.get('saved') is not True):
            from lib.storage.errors import StorageError
            raise StorageError('database_protocol_error', 'Swarm session save not acknowledged')
        logger.debug('[SwarmPersist] saved session key=%s status=%s specs=%d',
                     swarm_key, status, len(specs))
    except Exception as e:
        logger.error('[SwarmPersist] save_session(%s) FAILED — resumable '
                     'session state not updated: %s', swarm_key, e,
                     exc_info=True)
        if required:
            raise


def mark_session_terminated(swarm_key: str, *, run_id: str = '',
                            checkpoint_writer_epoch: int = 0) -> None:
    """Flag the session row terminated (driver thread exited)."""
    if not swarm_key:
        return
    try:
        result = _command('swarm.session.terminate', {
            'swarm_key': swarm_key, 'now_ms': _now_ms(),
            **_checkpoint_writer_fields(run_id, checkpoint_writer_epoch),
        })
        if isinstance(result, dict) and result.get('changed') is True:
            logger.debug('[SwarmPersist] session %s → terminated', swarm_key)
    except Exception as e:
        logger.warning('[SwarmPersist] mark_session_terminated(%s) failed: %s',
                       swarm_key, e)


def quarantine_ownerless_session(swarm_key: str) -> bool:
    """Persistently exclude an invalid ownerless row from startup recovery.

    The Sidecar re-checks the durable config inside the command transaction;
    a row repaired concurrently is left unchanged. Agent rows are retained as
    diagnostic/recovery evidence. ``True`` means this call performed the
    quarantine, while every storage failure remains best-effort and falsy.
    """
    if not swarm_key:
        return False
    try:
        result = _command('swarm.session.quarantine_ownerless', {
            'swarm_key': swarm_key, 'now_ms': _now_ms(),
        })
    except Exception as e:
        logger.error(
            '[SwarmPersist] quarantine_ownerless_session(%s) FAILED: %s',
            swarm_key, e, exc_info=True)
        return False
    changed = bool(result.get('changed')) if isinstance(result, dict) else False
    if changed:
        logger.debug(
            '[SwarmPersist] quarantined ownerless session key=%s; durable '
            'agent evidence retained', swarm_key)
    return changed


def _retirement_refusal(observation, error):
    from lib.storage.errors import StorageError
    if (observation is not None and isinstance(error, StorageError)
            and not error.retryable and error.code in {'database_conflict', 'database_not_found'}):
        observation['outcome'] = 'refused'


def _retirement_ack(observation, ack):
    if observation is not None and isinstance(ack, dict) and type(ack.get('deleted')) is bool:
        observation['outcome'] = 'committed' if ack['deleted'] else 'noop'


def delete_session(swarm_key: str, *, observation=None) -> None:
    """Remove a session and all its agent rows (TTL eviction / abort).

    These rows are resumable state. Callers record every settled outcome as a
    run result first (``save_run_result``/``archive_session_results``).
    """
    if not swarm_key:
        return
    if observation is not None:
        observation.update(requested=True, outcome='unknown', scope='key_only')
    try:
        ack = _command('swarm.session.delete', {'swarm_key': swarm_key})
        _retirement_ack(observation, ack)
    except Exception as e:
        _retirement_refusal(observation, e)
        logger.warning('[SwarmPersist] delete_session failed')


# ═══════════════════════════════════════════════════════════
#  Agent-level
# ═══════════════════════════════════════════════════════════

def save_agent(swarm_key: str, agent_id: str, *,
               role: str, objective: str, status: str,
               messages: list, result: dict | None = None,
               rounds_used: int = 0, delivered: bool | None = None,
               required: bool = False, task: dict | None = None,
               cancellable: bool = True, checkpoint_id: str = '',
               run_id: str = '', checkpoint_writer_epoch: int = 0) -> dict | None:
    """Upsert one agent's checkpoint; the authority's acknowledgement, or None.

    ``messages`` is the agent's full conversation array — the resumable state.
    ``result`` is ``SubAgentResult.to_dict()`` (or None mid-run). ``delivered``
    is left unchanged when None (so a checkpoint write doesn't clobber a
    previously-set delivered flag). A full save retires any checkpoint marker.
    A non-empty ``checkpoint_id`` asks the authority to keep this save as the
    base of :func:`append_agent_checkpoint`; the acknowledgement echoes the id
    only when it did. None means an optional write failed; a required write
    raises instead.
    """
    if not swarm_key or not agent_id:
        return None
    now = _now_ms()
    payload = {
        'swarm_key': swarm_key,
        'agent_id': agent_id,
        'role': role,
        'objective': objective,
        'status': status,
        'messages': messages or [],
        'result': result or {},
        'rounds_used': rounds_used,
        'delivered': delivered,
        'now_ms': now,
    }
    if checkpoint_id:
        payload['checkpoint_id'] = checkpoint_id
    try:
        payload.update(_checkpoint_writer_fields(run_id, checkpoint_writer_epoch))
        response = _command('swarm.agent.save', payload,
                            task=task if required else None, cancellable=cancellable)
        if 'run_id' in payload and (not isinstance(response, dict) or response.get('saved') is not True):
            from lib.storage.errors import StorageError
            raise StorageError('database_protocol_error', 'Exact swarm checkpoint not acknowledged')
        logger.debug('[SwarmPersist] saved agent key=%s id=%s status=%s rounds=%d msgs=%d',
                     swarm_key, agent_id, status, rounds_used, len(messages or []))
        return dict(response) if isinstance(response, dict) else {}
    except Exception as e:
        logger.error('[SwarmPersist] save_agent(%s/%s) FAILED — resumable '
                     'agent checkpoint not updated: %s', swarm_key, agent_id,
                     e, exc_info=True)
        if required:
            raise
        return None


def append_agent_checkpoint(swarm_key: str, agent_id: str, *,
                            base_checkpoint_id: str, base_message_count: int,
                            checkpoint_id: str, messages: list, result: dict,
                            status: str, rounds_used: int, task: dict,
                            cancellable: bool = True, run_id: str = '',
                            checkpoint_writer_epoch: int = 0) -> bool:
    """Append ``messages`` to the checkpoint this writer last wrote; True once applied.

    The base is that write's ``checkpoint_id`` and message count. The authority
    appends only while both still match and then holds exactly what a full save
    of the extended transcript would, ``result`` and ``status`` included. False
    means the base no longer matches (another write since, or no agent row) or
    the authority predates the operation (``unknown_operation``): the caller
    saves in full. Every other storage failure propagates; an append is always
    a required write.
    """
    from lib.storage.errors import StorageError
    try:
        response = _command('swarm.agent.append', {
            'swarm_key': swarm_key,
            'agent_id': agent_id,
            **_checkpoint_writer_fields(run_id, checkpoint_writer_epoch),
            'base_checkpoint_id': base_checkpoint_id,
            'base_message_count': base_message_count,
            'checkpoint_id': checkpoint_id,
            'messages': messages,
            'result': result,
            'status': status,
            'rounds_used': rounds_used,
            'now_ms': _now_ms(),
        }, task=task, cancellable=cancellable)
    except StorageError as error:
        if error.code != 'unknown_operation':
            raise
        logger.info('[SwarmPersist] authority has no swarm.agent.append; '
                    'saving %s/%s in full', swarm_key, agent_id)
        return False
    appended = isinstance(response, dict) and response.get('appended') is True
    logger.debug('[SwarmPersist] appended agent key=%s id=%s base=%d new=%d appended=%s',
                 swarm_key, agent_id, base_message_count, len(messages), appended)
    return appended


def mark_agent_checkpoint(swarm_key: str, agent_id: str, *, rounds_used: int,
                          result: dict, task: dict | None = None, run_id: str = '',
                          checkpoint_writer_epoch: int = 0) -> bool:
    """Overlay compact result fields on the agent's last full checkpoint.

    ``result`` holds only ``SWARM_CHECKPOINT_MARKER_RESULT_FIELDS`` (phase,
    completeness, usage, elapsed time, rounds); the transcript and every other
    field stay as the last ``save_agent`` wrote them. The write is one compact
    command whose size does not grow with the transcript. False means the
    authority holds no full checkpoint to mark, so the caller must save one.
    Storage failures propagate: this is a required write-ahead.
    """
    response = _command('swarm.agent.checkpoint.mark', {
        'swarm_key': swarm_key, **_checkpoint_writer_fields(run_id, checkpoint_writer_epoch),
        'agent_id': agent_id,
        'rounds_used': rounds_used,
        'result': result,
        'now_ms': _now_ms(),
    }, task=task)
    marked = isinstance(response, dict) and response.get('marked') is True
    logger.debug('[SwarmPersist] marked agent key=%s id=%s phase=%s rounds=%d marked=%s',
                 swarm_key, agent_id, result.get('checkpoint_phase'), rounds_used, marked)
    return marked


def mark_delivered(swarm_key: str, agent_ids, *, task_id: str,
                   checkpoint_writer_epoch: int = 0) -> bool:
    """Mark the given agents' results as delivered to the main model.

    Called from every channel that hands a result to the model: the
    orchestrator's inbox drain, and the master's await/get_agent_result
    dedup. After this, a rehydrate will NOT re-enqueue these as
    ``<swarm-update>``s.

    The durable session must still belong to ``task_id`` inside the ACK
    transaction. Unsupported/uncertain replies retain the pending receipt.
    """
    if not task_id or not swarm_key:
        return False
    if not agent_ids:
        return True
    ids = [str(a) for a in agent_ids]
    if not ids:
        return True
    try:
        result = _command('swarm.agents.acknowledge_results', {
            'swarm_key': swarm_key, 'task_id': task_id, 'agent_ids': ids,
            **({'checkpoint_writer_epoch': checkpoint_writer_epoch} if checkpoint_writer_epoch else {}),
        })
        return isinstance(result, dict) and result.get('acknowledged') is True
    except Exception as e:
        logger.warning('[SwarmPersist] mark_delivered(%s) failed: %s', swarm_key, e)
        return False


# ═══════════════════════════════════════════════════════════
#  Point lookup (status route fallback)
# ═══════════════════════════════════════════════════════════

class SqlSessionSnapshot(dict):
    """In-process provenance of a loaded SQL snapshot; no new durable field."""


def load_session(swarm_key: str, *, required: bool = False,
                 include_agents: bool = True,
                 agent_id: str | None = None, identity: dict | None = None) -> dict | None:
    """Fetch one persisted session row (+ every agent row) by swarm key.

    Used by ``integration.get_swarm_status`` when the session is NOT in
    process memory — the difference between "this swarm terminated, here are
    the real per-agent outcomes" and "no record at all, keep probing". A
    crash/restart evicts memory but leaves this durable row, so answering
    from it is what lets a reloaded panel settle with TRUTH instead of the
    unknown/无结果 limbo. Best-effort: returns None on any storage error.

    The header and the agent checkpoints are separate bounded reads, so a
    session whose checkpoints exceed one storage response is assembled page
    by page instead of being refused. ``include_agents=False`` returns only
    the header (run identity, status, specs, config) and reads no checkpoint.
    ``agent_id`` is the per-round run fence: one compact read returns the
    header without specs/config and ``agents`` holding at most that agent's
    state (no messages/result), independent of retained checkpoint volume.
    """
    if not swarm_key:
        return None
    from lib.storage.errors import StorageError
    try:
        storage = _storage()
        if agent_id is not None:
            return _load_agent_state(storage, swarm_key, agent_id)
        row = storage.query('swarm.session.get',
                            {'swarm_key': swarm_key, 'include_agents': False})
        if not isinstance(row, dict):
            return None
        if identity is not None and run_identity(swarm_key, row) != identity:
            return None
        if not include_agents:
            return row
        agents: list[dict] = []
        cut = {'swarm_key': swarm_key}
        exact_cut = 'checkpoint_writer_epoch' in row
        if exact_cut:
            cut.update(user_id=run_identity(swarm_key, row)['owner_id'],
                       expected_task_id=row['task_id'], expected_updated_at=row['updated_at'])
        page_request = cut
        while True:
            page = storage.query('swarm.agents.page', page_request)
            rows = page.get('agents') if isinstance(page, dict) else None
            cursor = page.get('next_after_agent_id') if isinstance(page, dict) else None
            if not isinstance(rows, list):
                raise StorageError('database_protocol_error', 'Invalid swarm agent page')
            if exact_cut:
                token = page.get('snapshot_cut')
                if (not isinstance(token, str) or len(token) != 64
                        or any(ch not in '0123456789abcdef' for ch in token)
                        or ('snapshot_cut' in cut and token != cut['snapshot_cut'])):
                    raise StorageError('database_protocol_error', 'Invalid swarm snapshot cut response')
                cut = {**cut, 'snapshot_cut': token}
            agents.extend(rows)
            if cursor is None:
                break
            if (not rows or cursor != rows[-1].get('agent_id')
                    or cursor == page_request.get('after_agent_id')):
                raise StorageError('database_protocol_error',
                                   'Swarm agent page continuation did not advance')
            page_request = {**cut, 'after_agent_id': cursor}
        from lib.storage.client import StorageClient
        if ('checkpoint_writer_epoch' in row or isinstance(storage, StorageClient)) and storage.query(
                'swarm.session.get', {'swarm_key': swarm_key, 'include_agents': False}) != row:
            raise StorageError('database_conflict', 'Swarm checkpoint cut changed during checkpoint read')
    except Exception as e:
        logger.warning('[SwarmPersist] load_session(%s) failed: %s',
                       swarm_key, e)

        if required:
            raise
        return None
    snapshot = {**row, 'agents': agents}
    return SqlSessionSnapshot(snapshot) if isinstance(storage, StorageClient) else snapshot


def _load_agent_state(storage, swarm_key: str, agent_id: str) -> dict | None:
    from lib.storage.errors import StorageError
    row = storage.query('swarm.agent.state.get',
                        {'swarm_key': swarm_key, 'agent_id': agent_id})
    if not isinstance(row, dict):
        return None
    agent = row.pop('agent', None)
    if agent is not None and (not isinstance(agent, dict)
                              or agent.get('agent_id') != agent_id):
        raise StorageError('database_protocol_error', 'Invalid swarm agent state')
    return {**row, 'agents': [agent] if agent is not None else []}


# ═══════════════════════════════════════════════════════════
#  Rehydration (startup)
# ═══════════════════════════════════════════════════════════

def list_resumable_session_keys() -> list[str]:
    """Return the keys of persisted sessions worth rehydrating on startup.

    A session is worth rehydrating when it has at least one non-terminal
    agent (work to resume) OR at least one completed-but-undelivered result
    (a notification the main agent never saw). Fully-terminated, fully-
    delivered sessions are skipped (their finished transcripts already live
    on disk and in ``swarm_agents`` for ad-hoc ``get_agent_result``).

    Keys only: each session is then read with :func:`load_session`, so the
    retained checkpoint volume never has to fit one storage response. A
    storage failure propagates; it is never reported as "no sessions".
    """
    from lib.storage.errors import StorageError
    rows = _storage().query('swarm.resumable.list', {})
    if not isinstance(rows, list) or not all(
            isinstance(row, dict) and isinstance(row.get('swarm_key'), str)
            and row['swarm_key'] for row in rows):
        raise StorageError('database_protocol_error', 'Invalid swarm resumable index response')
    if rows:
        logger.info('[SwarmPersist] %d resumable swarm session(s) found on startup',
                    len(rows))
    return [row['swarm_key'] for row in rows]


# ═══════════════════════════════════════════════════════════
#  Run results (durable user state keyed by swarm_run_id)
# ═══════════════════════════════════════════════════════════

def _run_result_key(owner_id: int, swarm_run_id: str, agent_id: str) -> str:
    """Owner-first record key; run and agent IDs are digested to bound it.

    The owner prefix keeps one owner's records unaddressable by another on an
    authority whose generic records are not owner-partitioned (SQL). The
    record value carries the readable identities.
    """
    return swarm_run_result_key(owner_id, swarm_run_id, agent_id)


def save_run_result(*, owner_id: int, tenant_id, conversation_id: str,
                    swarm_key: str, swarm_run_id: str, agent_id: str,
                    role: str, objective: str, status: str,
                    result: dict, checkpoint_writer_epoch: int = 0) -> bool:
    """Record one agent outcome under its run; True once acknowledged.

    ``status`` is the agent's durable status and ``result`` its
    ``SubAgentResult.to_dict()``; only ``SWARM_RUN_RESULT_FIELDS`` are kept.
    Recording the same run agent again replaces its record with the newer
    outcome. Storage failures are logged and reported as ``False`` so session
    teardown keeps the resumable rows instead of deleting unrecorded results.
    """
    owner = require_user_id(owner_id, context='swarm run result')
    if not swarm_run_id or not agent_id:
        raise ValueError('swarm run result requires swarm_run_id and agent_id')
    result = result if isinstance(result, dict) else {}
    value = {
        'format': SWARM_RUN_RESULT_FORMAT,
        'ownerId': owner,
        'tenantId': tenant_id,
        'conversationId': conversation_id or '',
        'swarmKey': swarm_key or '',
        'swarmRunId': swarm_run_id,
        'agentId': agent_id,
        'role': role or '',
        'objective': objective or '',
        'status': status or 'unknown',
        'result': swarm_archive_result(result),
        'recordedAtMs': _now_ms(),
    }
    try:
        _command('record.put', {
            'namespace': SWARM_RUN_RESULT_NAMESPACE,
            'key': _run_result_key(owner, swarm_run_id, agent_id),
            'value': value,
            **({'checkpoint_writer_epoch': checkpoint_writer_epoch} if checkpoint_writer_epoch else {}),
        })
    except Exception as e:
        logger.error('[SwarmPersist] save_run_result(%s/%s) FAILED — run '
                     'result not recorded: %s', swarm_run_id, agent_id, e,
                     exc_info=True)
        return False
    logger.debug('[SwarmPersist] recorded run result run=%s agent=%s status=%s',
                 swarm_run_id, agent_id, value['status'])
    return True


def load_run_result(*, owner_id: int, swarm_run_id: str,
                    agent_id: str) -> dict | None:
    """Return this owner's record of one run agent, or None when absent.

    Storage failures propagate: an unavailable authority is not absence. A
    record whose identity disagrees with its key is an integrity failure.
    """
    from lib.storage.errors import StorageError
    owner = require_user_id(owner_id, context='swarm run result')
    row = _storage().query('record.get', {
        'namespace': SWARM_RUN_RESULT_NAMESPACE,
        'key': _run_result_key(owner, swarm_run_id, agent_id),
    })
    if row is None:
        return None
    value = row.get('value') if isinstance(row, dict) else None
    if (not isinstance(value, dict)
            or value.get('format') != SWARM_RUN_RESULT_FORMAT
            or value.get('ownerId') != owner
            or value.get('swarmRunId') != swarm_run_id
            or value.get('agentId') != agent_id
            or not isinstance(value.get('result'), dict)):
        raise StorageError('database_integrity', 'Invalid swarm run result record')
    return value


def dispose_run(identity: dict, disposition: str, *, checkpoint_writer_epoch: int | None = None,
                observation=None) -> bool:
    """Archive only this run, then let SQL compare and seal its checkpoints."""
    key = identity['swarm_key']
    row = load_session(key, required=True, identity=identity)
    epoch = checkpoint_writer_epoch
    if row is not None:
        observed_epoch = row.get('checkpoint_writer_epoch', 0)
        if epoch is not None and epoch != observed_epoch:
            return False
        epoch = observed_epoch
        if not _archive_snapshot(key, row):
            return False
    elif epoch is None and disposition != 'stop':
        # A disposed receipt can authorize a disposition upgrade, not a new
        # write against a live successor that reuses the logical run ID.
        previous = read_lifecycle(identity)
        if previous and previous.get('disposition'):
            epoch = previous.get('checkpoint_writer_epoch', 0)
    if observation is not None:
        observation.update(requested=True, outcome='unknown', scope='exact_run')
    try:
        ack = _command(SWARM_DELETE_EXACT_OPERATION, {**identity, 'disposition': disposition,
            **({'checkpoint_writer_epoch': epoch} if epoch and disposition != 'stop' else {})})
    except Exception as error:
        _retirement_refusal(observation, error)
        raise
    # An ACK may describe a historical transaction; observe the exact receipt.
    receipt = read_lifecycle(identity)
    if not receipt or not receipt.get('disposition'):
        return False
    if receipt.get('identity') == identity:
        _retirement_ack(observation, ack)
    return disposition == 'ordinary_retirement' or receipt['disposition'] == disposition


def archive_session_results(swarm_key: str, *, correlation: dict | None = None) -> bool:
    """Record every agent row of one durable session as a run result.

    Teardown uses this when the resumable rows are reclaimed without an
    in-memory orchestrator to record from. ``True`` means every agent row is
    recorded (or no session exists); an unreadable session, a missing run
    identity or an unprovable owner returns ``False`` so the rows are kept.
    """
    try:
        row = load_session(swarm_key, required=True)
    except Exception as exc:
        # Uncertainty keeps durable rows: they are recorded on a later attempt.
        logger.warning('[SwarmPersist] archive_session_results(%s) cannot read the '
                       'session; its rows are kept: %s', swarm_key, exc)
        return False
    if row is None:
        return True
    return _archive_snapshot(swarm_key, row, correlation=correlation)


def _archive_snapshot(swarm_key: str, row: dict, *, correlation: dict | None = None) -> bool:
    config = row.get('config') if isinstance(row.get('config'), dict) else {}
    try:
        owner = require_user_id(config.get('user_id'), context='swarm run archive')
    except ValueError:
        return False
    if correlation is not None:
        correlation.update(owner=owner, conversation=row.get('conv_id'), run=row.get('task_id'))
    swarm_run_id = str(row.get('task_id') or '')
    if not swarm_run_id:
        return False
    parent_cfg = config.get('parent_cfg')
    tenant_id = parent_cfg.get('_tenant_id') if isinstance(parent_cfg, dict) else None
    recorded = True
    for agent in row.get('agents') or []:
        if not isinstance(agent, dict) or not agent.get('agent_id'):
            recorded = False
            continue
        recorded = save_run_result(
            owner_id=owner, tenant_id=tenant_id,
            conversation_id=str(row.get('conv_id') or ''), swarm_key=swarm_key,
            swarm_run_id=swarm_run_id, agent_id=str(agent['agent_id']),
            role=agent.get('role') or '', objective=agent.get('objective') or '',
            status=agent.get('status') or 'unknown',
            result=swarm_archive_result(agent.get('result') or {},
                                        rounds_used=agent.get('rounds_used')),
            checkpoint_writer_epoch=row.get('checkpoint_writer_epoch', 0)) and recorded
    return recorded
