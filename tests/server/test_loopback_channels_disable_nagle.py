"""Tofu's own loopback request/response channels disable Nagle on every socket.

A message written in more than one send must never wait for the peer's delayed
acknowledgement (about 40 ms on Linux). The 2026-11-13 channel audit found three
production channels without TCP_NODELAY: the shared loopback connect helper (the
execution-control and browser-relay clients), the browser relay's accepted
sockets, and the manager's control handler, whose replies are two sends. None
stalled measurably then (each wrote once per message or closed after replying);
these specifications keep a later second send from costing 40 ms.
"""

from __future__ import annotations

import socket
import threading

import pytest


pytestmark = pytest.mark.unit


def _nodelay(connection) -> int:
    return connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)


def test_loopback_connections_disable_nagle():
    from tofu_db._loopback import create_connection

    with socket.create_server(('127.0.0.1', 0)) as listener:
        with create_connection(listener.getsockname(), timeout=10) as connection:
            assert _nodelay(connection)


def test_browser_relay_accepted_sockets_disable_nagle(tmp_path, monkeypatch):
    from lib.browser import bridge_relay

    accepted = []
    served = threading.Event()

    def recording_serve(self, connection, cancel):
        accepted.append(_nodelay(connection))
        connection.close()
        served.set()

    monkeypatch.setattr(bridge_relay.RelayServer, '_serve', recording_serve)
    server = bridge_relay.RelayServer(bridge_relay.record_path(tmp_path))
    endpoint = server.start()
    try:
        with socket.create_connection((endpoint['host'], endpoint['port']), timeout=10):
            assert served.wait(10), 'the relay never accepted the connection'
    finally:
        assert server.close(2.0)
    assert accepted == [1]


def test_manager_control_handler_disables_nagle():
    from http.server import ThreadingHTTPServer

    import supervisor

    accepted = []
    ready = threading.Event()

    class RecordingHandler(supervisor.SupervisorHandler):
        def setup(self):
            super().setup()
            accepted.append(_nodelay(self.connection))
            ready.set()

        def handle(self):
            """The specification needs only the accepted socket."""

    server = ThreadingHTTPServer(('127.0.0.1', 0), RecordingHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with socket.create_connection(server.server_address, timeout=10):
            assert ready.wait(10), 'the manager handler never accepted the connection'
    finally:
        server.shutdown()
        server.server_close()
        worker.join(10)
    assert accepted == [1]
