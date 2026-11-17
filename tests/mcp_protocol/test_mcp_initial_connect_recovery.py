"""A server whose first connect failed is retried like a failed reconnect.

On 2026-11-17 the execution host and its ~50 execution shards each ran MCP
auto-connect at once; npx launches racing on the shared npm cache exited before
the handshake in 17 processes (github) and 14 (12306-train). Those processes
never retried: the keepalive only revisits live servers and servers in reconnect
backoff, so a Turn there lacked the server for the process's life.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytest.importorskip('mcp', reason='MCP SDK not installed')

pytestmark = pytest.mark.unit

ROW = {'command': 'npx', 'args': ['-y', 'flaky-mcp'], 'enabled': True}


def _failing_boot(monkeypatch):
    from lib.mcp.client import _bridge as B
    from lib.mcp.client._errors import MCPConnectError
    br = B.MCPBridge(owner_user_id=7)
    monkeypatch.setattr(B, 'load_mcp_config', lambda user_id: {'flaky': dict(ROW), 'ok': {'command': 'ok'}})

    def connect_server(name, row):
        if name == 'flaky':
            raise MCPConnectError(name, RuntimeError('launcher exited before the handshake'))
        return []

    monkeypatch.setattr(br, 'connect_server', connect_server)
    assert br.connect_all() == {'ok': []}
    return br


def _sweep(br, monkeypatch, *, window=0.3):
    from lib.mcp.client import _bridge as B
    monkeypatch.setattr(B, 'MCP_KEEPALIVE_INTERVAL', 0.02)

    async def main():
        br._keepalive_stop = asyncio.Event()
        task = asyncio.get_running_loop().create_task(br._keepalive_loop())
        await asyncio.sleep(window)
        br._keepalive_stop.set()
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    asyncio.run(main())


def test_a_failed_first_connect_is_retried_by_the_keepalive(monkeypatch):
    br = _failing_boot(monkeypatch)
    assert br.get_breaker_state('flaky')['failures'] == 1
    retried = []
    monkeypatch.setattr(br, '_reconnect_server', lambda name: retried.append(name))
    with br._lock:
        br._breaker['flaky'] = (1, 0.0)  # its backoff elapsed
    _sweep(br, monkeypatch)
    assert 'flaky' in retried


def test_the_retry_connects_with_the_stored_row(monkeypatch):
    br = _failing_boot(monkeypatch)
    from lib.mcp.client import _bridge as B
    connected = []

    def connect_server(name, row):
        connected.append((name, row['command']))
        br._servers[name] = B._MCPServerHandle(name, row)
        return []

    monkeypatch.setattr(br, 'connect_server', connect_server)
    monkeypatch.setattr(br, '_stored_server_config', lambda name, refresh_browser: (dict(ROW), None))
    br._reconnect_server('flaky')
    assert connected == [('flaky', 'npx')]


def test_a_server_disabled_since_boot_is_forgotten_not_retried(monkeypatch):
    br = _failing_boot(monkeypatch)
    connected = []
    monkeypatch.setattr(br, 'connect_server', lambda name, row: connected.append(name) or [])
    monkeypatch.setattr(br, '_stored_server_config',
                        lambda name, refresh_browser: ({**ROW, 'enabled': False}, None))
    with pytest.raises(ValueError):
        br._reconnect_server('flaky')
    assert connected == []
    assert br.get_breaker_state('flaky') is None


def test_reconcile_disconnect_forgets_a_server_still_in_first_connect_backoff(monkeypatch):
    import lib.mcp.client as client
    import lib.mcp.config as config
    import lib.mcp.startup as startup
    import logging
    br = _failing_boot(monkeypatch)
    monkeypatch.setattr(client, 'get_bridge', lambda owner: br)
    monkeypatch.setattr(config, 'load_mcp_config', lambda user_id: {})
    startup._reconcile_one(7, 'flaky', 'disconnect', logger=logging.getLogger(__name__))
    assert br.get_breaker_state('flaky') is None


def test_a_failed_reconcile_connect_is_retried_by_the_keepalive(monkeypatch):
    """Measured 2026-11-18 on an isolated instance: a server added at runtime
    reached the execution host through the reconcile worker, whose connect
    timed out at 'initialize' under a 19-shard burst. Only connect_all handed
    failures to the keepalive, so the one bridge every shard relays to lacked
    the server until a restart."""
    import logging
    import lib.mcp.client as client
    import lib.mcp.config as config
    import lib.mcp.startup as startup
    from lib.mcp.client import _bridge as B
    from lib.mcp.client._errors import MCPConnectError
    br = B.MCPBridge(owner_user_id=7)
    monkeypatch.setattr(client, 'get_bridge', lambda owner: br)
    monkeypatch.setattr(config, 'load_mcp_config', lambda user_id: {'flaky': dict(ROW)})

    def connect_server(name, row):
        raise MCPConnectError(name, RuntimeError("Request 'initialize' timed out"))

    monkeypatch.setattr(br, 'connect_server', connect_server)
    with pytest.raises(MCPConnectError):
        startup._reconcile_one(7, 'flaky', 'connect', logger=logging.getLogger(__name__))
    assert br.get_breaker_state('flaky')['failures'] == 1
    retried = []
    monkeypatch.setattr(br, '_reconnect_server', lambda name: retried.append(name))
    with br._lock:
        br._breaker['flaky'] = (1, 0.0)
    _sweep(br, monkeypatch)
    assert 'flaky' in retried
