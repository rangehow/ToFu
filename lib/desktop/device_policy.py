"""Home permission ceilings and owner-marked default devices (contracts/device_routing_v1.schema.json).

Responsibility: the one owner of the durable, owner-scoped device policy a Home
applies to tool calls: per-node ceilings (decision 6: Home ceiling AND the
device's own switch; a node without a stored ceiling is read-only, so a new
device starts read-only) and the owner-marked default device per kind
(decision 7). One record per owner in portable record CAS; its record version
is the policy ``rev``.

Caching: each process keeps the owner's record after the first read and drops
it on an explicit invalidation, never by age. The API process invalidates on
its own commits; the execution incarnation and its shards are invalidated
through the owner command control ``device_policy_reconcile``
(``lib/tasks_pkg/command_controls.py``). A failed storage read fails closed:
the enforcement boundary sees a read-only ceiling.

Entry points: home_tiers, effective_tiers, permissions_view, permissions_listing,
defaults, set_ceiling, set_defaults, forget_node, invalidate, ceiling_tiers.
Dependencies: lib.storage (record.*), lib.desktop_wire.node_protocol (tiers).
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import threading
import uuid

from lib.agent_core.storage_errors import StorageError
from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                       'device_routing_v1.schema.json').read_text(encoding='utf-8'))
NAMESPACE = CONTRACT['x-namespace']
KINDS = tuple(CONTRACT['x-kinds'])
LIVENESS_WINDOW_S = CONTRACT['x-liveness-window-seconds']
CEILING_TIERS = tuple(CONTRACT['$defs']['Ceiling']['required'])


class PolicyNotFound(LookupError):
    """The node or device is not the owner's."""


@lru_cache(maxsize=None)
def _validator(name: str):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + name})


def validate(value, name: str):
    error = next(_validator(name).iter_errors(value), None)
    if error is not None:
        location = '.'.join(str(part) for part in error.absolute_path) or name
        raise ValueError(f'Invalid device policy {name} ({location}): {error.message}')
    return value


def _empty(owner: int) -> dict:
    return {'version': 1, 'owner': owner, 'ceilings': {},
            'defaults': {kind: None for kind in KINDS}}


