"""A directly started server never serves open auth beyond loopback.

2026-11-13: GAME-UI-2's first scratch server (10:24-10:27) bound 0.0.0.0 with open auth (no
credential) while carrying the production TOFU_PROJECT_PATH; nothing stopped it. A
``TOFU_RUN_SERVER=1`` start now refuses an open-auth bind to a non-loopback address with the typed
``UnauthenticatedNetworkBind`` (lib/server_boot/exposure.py). Managed workers keep their
configured bind: production runs as a ``TOFU_SERVER_WORKER`` in open mode on every interface.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from lib.server_boot import exposure

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('host', ['0.0.0.0', '::', '', '10.114.80.41', 'tofu-host.example'])
def test_open_auth_beyond_loopback_is_refused(host):
    with pytest.raises(exposure.UnauthenticatedNetworkBind, match='bind 127.0.0.1'):
        exposure.refuse_unauthenticated_network_bind(host, 'open')


@pytest.mark.parametrize('host, auth_mode', [
    ('127.0.0.1', 'open'), ('127.0.0.2', 'open'), ('localhost', 'open'), ('::1', 'open'),
    ('[::1]', 'open'), ('::ffff:127.0.0.1', 'open'),
    ('0.0.0.0', 'private'), ('::', 'multi-user'),
])
def test_loopback_or_credentials_are_allowed(host, auth_mode):
    exposure.refuse_unauthenticated_network_bind(host, auth_mode)


def test_a_scratch_server_bound_to_every_interface_with_open_auth_refuses_to_start(tmp_path):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    (tmp_path / 'project').mkdir()
    environment = {**os.environ, 'TOFU_RUN_SERVER': '1', 'TOFU_AUTH_MODE': 'open',
                   'TOFU_DATA_DIR': str(tmp_path / 'scratch'), 'TOFU_PROJECT_PATH': str(tmp_path / 'project'),
                   'TOFU_HEARTBEAT_DIR': str(tmp_path / 'heartbeat')}
    server = subprocess.Popen([sys.executable, str(ROOT / 'server.py'), '--host', '0.0.0.0', '--port', str(port)],
                              cwd=tmp_path, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True)
    try:
        _, stderr = server.communicate(timeout=240)
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGKILL)
            server.wait()
    assert server.returncode == 1, stderr[-3000:]
    assert "refusing to serve auth mode 'open' (no credential) on '0.0.0.0'" in stderr
    with socket.socket() as client:
        assert client.connect_ex(('127.0.0.1', port)) != 0  # it never listened
