"""Home node registry: durable node records, confirmation, revocation, admission.

Responsibility: the Home's single record of which nodes paired with it, in
which trust state, bound to which credential (contracts/
desktop_node_registry_v1.schema.json; docs/modules/remote_execution.md,
"Node registry, confirmation and trust"). Records live in the Home's selected
storage authority (portable record CAS), so they survive a Home restart and
both runtime processes read the same rows. Liveness is never stored.

- ``record_pairing`` runs inside ``POST /api/desktop/pair`` after the pairing
  is consumed and the credential minted: a new node is ``pending``; a pairing
  for an already active node ID becomes its pending ``replacement``.
- ``confirm`` / ``reject`` / ``rename`` / ``forget`` are the owner's commands.
  Rejecting and forgetting revoke credentials and disconnect live nodes.
- ``admit_poll`` gates every remote poll before the bridge registers it: only
  an ``active`` node bound to the presented credential reaches the bridge, so
  a pending, conflicting or unregistered node can never be addressed.

Transport facts of the poll-terminating process (boot IDs, route kind, round
trip, in-flight count) are kept in memory here: they are reconstructible and
die with the process, exactly like the bridge's liveness.

Entry points: NodeRegistry, Admission, fingerprint, wait_for_confirmation,
transport_facts, CONTRACT, NAMESPACES, CREDENTIAL_ORIGIN.
Dependencies: lib.storage (record.*), lib.api_keys (credential metadata and
revocation), lib.desktop.bridge (disconnect, liveness window), lib.agent_core
push (wake hint).
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import threading
import time
import uuid

from lib.agent_core.storage_errors import StorageError
from lib.desktop_wire.node_protocol import fingerprint
from lib.identity import require_user_id
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                       'desktop_node_registry_v1.schema.json').read_text(encoding='utf-8'))
NAMESPACES = CONTRACT['namespaces']
CREDENTIAL_ORIGIN = CONTRACT['x-credential-origin']
DEVICES_PUSH_TASK_ID = '__devices__'
# The storage record.list protocol maximum per page (storage authority _record_list).
_RECORD_LIST_LIMIT = 1000


class NodeNotFound(LookupError):
    """No node with this ID belongs to the owner."""


class NodeStateError(ValueError):
    """The requested transition does not apply to the node's current state."""


@lru_cache(maxsize=None)
def _validator(name: str):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + name})


def validate(value, name: str):
    error = next(_validator(name).iter_errors(value), None)
    if error is not None:
        location = '.'.join(str(part) for part in error.absolute_path) or name
        raise ValueError(f'Invalid node registry {name} ({location}): {error.message}')
    return value


def valid_node_id(value) -> bool:
    import re
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value) is not None


# ── Transport facts of this (poll-terminating) process ──────────────────

@dataclass
class _Presence:
    boot_id: str = ''
    superseded: dict | None = None  # boot_id -> last seen (monotonic)
    route: str | None = None
    rtt_ms: int | None = None
    running: int = 0
    seen: float = 0.0


_PRESENCE: dict[tuple[int, str], _Presence] = {}
_WAITERS: dict[tuple[int, str], set] = {}
_LOCK = threading.Lock()


def _liveness_window() -> float:
    from lib.desktop.bridge import _CONNECTED_WINDOW_S
    return float(_CONNECTED_WINDOW_S)


def _observe_boot(owner: int, node_id: str, boot_id: str, frame: Mapping) -> str:
    """Record one poll's boot: ``'same'``, ``'restarted'`` (a new boot superseded
    the current one) or ``'superseded'`` (an earlier boot polling again).

    A new boot supersedes the current one. Only a superseded boot that polls
    again within the liveness window is a duplicate; a restarted process
    whose predecessor fell silent never is.
    """
    now = time.monotonic()
    window = _liveness_window()
    with _LOCK:
        presence = _PRESENCE.setdefault((owner, node_id), _Presence(superseded={}))
        presence.superseded = {boot: seen for boot, seen in presence.superseded.items()
                               if now - seen < window}
        outcome = 'same'
        if boot_id:
            if boot_id in presence.superseded:
                presence.superseded[boot_id] = now
                return 'superseded'
            if presence.boot_id and presence.boot_id != boot_id:
                presence.superseded[presence.boot_id] = presence.seen
                outcome = 'restarted'
            presence.boot_id = boot_id
        route = frame.get('route')
        presence.route = route if route in ('direct', 'browser_relay') else None
        rtt = frame.get('rtt_ms')
        presence.rtt_ms = rtt if type(rtt) is int and 0 <= rtt < 3_600_000 else presence.rtt_ms
        active = frame.get('active_command_ids')
        presence.running = len(active) if isinstance(active, list) else 0
        presence.seen = now
        return outcome


