"""A loopback relay owner stops promptly and joins its accept thread.

Found 2026-11-18 while adding the MCP relay: ``RelayServer.close`` only closed
the listening socket, which on Linux does not wake a thread blocked in
``accept()``. Every close waited its whole timeout for the accept thread and
still reported success (only call threads were checked), so each process stop
spent its full relay budget per bridge without a trace.
"""
import time

import pytest

pytestmark = pytest.mark.unit


def test_close_wakes_the_accept_thread_at_once(tmp_path):
    from lib.runtime.loopback_relay import RelayServer
    server = RelayServer(tmp_path / 'relay.json', format='test/v1', methods={}, name='test')
    server.start()
    started = time.monotonic()
    assert server.close(timeout=30.0)
    assert time.monotonic() - started < 5.0, 'close waited for its timeout'
    assert not server._accept_thread.is_alive()
    assert not (tmp_path / 'relay.json').exists()
