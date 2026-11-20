"""SQL exact-run teardown and the shared mailbox/archive fences."""
from collections.abc import Mapping
import hashlib

import orjson

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id
from lib.storage.errors import StorageError
from lib.swarm.message_contract import NAMESPACE
from lib.swarm.persistence_contract import (
    SWARM_DISPOSITION_PRIORITY, SWARM_MAILBOX_DISPOSITION_FIELD,
    SWARM_RUN_RESULT_FORMAT, SWARM_RUN_RESULT_NAMESPACE,
    SWARM_SESSION_WRITABLE_STATUSES, SwarmDeleteExactResult, SwarmRunIdentity,
    swarm_archive_result, swarm_mailbox_key, swarm_run_result_key,
)
from ._common import _dump, _integer, _load, _required_text
from ._records import _record_get_owned, _record_put_owned


_BINDING_FIELDS = dict(swarm_key='swarmKey', run_id='swarmRunId', owner_id='owner',
                       tenant_id='tenant', conversation_id='conversationId',
                       project_path='projectPath')


def _run_address(payload: Mapping) -> tuple[int, str]:
    try:
        owner = require_user_id(payload.get('owner_id'), context='swarm exact run')
    except ValueError as exc:
        raise StorageError('database_protocol_error', 'Invalid swarm owner') from exc
    if type(payload.get('owner_id')) is not int or owner > 2**63 - 1:
        raise StorageError('database_protocol_error', 'Invalid swarm owner')
    run = _required_text(payload, 'run_id', 4096)
    if '\0' in run:
        raise StorageError('database_protocol_error', 'Invalid swarm run')
    return owner, run


def _identity(payload: Mapping) -> SwarmRunIdentity:
    owner, run = _run_address(payload)
    key = _required_text(payload, 'swarm_key', 512)
    if '\0' in key:
        raise StorageError('database_protocol_error', 'Invalid swarm key or run')
    fields = {}
    for name, limit in [('tenant_id', 256), ('conversation_id', 4096), ('project_path', 4096)]:
        value = payload.get(name)
        if not isinstance(value, str) or len(value) > limit or '\0' in value:
            raise StorageError('database_protocol_error', f'Invalid swarm {name}')
        fields[name] = value
    if fields['project_path'] != normalize_project_path(fields['project_path']):
        raise StorageError('database_protocol_error', 'Expected canonical swarm project path')
    return SwarmRunIdentity(swarm_key=key, run_id=run, owner_id=owner, **fields)


def _session_header(session, key):
    session.lock_key('swarm.session', key)
    return session.fetch_one('SELECT conv_id, task_id, config_json, specs_json, status, updated_at FROM swarm_sessions '
                             'WHERE swarm_key = ?', (key,))


def session_identity(key, run, conversation, config):
    if not isinstance(config, Mapping):
        raise StorageError('database_integrity', 'Invalid swarm session config')
    parent = config.get('parent_cfg') or {}
    if not isinstance(parent, Mapping):
        raise StorageError('database_integrity', 'Invalid swarm parent config')
    try:
        owner = require_user_id(config.get('user_id'), context='swarm session owner')
    except ValueError as exc:
        raise StorageError('database_protocol_error', 'Invalid swarm session owner') from exc
    project = (parent.get('canonicalProjectPath') or parent.get('projectPath')
               or config.get('project_path') or '')
    tenant = parent.get('_tenant_id')
    if not isinstance(project, str):
        raise StorageError('database_protocol_error', 'Invalid swarm project path')
    return _identity(dict(swarm_key=key, run_id=run, conversation_id=conversation,
        owner_id=owner, tenant_id='' if tenant is None else tenant,
        project_path=normalize_project_path(project)))


def _matches(header, identity):
    if (header is None or header['task_id'] != identity['run_id']
            or (header['conv_id'] or '') != identity['conversation_id']
            or header['status'] not in SWARM_SESSION_WRITABLE_STATUSES):
        return False
    try:
        return session_identity(identity['swarm_key'], header['task_id'], header['conv_id'] or '',
                                _load(header['config_json'])) == identity
    except StorageError as exc:
        if exc.code != 'database_protocol_error':
            raise
        return False


