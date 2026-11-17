"""
Desktop Agent Bridge — Server-side endpoint for local machine control.

Mirrors the architecture of routes/browser.py:
  - LLM calls tool → command queued
  - Desktop Agent polls /api/desktop/poll → picks up commands, returns results
"""

import asyncio
import time

from quart import Blueprint, jsonify

from lib.desktop_wire import limit, protocol_version

from lib.log import get_logger
from lib.request_parser import async_parse_body
from routes._bridge_caller import (
    bridge_unauthorized as _bridge_unauthorized,
    resolve_bridge_caller as _resolve_bridge_caller,
)

logger = get_logger(__name__)

desktop_bp = Blueprint('desktop', __name__)

# Bridge caller resolution lives in routes/_bridge_caller.py, shared with
# the browser bridge so the two identity layers are literally the same
# object (B0 §5.3 / ). Auth order (RWA P4a 约束②第三条):
# A remote caller must present an owner-scoped agents:bridge credential. The
# packaged app's in-process capability is accepted only on this desktop poll.

# The queue lives below routes so tool handlers never import delivery code.
from lib.desktop import (  # noqa: E402
    register_agent,
    resolve_results,
    resolve_streams,
    take_pending_commands_async,
)


_NODE_REFUSAL_HINTS = {
    'credential_reused': 'this credential belongs to another node; pair this host again',
    'node_id_taken': 'another device already uses this node ID; pair this host again',
    'duplicate_boot': 'another live process presents this node ID; stop it or give this '
                      'process its own node config (TOFU_DESKTOP_CONFIG)',
    'invalid_node_id': 'node IDs are 1-128 letters, digits, dot, dash or underscore',
    'node_unregistered': 'the Home has no record of this pairing; pair this host again',
}


async def _node_admission(agent_id, frame, owner, key_id, *, held, negotiated):
    """Trust gate of the Home's node registry (lib/desktop/node_registry.py).

    Only an active node bound to the presented credential reaches the bridge.
    A node awaiting the owner's confirmation gets an empty, held answer that
    ends early when the owner confirms or rejects it. The packaged app's
    in-process node (process capability) has no registry record.
    """
    from lib.bridge_auth import _PROCESS_AGENT_KEY_ID
    if key_id == _PROCESS_AGENT_KEY_ID:
        return None
    from lib.desktop.bridge import POLL_WAIT_TIMEOUT
    from lib.desktop.node_registry import NodeRegistry, wait_for_confirmation
    from quart import g
    context = getattr(g, 'bridge_auth_context', None)
    registry = NodeRegistry(int(owner), tenant_id=getattr(context, 'tenant_id', None))

    def admit():
        return registry.admit_poll(credential_id=key_id, agent_id=agent_id, frame=frame)

    admission = await asyncio.to_thread(admit)
    if admission.state == 'pending' and held:
        await wait_for_confirmation(int(owner), agent_id, POLL_WAIT_TIMEOUT)
        admission = await asyncio.to_thread(admit)
    if admission.admitted:
        # The Home registry's kind (declared at pairing, owner-confirmed) is the
        # one placement uses (lib/desktop/routing.device_kind).
        frame['kind'] = admission.kind
        if admission.restarted:
            from lib.desktop.bridge import settle_lost_claims
            settle_lost_claims(agent_id, owner, frame.get('active_command_ids'))
        return None
    if admission.state == 'pending':
        response = {'commands': [], 'node': {'state': 'pending_confirmation',
                                             'fingerprint': admission.fingerprint}}
        if negotiated:
            response.update(protocol_version=protocol_version(), server_time=time.time())
        return jsonify(response)
    if admission.state == 'revoked':
        return _bridge_unauthorized()
    if admission.state == 'conflict':
        return jsonify({'error': 'desktop_node_identity_conflict', 'reason': admission.reason,
                        'hint': _NODE_REFUSAL_HINTS.get(admission.reason, '')}), 409
    return jsonify({'error': 'desktop_node_unregistered', 'reason': admission.reason,
                    'hint': _NODE_REFUSAL_HINTS.get(admission.reason, '')}), 409


