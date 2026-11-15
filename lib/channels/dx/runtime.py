"""The one Node the 大象 launcher runs: one setting, verified once per file.

Responsibility: resolve the Node executable of the bridge and of its SSO step
from one durable setting, ``TOFU_NODE`` (the project ``.env`` value the server
loads into its environment at boot; else an explicit ``MTSSO_NODE_EXECUTABLE``;
else the SSO adapter's own choice from ``PATH``), verify it with the SSO
adapter's ``node_runtime`` (official engine floor, typed failures) and against
the bridge's own requirement (contract ``x-dx.bridgeNode``), and hand exactly
that binary to every launcher child: ``DX_NODE`` (the bridge, which dx-tofu.sh
prefers), ``MTSSO_NODE_EXECUTABLE`` (the SSO step) and ``MTSSO_NODE_VERIFIED``
(the verification, so the SSO step never probes that binary again). A verified
file is trusted while its identity (path, device, inode, size, mtime) is
unchanged; a changed file is verified again. docs/CHANNELS.md C6 owns the
behavior.

Entry points: ``bridge_node(environment)`` (``BridgeNode``), ``meets_bridge_floor``.
Failures are ``ChannelError``: ``runtime_unavailable`` for a missing, too-old or
unrunnable Node, which no login can fix, and ``runtime_unverified`` when the
check could not finish (the version probe outlived its deadline under host
load, or the adapter query failed), which the supervisor's next tick retries.
Dependencies: ``lib.channels.dx.login.adapter_answer`` (the SSO adapter in a
child process), ``lib.channels.model.DX``.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping

from lib.channels.dx.login import adapter_answer
from lib.channels.model import DX, ChannelError
from lib.log import get_logger

logger = get_logger(__name__)

NODE_UNAVAILABLE = 'runtime_unavailable'
NODE_UNVERIFIED = 'runtime_unverified'
# Where the launcher's Node comes from, in precedence order; PATH means the SSO
# adapter's own choice.
_SETTINGS = ('TOFU_NODE', 'MTSSO_NODE_EXECUTABLE')
_ENV_PROXY_SINCE = tuple(sorted(tuple(int(part) for part in version.split('.'))
                                for version in DX['bridgeNode']['envProxySince']))
_VERSION = re.compile(r'v(\d+)\.(\d+)\.(\d+)')
# The adapter's verdict as JSON: its hand-off record, or its typed refusal.
_NODE_RUNTIME = ('import json\n'
                 'from tofu_mtsso_auth import OfficialAuthError, node_runtime\n'
                 'try:\n'
                 '    print(json.dumps({"runtime": node_runtime()}))\n'
                 'except OfficialAuthError as error:\n'
                 '    print(json.dumps({"refusal": {"code": error.code, "message": error.message}}))\n')
_LOCK = threading.Lock()
# (setting, configured path) -> its verified Node, kept while the file is unchanged.
_VERIFIED: dict[tuple[str, str], 'BridgeNode'] = {}


@dataclass(frozen=True)
class BridgeNode:
    """A verified Node: realpath, ``--version``, file identity and the setting that named it."""
    path: str
    version: str
    identity: tuple[int, int, int, int]
    source: str

    def launcher_environment(self) -> dict[str, str]:
        record = {'path': self.path, 'identity': list(self.identity), 'version': self.version}
        return {'DX_NODE': self.path, 'MTSSO_NODE_EXECUTABLE': self.path,
                'MTSSO_NODE_VERIFIED': json.dumps(record, separators=(',', ':'))}

    def describe(self) -> str:
        return f'{self.path} {self.version} (from {self.source})'


def _floor_text() -> str:
    lines = ['.'.join(map(str, floor)) for floor in _ENV_PROXY_SINCE]
    return ' or '.join(f'{floor}+ on {floor.split(".")[0]}.x' for floor in lines[:-1]) + f' or {lines[-1]}+'


def meets_bridge_floor(version: str) -> bool:
    """Whether ``version`` honours NODE_USE_ENV_PROXY (contract ``x-dx.bridgeNode``)."""
    found = _VERSION.match(version or '')
    if not found:
        return False
    parts = tuple(int(group) for group in found.groups())
    line = [floor for floor in _ENV_PROXY_SINCE if floor[0] == parts[0]]
    return parts >= line[0] if line else parts[0] > _ENV_PROXY_SINCE[-1][0]


def _setting(environment: Mapping[str, str]) -> tuple[str, str]:
    for key in _SETTINGS:
        value = (environment.get(key) or '').strip()
        if value:
            return key, value
    return 'PATH', ''


def _identity(path: str) -> tuple[int, int, int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _still_verified(node: BridgeNode, configured: str) -> bool:
    if configured and os.path.realpath(configured) != node.path:
        return False  # the setting now names another file (a re-pointed link)
    return _identity(node.path) == node.identity


def _verify(environment: Mapping[str, str], source: str, configured: str) -> BridgeNode:
    named = f'{source}={configured}' if configured else 'PATH'
    query = {key: value for key, value in environment.items()
             if key not in ('MTSSO_NODE_EXECUTABLE', 'MTSSO_NODE_VERIFIED')}
    if configured:
        query['MTSSO_NODE_EXECUTABLE'] = configured
    answer: dict[str, Any] = adapter_answer(_NODE_RUNTIME, query)
    refusal = answer.get('refusal')
    if isinstance(refusal, dict):
        code = str(refusal.get('code') or 'SSO_NODE_UNKNOWN')
        state = NODE_UNVERIFIED if code == 'SSO_NODE_PROBE_TIMED_OUT' else NODE_UNAVAILABLE
        raise ChannelError(state, f'{code}: {refusal.get("message") or ""} ({named})'.strip())
    runtime = answer.get('runtime')
    if not (isinstance(runtime, dict) and isinstance(runtime.get('path'), str)
            and isinstance(runtime.get('version'), str) and isinstance(runtime.get('identity'), list)
            and len(runtime['identity']) == 4 and all(isinstance(value, int) for value in runtime['identity'])):
        raise ChannelError(NODE_UNVERIFIED, f'SSO adapter gave no Node verdict ({named})')
    node = BridgeNode(runtime['path'], runtime['version'], tuple(runtime['identity']), source)
    if not meets_bridge_floor(node.version):
        raise ChannelError(NODE_UNAVAILABLE, (
            f'DX_NODE_TOO_OLD: {node.describe()} cannot run the 大象 bridge, which needs Node '
            f'{_floor_text()} (NODE_USE_ENV_PROXY); set TOFU_NODE to such a Node'))
    return node


def bridge_node(environment: Mapping[str, str]) -> BridgeNode:
    """The verified Node every launcher child runs; raises a typed ``ChannelError``.

    Only the first use of a file probes it (in a child process, under the SSO
    adapter's own probe deadline); later launches only compare its identity.
    """
    source, configured = _setting(environment)
    with _LOCK:
        known = _VERIFIED.get((source, configured))
    if known is not None and _still_verified(known, configured):
        return known
    node = _verify(environment, source, configured)
    with _LOCK:
        _VERIFIED[(source, configured)] = node
    logger.info('[Channels] dx launcher Node verified: %s identity=%s', node.describe(),
                ':'.join(map(str, node.identity)))
    return node


__all__ = ['NODE_UNAVAILABLE', 'NODE_UNVERIFIED', 'BridgeNode', 'bridge_node', 'meets_bridge_floor']
