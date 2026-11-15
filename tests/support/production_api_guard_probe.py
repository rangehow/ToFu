"""Probes for tests/meta/test_production_api_guard.py; collected only when named explicitly.

Each probe does what an offending (or a correct) test does, inside a nested pytest session that
loads tests/conftest.py like every test. This module stays safe when the guard is missing or
broken: below every guard it refuses connections to the production defaults itself, and the
probe children only name their target, they never connect to it.
"""
from __future__ import annotations

import errno
import os
import shlex
import socket
import subprocess
import sys
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit

_DEFAULTS = frozenset({15000, 15001})
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex


def _nothing_answers_on_the_defaults(self, address):
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in _DEFAULTS:
        raise ConnectionRefusedError(errno.ECONNREFUSED, 'probe: nothing answers here')
    return _connect(self, address)


def _nothing_answers_on_the_defaults_ex(self, address):
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in _DEFAULTS:
        return errno.ECONNREFUSED
    return _connect_ex(self, address)


# Installed at import, before any fixture: the guard wraps these, never the other way round.
socket.socket.connect = _nothing_answers_on_the_defaults
socket.socket.connect_ex = _nothing_answers_on_the_defaults_ex

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_swallowing_errors(url: str) -> int | None:
    """Code under test that swallows a failed request, like oob_alert.http_get_json."""
    try:
        with _NO_PROXY.open(url, timeout=10) as response:
            return response.status
    except OSError:
        return None


def _start_swallowing_errors(argv, **options) -> int | None:
    try:
        return subprocess.run(argv, capture_output=True, timeout=60, **options).returncode
    except OSError:
        return None


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *_):
        pass


@contextmanager
def _own_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}/'
    finally:
        server.shutdown()
        server.server_close()


def test_swallowed_request_to_the_production_api_default():
    assert _get_swallowing_errors('http://127.0.0.1:15000/api/ready') is None


def test_swallowed_request_to_the_production_manager_default():
    assert _get_swallowing_errors('http://localhost:15001/status') is None


def test_a_server_of_its_own_on_a_free_port():
    with _own_server() as url:
        assert _get_swallowing_errors(url) == 200


@pytest.mark.parametrize('variable', ['PORT', 'TOFU_SUPERVISOR_PORT'])
def test_swallowed_request_to_an_inherited_port(variable):
    # The outer spec named a scratch port (nothing listens there) in each inherited variable, and
    # tells the probe which one through PRODUCTION_API_GUARD_PROBE_PORTS: the conftest scrubbed them.
    port = dict(item.split('=') for item in os.environ['PRODUCTION_API_GUARD_PROBE_PORTS'].split(','))[variable]
    assert _get_swallowing_errors(f'http://127.0.0.1:{port}/api/ready') is None


def test_a_child_given_the_production_api_url():
    _start_swallowing_errors([sys.executable, '-c', 'pass', '--api-url', 'http://127.0.0.1:15000'])


def test_a_child_whose_environment_names_the_production_manager_port():
    _start_swallowing_errors([sys.executable, '-c', 'pass'], env={**os.environ, 'TOFU_SUPERVISOR_PORT': '15001'})


def test_a_shell_child_given_the_production_manager_address():
    _start_swallowing_errors(f'{shlex.quote(sys.executable)} -c pass localhost:15001/status', shell=True)


def test_leaves_a_production_port_in_the_environment():
    os.environ['_TOFU_REEXEC_PORT'] = '15000'  # as a re-exec stamps it; this test never restores it


def test_a_later_test_starts_without_it():
    assert '_TOFU_REEXEC_PORT' not in os.environ


def test_a_child_aimed_at_a_server_of_its_own():
    fetch = ('import sys, urllib.request; '
             'urllib.request.build_opener(urllib.request.ProxyHandler({})).open(sys.argv[1], timeout=10)')
    with _own_server() as url:
        assert _start_swallowing_errors([sys.executable, '-c', fetch, url]) == 0