def _default_client(write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


_cache: dict[int, tuple[dict, int]] = {}
_cache_lock = threading.Lock()
_generation = [0]


def invalidate(owner=None) -> None:
    """Drop the cached policy of one owner (or every owner)."""
    with _cache_lock:
        _generation[0] += 1
        if owner is None:
            _cache.clear()
        else:
            _cache.pop(require_user_id(owner, context='device policy'), None)


def _read(owner: int, client) -> tuple[dict, int]:
    row = client().query('record.get', {'namespace': NAMESPACE, 'key': str(owner)})
    if row is None:
        return _empty(owner), 0
    try:
        value = validate(row['value'], 'PolicyRecord')
        if value['owner'] != owner or type(row['version']) is not int or row['version'] < 1:
            raise ValueError('Device policy record escaped its owner')
    except (KeyError, TypeError, ValueError) as error:
        raise StorageError('database_integrity', 'Invalid device policy record') from error
    return value, row['version']


def policy(owner, *, client=None) -> tuple[dict, int]:
    """``(record, rev)`` for one owner; ``rev`` 0 means nothing was ever stored."""
    owner = require_user_id(owner, context='device policy')
    if client is not None:
        return _read(owner, client)
    with _cache_lock:
        cached = _cache.get(owner)
        generation = _generation[0]
    if cached is not None:
        return deepcopy(cached[0]), cached[1]
    value, rev = _read(owner, _default_client)
    with _cache_lock:
        # An invalidation that raced this read wins: keep nothing stale.
        if _generation[0] == generation:
            _cache[owner] = (value, rev)
    return deepcopy(value), rev


def _mutate(owner, change, *, client=None) -> tuple[dict, int]:
    """Apply ``change(record)`` under CAS; returns the committed ``(record, rev)``."""
    owner = require_user_id(owner, context='device policy')
    reader = client or _default_client
    while True:
        value, version = _read(owner, reader)
        updated = change(deepcopy(value))
        if updated is None or updated == value:
            return value, version
        validate(updated, 'PolicyRecord')
        try:
            receipt = reader(write=True).command('record.put', {
                'namespace': NAMESPACE, 'key': str(owner), 'value': updated,
                'expected_version': version,
            }, 'device-policy:' + uuid.uuid4().hex)
        except StorageError as error:
            if error.code == 'database_conflict':
                continue
            raise
        rev = int((receipt or {}).get('version') or version + 1)
        invalidate(owner)
        _notify_changed(owner, rev)
        return updated, rev


def _notify_changed(owner: int, rev=None) -> None:
    """Wake hint ``devices.changed`` on the owner's notify channel (never authority)."""
    try:
        from lib.agent_core.events import build_push_frame
        from lib.agent_core.push import push_event
        fields = {'userId': owner}
        if rev is not None:
            fields['rev'] = int(rev)
        push_event('notify', '__devices__', build_push_frame('devices.changed', **fields),
                   user_id=owner)
    except Exception as error:
        logger.debug('[DevicePolicy] devices.changed push skipped: %s', error)


def notify_devices_changed(owner) -> None:
    """Liveness transitions publish the same hint, without a policy rev."""
    try:
        owner = require_user_id(owner, context='device policy')
    except ValueError:
        return
    _notify_changed(owner)


# ── tiers ─────────────────────────────────────────────────────────────────

def ceiling_tiers(record: dict, node_id: str) -> dict:
    """The Home column: read plus the stored ceiling; read-only when none is stored."""
    stored = (record.get('ceilings') or {}).get(node_id) or {}
    tiers = {'read': True}
    for tier in CEILING_TIERS:
        tiers[tier] = bool(stored.get(tier))
    return tiers


def device_tiers(capabilities) -> dict:
    from lib.desktop_wire.node_protocol import granted_tiers
    return granted_tiers(capabilities)


def intersect(home: dict, device: dict) -> dict:
    return {tier: bool(home.get(tier)) and bool(device.get(tier)) for tier in home}


def home_tiers(owner, node_id: str) -> dict:
    """The Home ceiling for one node; a storage failure reads as read-only (fail closed)."""
    try:
        record, _rev = policy(owner)
    except (StorageError, ValueError) as error:
        logger.warning('[DevicePolicy] ceiling unavailable for node %s; read-only: %s',
                       str(node_id)[:8], error)
        record = {'ceilings': {}}
    return ceiling_tiers(record, node_id)


def effective_tiers(owner, node_id: str, capabilities) -> dict:
    return intersect(home_tiers(owner, node_id), device_tiers(capabilities))


def permissions_view(owner, node_id: str, capabilities=None, *, record=None, rev=None,
                     known=None) -> dict:
    """DevicePermissions: Home column, device column (None if never polled), effective."""
    if record is None:
        record, rev = policy(owner)
    home = ceiling_tiers(record, node_id)
    device = None if (known is False or (known is None and capabilities is None)) \
        else device_tiers(capabilities)
    effective = intersect(home, device) if device is not None \
        else {tier: tier == 'read' for tier in home}
    return validate({'node_id': node_id, 'home': home, 'device': device,
                     'effective': effective, 'rev': int(rev or 0)}, 'DevicePermissions')


def permissions_listing(owner, agents) -> dict:
    """PermissionsListing over the owner's known agents plus every stored ceiling."""
    record, rev = policy(owner)
    rows = {}
    for agent in agents:
        rows[agent['agent_id']] = permissions_view(
            owner, agent['agent_id'], agent.get('capabilities'), record=record, rev=rev,
            known=True)
    for node_id in record['ceilings']:
        rows.setdefault(node_id, permissions_view(owner, node_id, record=record, rev=rev,
                                                  known=False))
    return validate({'permissions': rows, 'rev': rev,
                     'liveness_window_s': LIVENESS_WINDOW_S}, 'PermissionsListing')


# ── owner commands ────────────────────────────────────────────────────────

def set_ceiling(owner, node_id: str, tiers: dict, *, client=None) -> tuple[dict, int]:
    """Replace one node's ceiling. ``tiers`` follows CeilingRequest.tiers."""
    validate({'tiers': tiers}, 'CeilingRequest')
    if not isinstance(node_id, str) or not 1 <= len(node_id) <= 256:
        raise ValueError('node_id is required')
    ceiling = {tier: bool(tiers.get(tier)) for tier in CEILING_TIERS}

    def change(record):
        record['ceilings'][node_id] = ceiling
        return record
    return _mutate(owner, change, client=client)


def defaults(owner) -> dict:
    record, _rev = policy(owner)
    return dict(record['defaults'])


def set_defaults(owner, changes: dict, *, client=None) -> tuple[dict, int]:
    """Set or clear (None) the default device of each named kind."""
    validate(changes, 'DefaultsRequest')

    def change(record):
        record['defaults'].update(changes)
        return record
    return _mutate(owner, change, client=client)


def forget_node(owner, node_id: str, *, client=None) -> tuple[dict, int]:
    """A removed or re-paired node starts over read-only and stops being a default."""
    def change(record):
        record['ceilings'].pop(node_id, None)
        for kind, value in record['defaults'].items():
            if value == node_id:
                record['defaults'][kind] = None
        return record
    return _mutate(owner, change, client=client)


__all__ = [
    'CEILING_TIERS', 'CONTRACT', 'KINDS', 'LIVENESS_WINDOW_S', 'NAMESPACE', 'PolicyNotFound',
    'ceiling_tiers', 'defaults', 'device_tiers', 'effective_tiers', 'forget_node',
    'home_tiers', 'intersect', 'invalidate', 'notify_devices_changed', 'permissions_listing',
    'permissions_view', 'policy', 'set_ceiling', 'set_defaults', 'validate',
]