def _run_digest(run):
    return hashlib.sha256(run.encode()).digest()


def lock_run(session, run):
    session.lock_key('swarm.run', _run_digest(run).hex())


def lifecycle_row(session, run, *, owner=None):
    sql = 'SELECT receipt_json FROM swarm_run_lifecycle WHERE run_identity = ?'
    params = (_run_digest(run),)
    if owner is not None:
        sql += ' AND owner_id = ?'
        params += (owner,)
    row = session.fetch_one(sql, params)
    return _load(row['receipt_json']) if row else None


def read_lifecycle(session, payload):
    if set(payload) == {'owner_id', 'run_id'}:
        # Owner Stop retries know the original public run ID, not the
        # private scope fields. Expose only the committed outcome; never
        # grant an address for mutation or reveal an open roster.
        owner, run = _run_address(payload)
        receipt = lifecycle_row(session, run, owner=owner)
        if (receipt and receipt['disposition'] == 'stop'
                and receipt['identity']['owner_id'] == owner
                and receipt['identity']['run_id'] == run):
            return {'disposition': 'stop'}
        return None
    identity = _identity(payload)
    receipt = lifecycle_row(session, identity['run_id'], owner=identity['owner_id'])
    return receipt if receipt and receipt['identity'] == identity else None


def key_has_lifecycle(session, key):
    return session.fetch_one('SELECT 1 AS present FROM swarm_run_lifecycle WHERE swarm_key = ? LIMIT 1',
                             (key,)) is not None


def parent_identity(config):
    parent = config.get('parent_cfg') or {}
    if not isinstance(parent, Mapping):
        raise StorageError('database_integrity', 'Invalid swarm parent config')
    fields = {name: (parent[key] if parent.get(key) is not None else '') for name, key in (
        ('turn_id', '_swarmParentTurnId'), ('task_id', '_swarmParentTaskId'),
        ('attempt_id', '_swarmParentAttemptId'))}
    if any(not isinstance(value, str) or len(value) > 4096 or '\0' in value
           for value in fields.values()):
        raise StorageError('database_protocol_error', 'Invalid swarm parent identity')
    return fields


def validate_roster(roster):
    if not isinstance(roster, list):
        raise StorageError('database_protocol_error', 'Invalid swarm roster')
    by_id = {}
    for spec in roster:
        agent = _required_text(spec, 'id', 512) if isinstance(spec, Mapping) else None
        if not agent or '\0' in agent or agent in by_id:
            raise StorageError('database_protocol_error', 'Invalid swarm roster agent')
        by_id[agent] = spec
    return by_id


def reserve_lifecycle(session, identity, config, roster):
    validate_roster(roster)
    receipt = dict(identity=identity, parent=parent_identity(config), roster=roster, disposition=None)
    count = session.execute(
        'INSERT INTO swarm_run_lifecycle(run_identity, swarm_key, owner_id, receipt_json) '
        'VALUES (?, ?, ?, ?) ON CONFLICT(run_identity) DO NOTHING',
        (_run_digest(identity['run_id']), identity['swarm_key'], identity['owner_id'], _dump(receipt).decode()))
    if not count:
        raise StorageError('database_conflict', 'Swarm launch identity already consumed')
    return receipt


def update_lifecycle(session, receipt):
    identity = receipt['identity']
    session.execute('UPDATE swarm_run_lifecycle SET receipt_json = ? WHERE run_identity = ?',
                    (_dump(receipt).decode(), _run_digest(identity['run_id'])))


def _mailbox_address(binding, identity):
    agent = _required_text(binding, 'agentId', 512)
    return swarm_mailbox_key(identity['owner_id'], identity['tenant_id'],
                             identity['conversation_id'], identity['run_id'], agent)


