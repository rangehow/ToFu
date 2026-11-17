"""Device placement for tool calls: the Home resolves each call to one device.

Responsibility: every device tool call names a device or resolves to exactly
one before it is sent (contracts/device_routing_v1.schema.json ``x-resolution``).
A named device runs there or is refused; an unnamed call takes the owner-marked
default for the tool's kind (decision 7), else the owner's only online device;
several online without a default are refused. Nothing ever picks the most
recently seen device, and a refused or lost call is never retried elsewhere.

The resolution reads the bridge registry (relayed to the bridge owner in a
split deployment) and the owner's cached device policy, so the execution
host and every execution shard resolve identically.

Entry points: route_device_call, DeviceRoute, RoutingRefusal, device_kind,
call_idempotency_key, has_reachable_device.
Dependencies: lib.desktop.bridge (owner-scoped presence), lib.desktop.device_policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from lib.log import get_logger

logger = get_logger(__name__)

DESKTOP_KIND = 'computer'


@dataclass(frozen=True)
class RoutingRefusal:
    code: str
    message: str
    # The matched device when it is known but offline, so the caller can offer
    # await_device for it.
    agent: dict | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class DeviceRoute:
    agent: dict
    reason: str  # 'named', 'default' or 'only_online'


def device_kind(agent: dict) -> str:
    """A node's kind: the Home registry's when known, else its declared frame, else computer."""
    kind = agent.get('kind')
    return kind if kind in ('computer', 'server') else DESKTOP_KIND


def _label(agent) -> str:
    return f"{agent.get('name') or agent['agent_id']} (agent {agent['agent_id']})"


def _owner_agents(user_id) -> list:
    from lib.desktop import bridge
    return bridge.list_agents(user_id=str(user_id or ''))


def route_device_call(user_id, *, kind: str = DESKTOP_KIND, selector=None
                      ) -> DeviceRoute | RoutingRefusal:
    """Resolve one call to one of the owner's online devices, or a typed refusal."""
    from lib.desktop import bridge
    from lib.desktop import device_policy
    try:
        agents = _owner_agents(user_id)
    except Exception as error:  # RelayUnavailable: the bridge owner did not answer.
        logger.warning('[Routing] device registry unavailable: %s', error)
        return RoutingRefusal('device_unavailable',
                              f'The device registry is unavailable ({error}); nothing was sent.')
    online = [agent for agent in agents if agent['online']]
    listing = bridge._devices_listing(online)
    if isinstance(selector, str) and selector.strip():
        matches = bridge.match_devices(agents, selector.strip())
        if len(matches) > 1:
            return RoutingRefusal('device_ambiguous', (
                f'Several of your devices match {selector.strip()!r}: '
                + ', '.join(_label(a) for a in matches)
                + '. Retry with device set to one agent ID.'))
        if not matches:
            return RoutingRefusal('device_not_found', (
                f'None of your devices has the agent ID or name {selector.strip()!r}. ' + listing))
        agent = matches[0]
        if not agent['online']:
            return RoutingRefusal('device_offline',
                                  f'Device {_label(agent)} is offline. ' + listing, agent)
        return DeviceRoute(agent, 'named')
    try:
        default_id = device_policy.defaults(user_id).get(kind)
    except Exception as error:
        logger.warning('[Routing] default devices unavailable: %s', error)
        return RoutingRefusal('device_unavailable', (
            f'The device policy is unavailable ({error}); name the device explicitly.'))
    if default_id:
        agent = next((a for a in agents if a['agent_id'] == default_id), None)
        if agent is None or not agent['online']:
            known = agent or {'agent_id': default_id, 'name': ''}
            return RoutingRefusal('device_offline', (
                f'Your default {kind} {_label(known)} is offline. It is not replaced by '
                'another device; name one explicitly or wait for it. ' + listing), agent or known)
        return DeviceRoute(agent, 'default')
    if len(online) == 1:
        return DeviceRoute(online[0], 'only_online')
    if not online:
        return RoutingRefusal('device_none', 'None of your devices is online; nothing was sent.')
    return RoutingRefusal('device_ambiguous', (
        f'{len(online)} of your devices are online, the call names none and no default '
        f'{kind} is marked, so it was not sent. Retry with device set to one agent ID or '
        'name, or ask the owner to mark a default on the Devices page. ' + listing))


def has_reachable_device(user_id) -> bool:
    """Whether the owner has an online device: the desktop tool family is offered only then."""
    try:
        return any(agent['online'] for agent in _owner_agents(user_id))
    except Exception as error:
        logger.debug('[Routing] device registry unavailable for exposure: %s', error)
        return False


def call_idempotency_key(task: dict, round_num, tool_call_id: str, fn_name: str) -> str:
    """The command id of one tool call: the same call always maps to the same key.

    The Home attaches a repeated key to the command already queued, and the node
    journal returns the recorded outcome instead of running it again
    (``lib/desktop_agent/_journal.py``), so a resumed or replayed call never
    duplicates its side effect.
    """
    identity = [str(task.get('_userId') or ''), str(task.get('convId') or ''),
                str(task.get('id') or ''), str(round_num), str(tool_call_id), fn_name]
    digest = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    return 'call-' + digest[:40]


__all__ = ['DESKTOP_KIND', 'DeviceRoute', 'RoutingRefusal', 'call_idempotency_key',
           'device_kind', 'has_reachable_device', 'route_device_call']
