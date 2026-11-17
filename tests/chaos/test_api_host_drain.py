"""API-host graceful drain on a real process: an orphaned request job never holds exit.

Contract (lib/server/server_loop_runtime.py ``_retire_sync_executor``,
lib/server/server_shutdown.py ``exit_after_completed_lifespan``): once the
application lifespan has stopped every owner, the API host exits 0 promptly,
even while a synchronous request job whose client is gone still blocks on an
upstream. Before, ``asyncio.run`` teardown and interpreter exit joined that job,
and a slow enough one ran into the hard deadline, which exits 1 (the
2026-11-18 02:12:28 restart: 60.4 s of a 65 s grace, exit code 1).

The isolated instance is tests/chaos/instance.py; the blocking upstream is a
local listener that accepts and never answers, reached through the public
provider probe, a synchronous route that runs on the loop's default executor.
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import threading
import time

import pytest

from lib.llm_dispatch.discovery._discover import _DISCOVER_TIMEOUT
from tests.chaos.instance import ChaosInstance

pytestmark = [pytest.mark.api, pytest.mark.timeout(600)]


def test_api_host_exits_cleanly_without_joining_an_orphaned_request_job(tmp_path):
    silent = socket.socket()
    silent.bind(('127.0.0.1', 0))
    silent.listen(8)
    accepted: list[socket.socket] = []
    upstream_reached = threading.Event()

    def accept():
        while True:
            try:
                connection, _ = silent.accept()
            except OSError:
                return
            accepted.append(connection)
            upstream_reached.set()

    threading.Thread(target=accept, daemon=True).start()
    instance = ChaosInstance(tmp_path / 'instance', faultfs=False)
    try:
        instance.start()
        client = http.client.HTTPConnection('127.0.0.1', instance.port, timeout=1)
        body = json.dumps({'base_url': f'http://127.0.0.1:{silent.getsockname()[1]}/v1',
                           'api_key': 'probe-key'})

        def request():
            try:
                client.request('POST', '/api/v4/providers/probe', body,
                               {'Content-Type': 'application/json'})
                client.getresponse().read()
            except OSError:
                pass  # The client gives up; the job keeps waiting upstream.
            finally:
                client.close()

        threading.Thread(target=request, daemon=True).start()
        assert upstream_reached.wait(60), 'the probe job never reached the upstream'
        reached_at = time.monotonic()

        api = instance.api
        os.kill(api.pid, signal.SIGTERM)  # The manager signals the API host only.
        api.wait(timeout=120)
        exited_at = time.monotonic()
        assert api.returncode == 0, f'exit {api.returncode}; see {instance.root}/api-1.log'
        # The job would block until its own discovery timeout; exit came first.
        assert exited_at - reached_at < _DISCOVER_TIMEOUT, exited_at - reached_at
        instance.api = None
    finally:
        try:
            instance.stop()
        finally:
            silent.close()
            for connection in accepted:
                connection.close()
            leaked = instance.leaked_processes()
            instance.kill_leaked()
    assert not leaked, leaked