def put_mailbox(session, payload):
    value = payload.get('value')
    binding = value.get('binding') if isinstance(value, Mapping) else None
    if (not isinstance(binding, Mapping) or SWARM_MAILBOX_DISPOSITION_FIELD in value
            or type(payload.get('expected_version')) is not int):
        raise StorageError('database_protocol_error', 'Mailbox mutation requires an exact binding and CAS')
    identity = _identity({key: binding.get(field) for key, field in _BINDING_FIELDS.items()})
    key = _mailbox_address(binding, identity)
    if key != payload.get('key'):
        raise StorageError('database_protocol_error', 'Invalid exact-run mailbox address')
    header = _session_header(session, identity['swarm_key'])
    if not _matches(header, identity):
        raise StorageError('database_not_found', 'Mailbox run is not current')
    receipt = lifecycle_row(session, identity['run_id'], owner=identity['owner_id']) or {}
    if _integer(payload, 'checkpoint_writer_epoch', minimum=0, default=0) != receipt.get('checkpoint_writer_epoch', 0):
        raise StorageError('database_conflict', 'Mailbox writer epoch changed')
    session.lock_key('record', f'{NAMESPACE}:{key}')
    current = _record_get_owned(session, payload)
    if current is not None and (not isinstance(current['value'], Mapping)
                                or SWARM_MAILBOX_DISPOSITION_FIELD in current['value']
                                or current['value'].get('binding') != binding):
        raise StorageError('database_conflict', 'Mailbox run is disposed or binding changed')
    return _record_put_owned(session, payload)


def _archive_address_matches(key, value):
    if not isinstance(value, Mapping) or value.get('format') != SWARM_RUN_RESULT_FORMAT:
        return False
    owner, run, agent = (value.get(field) for field in ('ownerId', 'swarmRunId', 'agentId'))
    return (type(owner) is int and owner > 0
            and isinstance(run, str) and bool(run)
            and isinstance(agent, str) and bool(agent)
            and key == swarm_run_result_key(owner, run, agent))


def put_archive(session, payload):
    key = _required_text(payload, 'key')
    value = payload.get('value')
    if not _archive_address_matches(key, value) or 'sealedByTeardown' in value:
        raise StorageError('database_conflict', 'Swarm archive address or seal is invalid')
    run = value['swarmRunId']
    # Teardown takes this lock before touching records or publishing a
    # disposition, including when there is no archive row to lock.
    lock_run(session, run)
    receipt = lifecycle_row(session, run)
    if receipt is not None and receipt['disposition'] is not None:
        raise StorageError('database_conflict', 'Swarm run is disposed')
    epoch = _integer(payload, 'checkpoint_writer_epoch', default=0, minimum=0)
    if epoch != (receipt or {}).get('checkpoint_writer_epoch', 0):
        raise StorageError('database_conflict', 'Swarm archive requires its exact current swarm writer')
    session.lock_key('record', f'{SWARM_RUN_RESULT_NAMESPACE}:{key}')
    current = _record_get_owned(session, payload)
    if current and current['value'].get('sealedByTeardown') is True:
        raise StorageError('database_conflict', 'Swarm archive is sealed')
    return _record_put_owned(session, payload)


def get_archive(session, payload):
    key = _required_text(payload, 'key')
    try:
        _, run_hex, _ = key.split(':')
        run_digest = bytes.fromhex(run_hex) if len(run_hex) == 64 else None
    except ValueError:
        run_digest = None
    # One statement: PostgreSQL's default read isolation must not combine
    # the old unsealed value with a newer committed disposition.
    row = session.fetch_one(
        'SELECT r.value_json, r.version, r.updated_at_ms, l.receipt_json '
        'FROM storage_records AS r LEFT JOIN swarm_run_lifecycle AS l ON l.run_identity = ? '
        'WHERE r.namespace = ? AND r.record_key = ?',
        (run_digest, SWARM_RUN_RESULT_NAMESPACE, key),
    )
    if row is None:
        return None
    value = _load(row['value_json'])
    if not _archive_address_matches(key, value):
        raise StorageError('database_integrity', 'Invalid swarm archive address')
    receipt = _load(row['receipt_json'])
    if receipt is not None and receipt['disposition'] is not None:
        identity = receipt['identity']
        tenant = value.get('tenantId')
        if (value.get('sealedByTeardown') is not True or value['ownerId'] != identity['owner_id']
                or value['swarmRunId'] != identity['run_id'] or value.get('swarmKey') != identity['swarm_key']
                or value.get('conversationId') != identity['conversation_id']
                or ('' if tenant is None else tenant) != identity['tenant_id']):
            raise StorageError('database_integrity', 'Disposed swarm archive is unsealed or misbound')
    return {'value': value, 'version': int(row['version']), 'updated_at_ms': int(row['updated_at_ms'])}