def transport_facts(owner: int, node_id: str) -> dict:
    """``{route, rtt_ms, running}`` observed by this process's latest admitted poll."""
    with _LOCK:
        presence = _PRESENCE.get((int(owner), node_id))
        if presence is None:
            return {'route': None, 'rtt_ms': None, 'running': 0}
        return {'route': presence.route, 'rtt_ms': presence.rtt_ms, 'running': presence.running}


def _online(owner: int, node_id: str) -> bool:
    from lib.desktop.bridge import online_connection
    return online_connection(node_id, str(owner)) is not None


def _forget_presence(owner: int, node_id: str) -> None:
    with _LOCK:
        _PRESENCE.pop((owner, node_id), None)


async def wait_for_confirmation(owner: int, node_id: str, timeout: float) -> None:
    """Hold a pending node's poll until a transition of that node or ``timeout``.

    Confirm and reject run in this same (poll-terminating) process; with
    several API workers the hold simply ends at ``timeout`` and the node polls
    again.
    """
    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    entry = (loop, event)
    key = (int(owner), node_id)
    with _LOCK:
        _WAITERS.setdefault(key, set()).add(entry)
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
    except asyncio.TimeoutError:
        pass
    finally:
        with _LOCK:
            waiters = _WAITERS.get(key)
            if waiters is not None:
                waiters.discard(entry)
                if not waiters:
                    del _WAITERS[key]


def _wake(owner: int, node_id: str) -> None:
    with _LOCK:
        waiters = list(_WAITERS.get((owner, node_id)) or ())
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # the loop closed: its poll is gone too
            continue