# ══════════════════════════════════════════════════════════
#  Poll Endpoint — Desktop Agent calls this
# ══════════════════════════════════════════════════════════

@desktop_bp.route('/api/desktop/poll', methods=['POST'])
async def desktop_poll():
    _auth_ok, _bridge_user, _bridge_key = _resolve_bridge_caller('desktop')
    if not _auth_ok:
        return _bridge_unauthorized()
    body = await async_parse_body(exact=('agent', 'results', 'streams',))
    agent_frame = body.get('agent')
    if not isinstance(agent_frame, dict) or not agent_frame.get('agent_id'):
        return jsonify({
            'error': 'desktop_agent_identity_required',
            'hint': 'pair this device again with a current agent',
        }), 400
    version = agent_frame.get('protocol_version')
    if version is not None and (type(version) is not int or version != protocol_version()):
        return jsonify({'error': 'desktop_protocol_upgrade_required',
                        'supported_protocol_version': protocol_version()}), 426
    if version == protocol_version():
        slots = agent_frame.get('execution_slots')
        active = agent_frame.get('active_command_ids')
        if (type(slots) is not int or slots < 0 or not isinstance(active, list)
                or len(active) > limit('legacy_execution_ids')
                or any(not isinstance(cid, str) or not cid or len(cid) > 256 for cid in active)):
            return jsonify({'error': 'desktop_admission_frame_invalid'}), 400
    agent_id = str(agent_frame['agent_id'])
    refusal = await _node_admission(agent_id, agent_frame, _bridge_user, _bridge_key,
                                    held=not (body.get('results') or body.get('streams')),
                                    negotiated=version == protocol_version())
    if refusal is not None:
        return refusal
    connection = register_agent(
        agent_id,
        agent_frame,
        user_id=_bridge_user,
        key_id=_bridge_key,
    )

    # Settle results only after binding the poll to its authenticated owner
    # and stable device identity.
    resolved = resolve_results(
        body.get('results', []), agent_id=agent_id, user_id=_bridge_user)
    if resolved:
        logger.info('[Desktop] resolved %d command results', resolved)
    # 1a) RWA P2: streamed-command output frames (reassembly dedupes by seq)
    stream_frames = resolve_streams(
        body.get('streams', []), agent_id=agent_id, user_id=_bridge_user)
    if stream_frames:
        logger.debug('[Desktop] ingested %d stream chunks', stream_frames)

    # Once per connection epoch: wake the conversations waiting for this device
    # (docs/modules/remote_execution.md, "Waiting for an offline device").
    if connection is not None:
        from lib.desktop.device_waits import deliver_device_waits
        await asyncio.to_thread(deliver_device_waits, connection)

    # 2) Long-poll for pending commands. Async-native wait releases the worker
    #    thread for the window (see lib.desktop.bridge.take_pending_commands_async)
    #    and hands the agent a command the instant it is queued.
    pending = await take_pending_commands_async(
        agent_id=agent_id, user_id=_bridge_user,
        timeout=0 if body.get('results') or body.get('streams') else None)
    if pending:
        logger.info('[Desktop] sending %d commands to agent %s: %s',
                    len(pending), agent_id,
                    [c['type'] for c in pending])
    response = {'commands': pending}
    if version == protocol_version():
        response.update(protocol_version=protocol_version(), server_time=time.time())
    return jsonify(response)


# Status endpoint moved to routes/api_v1/desktop.py — read state via the
# lib.desktop helpers (last_poll_time / pending_commands_count /
# is_desktop_agent_connected).
#
# Tool execution lives with the other task-loop handlers:
# lib/tasks_pkg/handlers/misc/_agents.py::_handle_desktop_tool (registered
# against DESKTOP_TOOL_NAMES via tool_registry). The wire contract is that
# the command ``type`` IS the full tool name — see
# tests/desktop/test_desktop_cmdtype_parity.py.