def _verify_archives(session, identity):
    from ._runs import _SWARM_MARKER_COLUMNS, _SWARM_MARKER_JOIN, _swarm_marked_state

    for agent in session.fetch_all(
        'SELECT a.agent_id, a.role, a.objective, a.status, a.result_json, a.rounds_used, a.updated_at, '
        f'{_SWARM_MARKER_COLUMNS} FROM swarm_agents AS a {_SWARM_MARKER_JOIN} '
        'WHERE a.swarm_key = ? ORDER BY a.agent_id', (identity['swarm_key'],),
    ):
        marker, rounds, _ = _swarm_marked_state(agent)
        checkpoint = _load(agent['result_json'])
        if not isinstance(checkpoint, dict):
            raise StorageError('database_integrity', 'Invalid swarm agent result')
        checkpoint.update(marker)
        key = swarm_run_result_key(identity['owner_id'], identity['run_id'], agent['agent_id'])
        session.lock_key('record', f'{SWARM_RUN_RESULT_NAMESPACE}:{key}')
        row = _record_get_owned(session, {'namespace': SWARM_RUN_RESULT_NAMESPACE, 'key': key})
        result = row['value'] if row is not None else None
        if (not isinstance(result, dict) or result.get('format') != SWARM_RUN_RESULT_FORMAT
                or result.get('agentId') != agent['agent_id']
                or type(result.get('ownerId')) is not int
                or result.get('ownerId') != identity['owner_id']
                or (result.get('tenantId') if result.get('tenantId') is not None else '') != identity['tenant_id']
                or result.get('conversationId') != identity['conversation_id']
                or result.get('swarmKey') != identity['swarm_key']
                or result.get('swarmRunId') != identity['run_id']
                or result.get('role') != (agent['role'] or '')
                or result.get('objective') != (agent['objective'] or '')
                or result.get('status') != (agent['status'] or 'pending')
                or orjson.dumps(result.get('result'), option=orjson.OPT_SORT_KEYS) != orjson.dumps(
                    swarm_archive_result(checkpoint, rounds_used=rounds),
                    option=orjson.OPT_SORT_KEYS)):
            raise StorageError('database_conflict', 'Exact swarm agent archive is missing or stale')
        if result.get('sealedByTeardown') is not True:
            _record_put_owned(session, {'namespace': SWARM_RUN_RESULT_NAMESPACE, 'key': key,
                                       'value': {**result, 'sealedByTeardown': True},
                                       'expected_version': row['version']})


