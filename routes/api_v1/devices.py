"""Device routing policy routes: Home permission ceilings and default devices.

Shapes: contracts/device_routing_v1.schema.json. Every write commits the
owner's policy record, drops this process's cached copy (inside the commit
path), repeats the invalidation on the execution incarnation and its shards
through the owner command control ``device_policy_reconcile`` and publishes
the ``devices.changed`` wake hint. The response's ``execution`` field reports
``applied``, ``refused``, ``unavailable`` (logged; running Turns of an
unreachable executor keep their cached policy until it answers) or
``not_split``.
"""
from __future__ import annotations

from quart import Blueprint, current_app

from lib.api_response import api_bad_request, api_error, api_not_found, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import BadRequest, parse_body
from lib.storage.errors import StorageError

from .auth import request_principal, require_auth

logger = get_logger(__name__)

api_v1_devices_bp = Blueprint('api_v1_devices', __name__)


def _schema(name: str) -> dict:
    from lib.desktop.device_policy import CONTRACT
    from lib.openapi import inline_schema_definition
    return inline_schema_definition(CONTRACT, name)


def _json_response(name: str, description: str) -> dict:
    return {'200': {'description': description,
                    'content': {'application/json': {'schema': _schema(name)}}}}


def _failure(error: Exception):
    from lib.storage import http_status_for_storage_error
    if isinstance(error, StorageError):
        return api_error(error, status=http_status_for_storage_error(error))
    return api_bad_request(str(error))


def _follow_in_execution(owner: int) -> str:
    from lib.tasks_pkg.command_control_runtime import execution_device_policy_reconcile
    from lib.tasks_pkg.command_control_transport import CommandControlUnavailable
    try:
        result = execution_device_policy_reconcile(current_app, user_id=owner)
    except CommandControlUnavailable as exc:
        logger.warning('[Devices] execution host did not drop the device policy: %s', exc)
        return 'unavailable'
    if result is None:
        return 'not_split'
    return 'applied' if result.get('applied') else 'refused'


def _owned_node(owner: int, node_id: str) -> dict | None:
    """The owner's node by ID: a known bridge agent, else one with a stored ceiling."""
    from lib.desktop import list_agents
    from lib.desktop.device_policy import policy
    for agent in list_agents(user_id=str(owner)):
        if agent['agent_id'] == node_id:
            return agent
    record, _rev = policy(owner)
    return {'agent_id': node_id, 'capabilities': None} if node_id in record['ceilings'] else None


def _owned_browser(owner: int, client_id: str) -> bool:
    from lib.browser.queue import get_connected_clients
    return any(row['client_id'] == client_id
               for row in get_connected_clients(owner_user_id=str(owner)))


@api_v1_devices_bp.route('/api/v1/devices/permissions', methods=['GET'])
@require_auth
@api_meta(
    summary="The Home ceiling, device switch and effective permissions of the caller's devices",
    description=('Shape: PermissionsListing in contracts/device_routing_v1.schema.json. '
                 'Covers every device known to the bridge and every device with a stored '
                 'ceiling; a device without one is read-only.'),
    tags=['capabilities'],
    responses=_json_response('PermissionsListing', 'Permissions of every known device'),
)
def device_permissions():
    """SYNC on purpose: the policy read blocks on storage I/O."""
    from lib.desktop import list_agents
    from lib.desktop.device_policy import permissions_listing
    owner = request_principal().require_owner(context='device permissions')
    try:
        return api_ok(permissions_listing(owner, list_agents(user_id=str(owner))))
    except (StorageError, ValueError) as error:
        return _failure(error)


@api_v1_devices_bp.route('/api/v1/devices/<node_id>/ceiling', methods=['PUT'])
@require_auth
@api_meta(
    summary="Set the Home permission ceiling of one of the caller's devices",
    description=('Body: CeilingRequest; response: DevicePermissions plus execution '
                 '(contracts/device_routing_v1.schema.json). Effective permissions are this '
                 'ceiling AND the device\'s own switch; raising the ceiling never raises the '
                 'device. Applies to running Turns on their next device call.'),
    tags=['capabilities'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': _schema('CeilingRequest')}}},
)
def device_ceiling(node_id):
    from lib.desktop.device_policy import permissions_view, set_ceiling
    owner = request_principal().require_owner(context='device permissions')
    try:
        body = parse_body(strict=True, exact=('tiers',))
        node = _owned_node(owner, node_id)
        if node is None:
            return api_not_found('Device not found')
        record, rev = set_ceiling(owner, node_id, body.get('tiers'))
        view = permissions_view(owner, node_id, node.get('capabilities'), record=record,
                                rev=rev, known=node.get('capabilities') is not None)
    except BadRequest as error:
        return api_bad_request(str(error))
    except (StorageError, ValueError) as error:
        return _failure(error)
    return api_ok({**view, 'execution': _follow_in_execution(owner)})


@api_v1_devices_bp.route('/api/v1/devices/defaults', methods=['GET'])
@require_auth
@api_meta(
    summary="The caller's default device per kind",
    description=('Shape: DefaultsResponse in contracts/device_routing_v1.schema.json. A call '
                 'that names no device runs on the default of its kind.'),
    tags=['capabilities'],
    responses=_json_response('DefaultsResponse', 'Default devices'),
)
def device_defaults():
    from lib.desktop.device_policy import policy
    owner = request_principal().require_owner(context='device defaults')
    try:
        record, rev = policy(owner)
    except (StorageError, ValueError) as error:
        return _failure(error)
    return api_ok({'defaults': record['defaults'], 'rev': rev})


@api_v1_devices_bp.route('/api/v1/devices/defaults', methods=['PUT'])
@require_auth
@api_meta(
    summary="Mark or clear the caller's default device per kind",
    description=('Body: DefaultsRequest (null clears, omitted kinds are unchanged); '
                 'response: DefaultsResponse plus execution. computer and server name one of '
                 'the caller\'s nodes, browser one of the caller\'s connected browser clients.'),
    tags=['capabilities'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': _schema('DefaultsRequest')}}},
)
def update_device_defaults():
    from lib.desktop.device_policy import set_defaults
    owner = request_principal().require_owner(context='device defaults')
    try:
        body = parse_body(strict=True, exact=('computer', 'server', 'browser'))
        for kind, node_id in body.items():
            if node_id is None:
                continue
            owned = (_owned_browser(owner, node_id) if kind == 'browser'
                     else _owned_node(owner, node_id) is not None)
            if not owned:
                return api_not_found(f'Device not found for {kind}')
        record, rev = set_defaults(owner, body)
    except BadRequest as error:
        return api_bad_request(str(error))
    except (StorageError, ValueError) as error:
        return _failure(error)
    return api_ok({'defaults': record['defaults'], 'rev': rev,
                   'execution': _follow_in_execution(owner)})