def notify_devices_changed(owner: int) -> None:
    """Best-effort owner-scoped wake hint; GET /api/v4/desktop/nodes stays the authority."""
    try:
        from lib.agent_core.events import build_push_frame
        from lib.agent_core.push import push_event
        push_event('notify', DEVICES_PUSH_TASK_ID,
                   build_push_frame('devices.changed', userId=owner, rev=time.time_ns() // 1_000_000),
                   user_id=owner)
    except Exception as exc:
        logger.debug('[NodeRegistry] devices.changed push skipped: %s', exc)


# ── Admission ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Admission:
    """Outcome of one poll. ``state``: active | pending | conflict | unregistered | revoked."""
    state: str
    node_id: str
    fingerprint: str | None = None
    reason: str = ''
    restarted: bool = False
    kind: str | None = None

    @property
    def admitted(self) -> bool:
        return self.state == 'active'


class NodeRegistry:
    """One owner's node records, credential index and transitions."""

    def __init__(self, owner: int, *, tenant_id: str | None = None, client=None):
        self.owner = require_user_id(owner, context='node registry')
        self.tenant_id = tenant_id
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.prefix = f'{self.owner}:'

    # ── record CAS ──────────────────────────────────────────────────────
    def _checked(self, row, definition):
        try:
            value = validate(row['value'], definition)
            if value['owner'] != self.owner or type(row['version']) is not int or row['version'] < 1:
                raise ValueError('node registry record escaped its owner')
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError('database_integrity', 'Invalid node registry record') from error
        return row

    def _get(self, namespace: str, key: str, definition: str):
        row = self.client().query('record.get', {'namespace': namespace, 'key': key})
        return None if row is None else self._checked(row, definition)

    def _put(self, namespace: str, key: str, value: dict, version: int) -> int:
        receipt = self.client(write=True).command('record.put', {
            'namespace': namespace, 'key': key, 'value': value, 'expected_version': version,
        }, 'desktop-node:' + uuid.uuid4().hex)
        return int((receipt or {}).get('version') or version + 1)

    def _delete(self, namespace: str, key: str, version: int) -> None:
        try:
            self.client(write=True).command('record.delete', {
                'namespace': namespace, 'key': key, 'expected_version': version,
            }, 'desktop-node-delete:' + uuid.uuid4().hex)
        except StorageError as error:
            if error.code != 'database_conflict':
                raise

    def _node_key(self, node_id: str) -> str:
        if not valid_node_id(node_id):
            raise NodeNotFound(node_id)
        return self.prefix + node_id

    def _node_row(self, node_id: str):
        return self._get(NAMESPACES['nodes'], self._node_key(node_id), 'nodeRecord')

    @staticmethod
    def policy_witness(node_id, row, *, active=False):
        return {'id': node_id, 'version': row['version'] if row else 0,
                'credential': row['value']['credentialId'] if row else None, 'active': active}

    def policy_target(self, node_id):
        """One snapshot for both route ownership and the durable commit witness."""
        row = self._node_row(node_id) if valid_node_id(node_id) else None
        if row is None or row['value']['state'] != 'active':
            return None
        return row['value'], self.policy_witness(node_id, row, active=True)

    def _bind_credential(self, credential_id: str, node_id: str) -> None:
        """Write the credential index before the record it names (a hint, validated on read)."""
        key = self.prefix + credential_id
        while True:
            row = self._get(NAMESPACES['credentials'], key, 'credentialIndex')
            value = validate({'version': 1, 'owner': self.owner, 'credentialId': credential_id,
                              'nodeId': node_id}, 'credentialIndex')
            if row is not None and row['value'] == value:
                return
            try:
                self._put(NAMESPACES['credentials'], key, value, row['version'] if row else 0)
                return
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise

    def _unbind_credential(self, credential_id: str) -> None:
        row = self._get(NAMESPACES['credentials'], self.prefix + credential_id, 'credentialIndex')
        if row is not None:
            self._delete(NAMESPACES['credentials'], self.prefix + credential_id, row['version'])

    def _mutate(self, node_id: str, change, *, reset_policy=False):
        """CAS-retry ``change(value) -> new value | None`` on one node record."""
        while True:
            row = self._node_row(node_id)
            if row is None:
                raise NodeNotFound(node_id)
            updated = change(dict(row['value']))
            if updated is None:
                return row['value']
            validate(updated, 'nodeRecord')
            try:
                from lib.desktop import device_policy
                witness = dict(self.policy_witness(node_id, row, active=row['value']['state'] == 'active'),
                               next=updated)
                if reset_policy and (row['value']['state'] != updated['state']
                                     or row['value']['credentialId'] != updated['credentialId']):
                    device_policy.forget_node(self.owner, node_id, nodes=(witness,), client=self.client)
                else:
                    device_policy.commit_node_metadata(self.owner, witness, client=self.client)
                return updated
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise
                if reset_policy:
                    raise NodeStateError('Device changed during confirmation. Refresh before retrying.') from error

    # ── reads ───────────────────────────────────────────────────────────
    def get(self, node_id: str) -> dict | None:
        row = self._node_row(node_id) if valid_node_id(node_id) else None
        return None if row is None else row['value']

    def list(self) -> list[dict]:
        out, after = [], ''
        while True:
            rows = self.client().query('record.list', {
                'namespace': NAMESPACES['nodes'], 'prefix': self.prefix, 'after_key': after,
                'limit': _RECORD_LIST_LIMIT})
            if not isinstance(rows, list):
                raise StorageError('database_integrity', 'Invalid node registry page')
            for row in rows:
                if not isinstance(row, Mapping) or not str(row.get('key') or '').startswith(self.prefix):
                    raise StorageError('database_integrity', 'Node registry page escaped its owner')
                out.append(self._checked(row, 'nodeRecord')['value'])
            if len(rows) < _RECORD_LIST_LIMIT:
                return out
            after = rows[-1]['key']

    # ── pairing ─────────────────────────────────────────────────────────
    def record_pairing(self, *, node_id: str, credential_id: str, name: str, platform: str,
                       kind: str, previous_node_id: str = '') -> dict:
        """A redeemed pairing: a pending node, or a pending replacement of an active one."""
        now = time.time()
        pending = {'credentialId': credential_id, 'fingerprint': fingerprint(node_id, credential_id),
                   'pairedAt': now, 'name': name[:80], 'platform': platform[:40], 'kind': kind}
        self._bind_credential(credential_id, node_id)
        key = self._node_key(node_id)
        superseded = []
        while True:
            row = self._node_row(node_id)
            if row is not None and row['value']['state'] == 'active':
                value = dict(row['value'])
                previous = value.get('replacement')
                if previous and previous['credentialId'] != credential_id:
                    superseded.append(previous['credentialId'])
                value['replacement'] = pending
            else:
                if row is not None and row['value']['credentialId'] != credential_id:
                    superseded.append(row['value']['credentialId'])
                predecessor = self.get(previous_node_id) if previous_node_id != node_id else None
                value = {'version': 1, 'owner': self.owner, 'nodeId': node_id, 'state': 'pending',
                         'credentialId': credential_id, 'fingerprint': pending['fingerprint'],
                         'name': pending['name'], 'platform': pending['platform'], 'kind': kind,
                         'origin': 'pairing', 'pairedAt': now, 'confirmedAt': None,
                         'replacement': None, 'conflict': None,
                         'predecessor': previous_node_id if predecessor else None}
                if predecessor:
                    value['predecessorCredentialId'] = predecessor['credentialId']
            validate(value, 'nodeRecord')
            try:
                if row is None:
                    self._put(NAMESPACES['nodes'], key, value, 0)
                else:
                    from lib.desktop import device_policy
                    witness = dict(self.policy_witness(node_id, row, active=row['value']['state'] == 'active'),
                                   next=value)
                    device_policy.commit_node_metadata(self.owner, witness, client=self.client)
                break
            except StorageError as error:
                if error.code != 'database_conflict':
                    raise
        # An unconfirmed earlier pairing of the same node ID is superseded by this one.
        self._revoke(superseded, node_id)
        audit_log('desktop_node_paired', owner_user_id=self.owner, node_id=node_id,
                  credential=credential_id, replacement=value['state'] == 'active')
        notify_devices_changed(self.owner)
        return value

    # ── owner transitions ───────────────────────────────────────────────
    def confirm(self, node_id: str, *, name: str | None = None) -> dict:
        revoked, predecessors = [], []

        def change(value):
            if value.get('predecessor'):
                predecessors.append((value['predecessor'], value.pop('predecessorCredentialId', None)))
                value['predecessor'] = None
            if value['state'] == 'pending':
                value['state'] = 'active'
                value['confirmedAt'] = time.time()
            elif value.get('replacement'):
                new = value['replacement']
                revoked.append(value['credentialId'])
                value.update(credentialId=new['credentialId'], fingerprint=new['fingerprint'],
                             platform=new['platform'], kind=new['kind'], pairedAt=new['pairedAt'],
                             confirmedAt=time.time(), origin='pairing', replacement=None)
                value['name'] = new['name'] or value['name']
            elif not value.get('conflict') and not name:
                raise NodeStateError('This node is already confirmed.')
            value['conflict'] = None
            if name:
                value['name'] = name[:80]
            return value

        value = self._mutate(node_id, change, reset_policy=True)
        disconnected = self._revoke(revoked, node_id)
        for credential in revoked:
            self._unbind_credential(credential)
        for predecessor, credential in predecessors:
            # Transfer is an intentional re-pair of the exact predecessor. A
            # live twin or an older record with no binding is never retired here.
            if credential is None or _online(self.owner, predecessor):
                continue
            try:
                disconnected += self.forget(
                    predecessor, reason='replaced', expected_credential=credential,
                    transfer_to=(node_id, value['credentialId']))['disconnected']
            except NodeNotFound:
                pass
        audit_log('desktop_node_confirmed', owner_user_id=self.owner, node_id=node_id,
                  replaced=revoked, predecessors=[node for node, _ in predecessors])
        _wake(self.owner, node_id)
        notify_devices_changed(self.owner)
        return {'node_id': node_id, 'state': value['state'], 'revoked_credentials': revoked,
                'disconnected': disconnected}

    def reject(self, node_id: str) -> dict:
        """Refuse a pending node (forget it) or a pending replacement (keep the active node)."""
        row = self._node_row(node_id)
        if row is None:
            raise NodeNotFound(node_id)
        if row['value']['state'] == 'pending':
            return self.forget(node_id, reason='rejected')
        revoked = []

        def change(value):
            if not value.get('replacement'):
                raise NodeStateError('This node has no pairing waiting for confirmation.')
            revoked.append(value['replacement']['credentialId'])
            value['replacement'] = None
            return value

        value = self._mutate(node_id, change)
        self._revoke(revoked, node_id)
        for credential in revoked:
            self._unbind_credential(credential)
        audit_log('desktop_node_replacement_rejected', owner_user_id=self.owner, node_id=node_id)
        _wake(self.owner, node_id)
        notify_devices_changed(self.owner)
        return {'node_id': node_id, 'state': value['state'], 'revoked_credentials': revoked,
                'disconnected': []}

    def rename(self, node_id: str, name: str) -> dict:
        def change(value):
            if value['name'] == name[:80]:
                return None
            value['name'] = name[:80]
            return value
        value = self._mutate(node_id, change)
        notify_devices_changed(self.owner)
        return {'node_id': node_id, 'state': value['state']}

    def forget(self, node_id: str, *, reason: str = 'removed',
               expected_credential=None, transfer_to=None) -> dict:
        """Revoke every credential of the node, disconnect it and delete its record.

        The single removal path (owner DELETE, reject of a pending node,
        credential revocation); it also drops the node's Home ceiling and
        default-device marks (lib/desktop/device_policy.py).
        """
        row = self._node_row(node_id)
        if row is None:
            raise NodeNotFound(node_id)
        value = row['value']
        if expected_credential is not None and value['credentialId'] != expected_credential:
            raise NodeNotFound(node_id)
        credentials = [value['credentialId']] + (
            [value['replacement']['credentialId']] if value.get('replacement') else [])
        from lib.desktop import device_policy
        witness = dict(self.policy_witness(node_id, row), next=None)
        target = self.policy_target(transfer_to[0]) if transfer_to else None
        if transfer_to and (target is None or target[0]['credentialId'] != transfer_to[1]):
            raise NodeStateError('Replacement changed. Refresh the device list.')
        # Revoke before retiring the registry row. Credential cleanup is not
        # part of the policy transaction and may already have run on conflict.
        disconnected = self._revoke(credentials, node_id)
        try:
            if target is None:
                device_policy.forget_node(self.owner, node_id, nodes=(witness,), client=self.client)
            else:
                device_policy.transfer_node(self.owner, node_id, transfer_to[0],
                                            nodes=(witness, target[1]), client=self.client)
        except StorageError as error:
            if error.code != 'database_conflict':
                raise
            raise NodeStateError('Device changed during removal. Refresh before trying again.') from error
        for credential in credentials:
            self._unbind_credential(credential)
        _forget_presence(self.owner, node_id)
        audit_log('desktop_node_forgotten', owner_user_id=self.owner, node_id=node_id,
                  reason=reason, credentials=credentials)
        _wake(self.owner, node_id)
        notify_devices_changed(self.owner)
        return {'node_id': node_id, 'state': 'removed', 'revoked_credentials': credentials,
                'disconnected': disconnected}

    def forget_credential(self, credential_id: str) -> dict | None:
        """The registry side of revoking one credential: forget the node it was issued to.

        Revoking a pending replacement drops only the replacement.
        """
        row = self._get(NAMESPACES['credentials'], self.prefix + credential_id, 'credentialIndex')
        if row is None:
            return None
        node_id = row['value']['nodeId']
        record = self.get(node_id)
        if record is None:
            self._unbind_credential(credential_id)
            return None
        if record['credentialId'] == credential_id:
            try:
                return self.forget(node_id, reason='credential_revoked', expected_credential=credential_id)
            except NodeNotFound:
                return None
        if (record.get('replacement') or {}).get('credentialId') == credential_id:
            return self.reject(node_id)
        self._unbind_credential(credential_id)
        return None

    def _revoke(self, credentials, node_id: str) -> list:
        from lib.api_keys import revoke_key
        from lib.desktop import disconnect_credential
        disconnected = []
        for credential in credentials:
            try:
                revoke_key(credential, owner_user_id=self.owner, tenant_id=self.tenant_id)
            except LookupError:
                pass  # already gone
            disconnected.extend(disconnect_credential(credential, str(self.owner)))
        return disconnected

    # ── poll admission ──────────────────────────────────────────────────
    def admit_poll(self, *, credential_id: str, agent_id: str, frame: Mapping) -> Admission:
        """Decide whether one authenticated remote poll may reach the bridge."""
        if not valid_node_id(agent_id):
            return Admission('conflict', str(agent_id)[:128], reason='invalid_node_id')
        row = self._node_row(agent_id)
        value = row['value'] if row else None
        if value is not None and value['credentialId'] == credential_id:
            if value['state'] == 'pending':
                return Admission('pending', agent_id, value['fingerprint'])
            boot_id = frame.get('boot_id')
            boot = _observe_boot(self.owner, agent_id, boot_id if isinstance(boot_id, str) else '', frame)
            if boot == 'superseded':
                self._record_conflict(agent_id, 'duplicate_boot', agent_id)
                return Admission('conflict', agent_id, value['fingerprint'], 'duplicate_boot')
            return Admission('active', agent_id, value['fingerprint'], restarted=boot == 'restarted',
                             kind=value['kind'])
        if value is not None and (value.get('replacement') or {}).get('credentialId') == credential_id:
            return Admission('pending', agent_id, value['replacement']['fingerprint'])
        index = self._get(NAMESPACES['credentials'], self.prefix + credential_id, 'credentialIndex')
        if index is not None:
            bound = self.get(index['value']['nodeId'])
            if bound is not None and credential_id in (
                    bound['credentialId'], (bound.get('replacement') or {}).get('credentialId')):
                self._record_conflict(bound['nodeId'], 'credential_reused', agent_id)
                return Admission('conflict', agent_id, None, 'credential_reused')
        if value is not None:
            # Another credential owns this node ID: a copied or forged identity.
            self._record_conflict(agent_id, 'node_id_taken', agent_id)
            return Admission('conflict', agent_id, None, 'node_id_taken')
        return self._adopt(credential_id, agent_id, frame)

    def _adopt(self, credential_id: str, agent_id: str, frame: Mapping) -> Admission:
        from lib.api_keys import get_key_by_id
        key = get_key_by_id(credential_id, owner_user_id=self.owner, tenant_id=self.tenant_id)
        if key is None or key.get('revoked_at') or key.get('disabled'):
            # Revoked after this poll authenticated (a held pending poll the
            # owner rejected): the same refusal as a revoked credential.
            return Admission('revoked', agent_id, reason='credential_revoked')
        if (key.get('metadata') or {}).get('origin') == CREDENTIAL_ORIGIN:
            return Admission('unregistered', agent_id, reason='node_unregistered')
        self._bind_credential(credential_id, agent_id)
        value = {'version': 1, 'owner': self.owner, 'nodeId': agent_id, 'state': 'active',
                 'credentialId': credential_id, 'fingerprint': fingerprint(agent_id, credential_id),
                 'name': str(frame.get('name') or key.get('name') or agent_id)[:80],
                 'platform': str(frame.get('platform') or '')[:40],
                 'kind': 'server' if frame.get('kind') == 'server' else 'computer',
                 'origin': 'adopted', 'pairedAt': float(key.get('created_at') or time.time()),
                 'confirmedAt': time.time(), 'replacement': None, 'conflict': None,
                 'predecessor': None}
        validate(value, 'nodeRecord')
        try:
            from lib.desktop import device_policy
            witness = dict(self.policy_witness(agent_id, None), next=value)
            device_policy.forget_node(self.owner, agent_id, nodes=(witness,), client=self.client)
        except StorageError as error:
            if error.code != 'database_conflict':
                raise
            return self.admit_poll(credential_id=credential_id, agent_id=agent_id, frame=frame)
        audit_log('desktop_node_adopted', owner_user_id=self.owner, node_id=agent_id,
                  credential=credential_id)
        notify_devices_changed(self.owner)
        return self.admit_poll(credential_id=credential_id, agent_id=agent_id, frame=frame)

    def _record_conflict(self, node_id: str, reason: str, agent_id: str) -> None:
        changed = []

        def change(value):
            current = value.get('conflict') or {}
            if current.get('reason') == reason and current.get('agentId') == agent_id[:256]:
                return None
            value['conflict'] = {'reason': reason, 'agentId': agent_id[:256], 'at': time.time()}
            changed.append(True)
            return value

        try:
            self._mutate(node_id, change)
        except NodeNotFound:
            return
        if changed:
            audit_log('desktop_node_identity_conflict', owner_user_id=self.owner, node_id=node_id,
                      reason=reason, agent_id=agent_id[:128])
            notify_devices_changed(self.owner)


__all__ = ['Admission', 'CONTRACT', 'CREDENTIAL_ORIGIN', 'DEVICES_PUSH_TASK_ID', 'NAMESPACES',
           'NodeNotFound', 'NodeRegistry', 'NodeStateError', 'fingerprint', 'notify_devices_changed',
           'transport_facts', 'valid_node_id', 'wait_for_confirmation']