def delete_exact(session, payload) -> SwarmDeleteExactResult:
    from ._runs import _swarm_session_delete_rows
    from lib.storage_authority_sql.schema import swarm_mailbox_json_expression

    identity = _identity(payload)
    disposition = payload.get('disposition')
    if not isinstance(disposition, str) or disposition not in SWARM_DISPOSITION_PRIORITY:
        raise StorageError('database_protocol_error', 'Invalid trusted swarm disposition')
    header = _session_header(session, identity['swarm_key'])
    lock_run(session, identity['run_id'])
    receipt = lifecycle_row(session, identity['run_id'])
    exact = _matches(header, identity)
    unchanged = {'deleted': False, 'mailboxes_changed': 0}
    if receipt is not None and receipt['identity'] != identity:
        return unchanged
    if disposition != 'stop':
        epoch = _integer(payload, 'checkpoint_writer_epoch', default=0, minimum=0)
        if epoch != (receipt or {}).get('checkpoint_writer_epoch', 0):
            raise StorageError('database_conflict', 'Swarm retirement requires its exact current swarm writer')
    if exact:
        if session.fetch_one('SELECT 1 AS present FROM swarm_sessions WHERE task_id = ? '
                             'AND swarm_key <> ? LIMIT 1', (identity['run_id'], identity['swarm_key'])):
            raise StorageError('database_conflict', 'Ambiguous legacy swarm run')
        if receipt is None:
            receipt = reserve_lifecycle(session, identity, _load(header['config_json']),
                                        _load(header['specs_json']))
        if receipt['disposition'] is not None:
            raise StorageError('database_integrity', 'Disposed swarm run still has a live session')
        _verify_archives(session, identity)
    elif (receipt is None or receipt['disposition'] is None
          or (header is not None and header['task_id'] == identity['run_id'])):
        return unchanged
    previous_disposition = receipt['disposition']
    if (previous_disposition is not None and
            SWARM_DISPOSITION_PRIORITY[previous_disposition] >= SWARM_DISPOSITION_PRIORITY[disposition]):
        return unchanged
    update_lifecycle(session, {**receipt, 'disposition': disposition})

    # Discover original-run rows even after the resumable roster has gone.
    # The partial index narrows candidates before decoding mailbox values.
    run_selector = swarm_mailbox_json_expression(session.backend, 'binding', 'swarmRunId')
    rows = session.fetch_all(
        f"SELECT record_key, value_json, version FROM storage_records WHERE namespace = '{NAMESPACE}' "
        f'AND ({run_selector}) = ?',
        (identity['run_id'],),
    )
    changed = 0
    for row in rows:
        value = _load(row['value_json'])
        binding = value.get('binding')
        if not isinstance(binding, dict) or any(binding.get(field) != identity[key]
                                                for key, field in _BINDING_FIELDS.items()):
            continue
        key = row['record_key']
        if key != _mailbox_address(binding, identity):
            raise StorageError('database_integrity', 'Invalid stored exact-run mailbox address')
        previous = value.get(SWARM_MAILBOX_DISPOSITION_FIELD)
        if previous is not None and (not isinstance(previous, str) or previous not in SWARM_DISPOSITION_PRIORITY):
            raise StorageError('database_integrity', 'Invalid stored swarm disposition')
        # Without the header, only an earlier exact teardown can authorize an
        # upgrade. A reused key or a legacy delete is not disposition evidence.
        if (previous is None and not exact) or (previous is not None and
                SWARM_DISPOSITION_PRIORITY[previous] >= SWARM_DISPOSITION_PRIORITY[disposition]):
            continue
        _record_put_owned(session, {'namespace': NAMESPACE, 'key': key,
                                   'value': {**value, SWARM_MAILBOX_DISPOSITION_FIELD: disposition},
                                   'expected_version': int(row['version'])})
        changed += 1
    if exact:
        _swarm_session_delete_rows(session, identity['swarm_key'])
    return {'deleted': exact, 'mailboxes_changed': changed}


def guard_legacy_delete(session, swarm_key):
    """Refuse ambiguous key-only Stop/cleanup once an exact disposition exists."""
    from lib.storage_authority_sql.schema import (
        swarm_disposed_mailbox_predicate, swarm_mailbox_json_expression,
    )

    if key_has_lifecycle(session, swarm_key):
        raise StorageError('database_conflict', 'Swarm key requires exact-run teardown')
    key_selector = swarm_mailbox_json_expression(session.backend, 'binding', 'swarmKey')
    if session.fetch_one(
        'SELECT 1 AS present FROM storage_records '
        f'WHERE {swarm_disposed_mailbox_predicate(session.backend)} '
        f'AND ({key_selector}) = ? LIMIT 1', (swarm_key,),
    ) is not None:
        raise StorageError('database_conflict', 'Swarm key requires exact-run teardown')
