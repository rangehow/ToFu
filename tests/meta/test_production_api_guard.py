"""Every test is guarded against the production Tofu.

docs/TESTING_STRATEGY.md, "Tests never reach the production Tofu": tests/conftest.py applies
tests/support/production_api_guard.py to every test, with no opt-out. A test whose code reaches a
production port (a default, or a port the inherited environment named before the conftest
scrubbed it), or starts a child process aimed at one, fails at teardown even when the code
swallowed the refusal; a test that starts its own server on a free port passes. The nested
sessions run tests/support/production_api_guard_probe.py, which cannot reach a real server even
when the guard is missing.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from tests.meta.test_configured_slot_switch_isolation import _standalone_session_environment
from tests.support import production_api_guard as guard

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_PROBE = 'tests/support/production_api_guard_probe.py'
_REFUSED = 'connections to the production Tofu were attempted'
_LEFT = 'left the environment naming the production Tofu'


def _probe_session(tmp_path: Path, probes: list[str], *, workers: int,
                   environment: dict[str, str]) -> tuple[dict[str, list[str]], str]:
    """Each probe's failures and errors in a nested session ([] when it passed)."""
    report = tmp_path / 'probes.xml'
    completed = subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-n', str(workers), '-q',
         '--timeout=240', f'--junitxml={report}', f'--basetemp={tmp_path / "nested"}',
         *[f'{_PROBE}::{probe}' for probe in probes]],
        cwd=_ROOT, env=environment, capture_output=True, text=True, timeout=280)
    output = completed.stdout[-6000:] + completed.stderr[-2000:]
    assert report.exists(), output
    outcomes = {case.get('name'): [f"{child.tag}: {child.get('message', '')}" for child in case
                                   if child.tag in {'failure', 'error', 'skipped'}]
                for case in ElementTree.parse(report).getroot().iter('testcase')}
    return outcomes, output


def test_a_test_reaching_production_fails_and_one_with_its_own_server_passes(tmp_path):
    offending = {'test_swallowed_request_to_the_production_api_default': _REFUSED,
                 'test_swallowed_request_to_the_production_manager_default': _REFUSED,
                 'test_a_child_given_the_production_api_url': _REFUSED,
                 'test_a_child_whose_environment_names_the_production_manager_port': _REFUSED,
                 'test_a_shell_child_given_the_production_manager_address': _REFUSED,
                 'test_leaves_a_production_port_in_the_environment': _LEFT}
    # The last one runs after the leak: the conftest restored the variable for it.
    correct = ['test_a_server_of_its_own_on_a_free_port', 'test_a_child_aimed_at_a_server_of_its_own',
               'test_a_later_test_starts_without_it']
    outcomes, output = _probe_session(tmp_path, [*offending, *correct], workers=0,
                                      environment=_standalone_session_environment())

    assert {name: outcomes.get(name) for name in correct} == {name: [] for name in correct}, output
    for name, message in offending.items():
        failures = outcomes.get(name)
        assert failures and len(failures) == 1 and message in failures[0], (name, output)


@pytest.mark.parametrize('workers', [0, 2], ids=['serial', 'xdist'])
def test_ports_the_inherited_environment_named_are_production_ports_too(tmp_path, workers):
    ports = {}
    for variable in ('PORT', 'TOFU_SUPERVISOR_PORT'):
        with socket.socket() as scratch:  # closed again at once: nothing listens on these ports
            scratch.bind(('127.0.0.1', 0))
            ports[variable] = str(scratch.getsockname()[1])
    environment = {**_standalone_session_environment(), **ports,
                   'PRODUCTION_API_GUARD_PROBE_PORTS': ','.join(f'{name}={port}' for name, port in ports.items())}
    outcomes, output = _probe_session(tmp_path, ['test_swallowed_request_to_an_inherited_port'],
                                      workers=workers, environment=environment)

    assert sorted(outcomes) == [f'test_swallowed_request_to_an_inherited_port[{variable}]'
                                for variable in sorted(ports)], output
    for name, failures in outcomes.items():
        assert len(failures) == 1 and _REFUSED in failures[0], (name, output)


@pytest.mark.parametrize('argv, environment', [
    (['curl', 'http://127.0.0.1:15000/api/ready'], {}),
    (['python3', 'debug/inspect_conversation.py', '--api-url', 'http://user:pw@[::1]:15000'], {}),
    ([b'tofu', b'http://[::ffff:127.0.0.1]:15001/status'], {}),
    ('curl -s 0.0.0.0:15001/status', {}),
    (['python3', 'server.py', '--port', '15000'], {}),
    (['python3', 'serverctl.py', '--manager-port=15001'], {}),
    (['python3', '-c', 'pass'], {'PORT': '15000'}),
    (['python3', '-c', 'pass'], {'TOFU_API_URL': 'http://LOCALHOST:15001'}),
])
def test_a_child_aimed_at_production_is_recognized(argv, environment):
    assert guard.production_child(argv, environment)


@pytest.mark.parametrize('argv, environment', [
    (['curl', 'http://127.0.0.1:15002/'], {}),
    (['curl', 'http://10.0.0.1:15000/', 'http://localhost/'], {}),
    (['python3', 'server.py', '--port', '0'], {}),
    (['python3', '-c', 'pass', '--report', '15000'], {}),
    (['python3', '-c', 'pass'], {'TIMEOUT_MS': '15000',
                                 'PYTEST_CURRENT_TEST': 'tests/x.py::test[http://127.0.0.1:15000] (call)'}),
])
def test_a_child_aimed_elsewhere_is_left_alone(argv, environment):
    assert guard.production_child(argv, environment) == ''


def test_audited_connections_binds_and_launches_are_refused_below_the_socket_methods(request):
    # A raw _socket.socket or a reference taken before the patch still raises the audit event.
    attempts = request.getfixturevalue('production_api_guard')
    with pytest.raises(guard.ProductionConnectionRefused, match='production Tofu'):
        sys.audit('socket.connect', None, ('::1', 15001))
    with pytest.raises(guard.ProductionConnectionRefused, match='child process aimed at the production Tofu'):
        sys.audit('os.posix_spawn', 'curl', ['curl', 'localhost:15000'], {})
    with pytest.raises(guard.ProductionConnectionRefused, match="bind the production Tofu's port"):
        sys.audit('socket.bind', None, ('0.0.0.0', 15000))
    assert attempts == [('::1', 15001), "child process: argument 'localhost:15000'", 'bind 0.0.0.0:15000']
    attempts.clear()  # this spec consumed its own attempts
    sys.audit('socket.connect', None, ('127.0.0.1', 15002))
    sys.audit('socket.bind', None, ('127.0.0.1', 0))
    sys.audit('subprocess.Popen', 'curl', ['curl', 'http://127.0.0.1:15002/'], None, {})
