"""Tests never reach the production Tofu on this host.

On 2026-11-13 at 11:44:11 an out-of-band watcher unit test inherited ``Config``'s default
``api_url`` and sent two ``GET /api/v4/model-routing`` (200) and eight reveal requests (405) to
the live server. ``tests/conftest.py`` scrubs ``PORT`` and ``TOFU_SUPERVISOR_PORT`` from the
environment, so code under test falls back to the hardcoded production defaults.

``tests/conftest.py`` applies ``production_api_guard`` to every test, and nothing opts out: a test
that needs a server starts its own on a free port, or uses a fake. The guard refuses each of these,
records it and fails the test at teardown, even when the code under test swallowed the refusal:

- a connection to, or a bind of, a loopback (or unspecified) address on a production port;
- a child process whose command line or environment names such an address, or a production port
  in a ``--...port`` option or a ``*PORT`` variable;
- such a variable left in ``os.environ`` after the test's teardown (``remember_environment`` and
  ``check_environment``, called from the conftest's runtest hooks; it is restored).

Production ports are the defaults below plus the ports the inherited variables in
``INHERITED_PORT_VARIABLES`` name; ``guard_this_process`` reads them before the conftest scrubs
them, and xdist workers, which start after the scrub, receive them from their controller.
Mechanism and its limits: docs/TESTING_STRATEGY.md, "Tests never reach the production Tofu".
"""
from __future__ import annotations

import errno
import ipaddress
import os
import re
import socket
import sys

import pytest

# server.py ``--port`` default and serverctl ``MANAGER_PORT`` default: the production API and manager.
PRODUCTION_DEFAULT_PORTS = frozenset({15000, 15001})
# The running Tofu's API port (``PORT``, and the bound port the server stamps into its children)
# and its manager port. tests/conftest.py removes them from every pytest process.
INHERITED_PORT_VARIABLES = ('PORT', '_TOFU_RUNTIME_PORT', '_TOFU_REEXEC_PORT', 'TOFU_SUPERVISOR_PORT')
PRODUCTION_PORTS = set(PRODUCTION_DEFAULT_PORTS)

_WORKER_INPUT_KEY = 'tofu_production_ports'
# A host and port as written in a URL, ``host:port`` or ``[v6]:port``.
_HOST_PORT = re.compile(r'(?<![\w.-])(localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[^\]\s]*\]):(\d{1,5})(?!\d)',
                        re.IGNORECASE)
# A port option: ``--port 15000``, ``--manager-port=15001``.
_PORT_OPTION = re.compile(r'--(?:\w+-)*port(?:=(.*))?$', re.IGNORECASE)


class ProductionConnectionRefused(ConnectionRefusedError):
    """A test tried to connect to the production Tofu."""


def _loopback(host: str) -> bool:
    if host in ('', 'localhost'):
        return True
    try:
        address = ipaddress.ip_address(host.split('%', 1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _production_port(value: str) -> bool:
    value = value.strip()
    return value.isdigit() and int(value) in PRODUCTION_PORTS


def production_target(address) -> bool:
    """Whether a socket address names the production Tofu on this host."""
    return (isinstance(address, tuple) and len(address) >= 2 and isinstance(address[0], str)
            and address[1] in PRODUCTION_PORTS and _loopback(address[0]))


def _names_production(text: str) -> bool:
    return any(int(port) in PRODUCTION_PORTS and _loopback(host.strip('[]').lower())
               for host, port in _HOST_PORT.findall(text))


def production_child(argv, environment) -> str:
    """Why a child with this command line and environment is aimed at the production Tofu, or ''.

    ``environment`` None means the child inherits ``os.environ``. Pytest's own ``PYTEST_*``
    bookkeeping (a test ID can contain a URL) never names a target.
    """
    words = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or ())
    words = [os.fsdecode(word) if isinstance(word, (str, bytes, os.PathLike)) else str(word)
             for word in words]
    for index, word in enumerate(words):
        if _names_production(word):
            return f'argument {word!r}'
        option = _PORT_OPTION.match(word)
        if option:
            value = option.group(1)
            if value is None:
                value = words[index + 1] if index + 1 < len(words) else ''
            if _production_port(value):
                return f'argument {word} {value}' if option.group(1) is None else f'argument {word}'
    for name, value in _production_variables(os.environ if environment is None else environment).items():
        return f'environment {name}={value}'
    return ''


def _production_variables(environment) -> dict[str, str]:
    """The variables of an environment that name the production Tofu."""
    named = {}
    for name, value in environment.items():
        name, value = os.fsdecode(name), os.fsdecode(value)
        if name.startswith('PYTEST_'):
            continue
        if (name.upper().endswith('PORT') and _production_port(value)) or _names_production(value):
            named[name] = value
    return named


# The running test's attempt list; attempts outside any test (collection, a session fixture) are
# reported by the next test that starts.
_active_attempts: list | None = None
_attempts_outside_tests: list = []
_audit_hook_installed = False


def _refuse(attempt, message: str):
    if _active_attempts is not None:
        _active_attempts.append(attempt)
    else:
        _attempts_outside_tests.append(('outside a test', attempt))
    raise ProductionConnectionRefused(message)


# Audit events that start a process: event -> (command line, environment) from the event's arguments.
_CHILD_LAUNCHES = {
    'subprocess.Popen': lambda args: (args[1], args[3]),  # executable, args, cwd, env
    'os.posix_spawn': lambda args: (args[1], args[2]),  # path, argv, env
    'os.exec': lambda args: (args[1], args[2]),  # path, args, env
    'os.system': lambda args: (args[0], None),  # command
}


def _audit(event: str, args: tuple) -> None:
    if event == 'socket.connect':
        # Code that bypasses the patched socket methods (a reference taken before the patch, a raw
        # _socket.socket) still reaches this event.
        if production_target(args[1]):
            _refuse(args[1][:2], f'test tried to connect to the production Tofu at {args[1][0]}:{args[1][1]}')
        return
    if event == 'socket.bind':
        # A test holding a production port would keep a restarting production server off it.
        if production_target(args[1]):
            _refuse(f'bind {args[1][0]}:{args[1][1]}',
                    f"test tried to bind the production Tofu's port {args[1][0]}:{args[1][1]}")
        return
    launch = _CHILD_LAUNCHES.get(event)
    if launch is not None:
        reason = production_child(*launch(args))
        if reason:
            _refuse(f'child process: {reason}',
                    f'test tried to start a child process aimed at the production Tofu: {reason}')


def dotenv_without_lifecycle_authority(loader, names):
    """A dotenv loader that re-scrubs lifecycle authority after each load.

    The scrub above removes this shell's production identity once. The
    application's own boot loads the checkout's ``.env`` later, and that file
    names the running server's ``PORT``; the loader only skips values already
    present, so a scrubbed name comes back. Every child this process starts then
    inherits a production-looking port and the guard refuses it — observed as
    ``EnginePreparationError: Cannot read the storage engine source manifest ...
    (ProductionConnectionRefused)`` when a booted test server read its engine
    manifest. Restoring the scrubbed names keeps the file's ordinary
    configuration and drops only the identity the conftest removed.
    """
    names = tuple(names)

    def load(*args, **kwargs):
        target = kwargs.get('environ')
        if target is None and len(args) > 1:
            target = args[1]
        target = target if target is not None else os.environ
        before = {name: target.get(name) for name in names}
        try:
            return loader(*args, **kwargs)
        finally:
            for name, value in before.items():
                if value is None:
                    target.pop(name, None)
                else:
                    target[name] = value

    return load


def guard_this_process(environment) -> None:
    """Treat the ports the inherited environment names as production ports, and watch every
    connection and process launch of this process. tests/conftest.py calls this before it scrubs
    those variables."""
    global _audit_hook_installed
    for name in INHERITED_PORT_VARIABLES:
        value = (environment.get(name) or '').strip()
        if value.isdigit() and 0 < int(value) < 65536:
            PRODUCTION_PORTS.add(int(value))
    if not _audit_hook_installed:
        sys.addaudithook(_audit)  # permanent: an audit hook cannot be removed
        _audit_hook_installed = True


def send_to_worker(workerinput: dict) -> None:
    """xdist ``pytest_configure_node``: workers start after the scrub, so hand them the ports."""
    workerinput[_WORKER_INPUT_KEY] = sorted(PRODUCTION_PORTS)


def receive_from_controller(workerinput) -> None:
    """xdist worker ``pytest_configure``: adopt the controller's production ports."""
    PRODUCTION_PORTS.update((workerinput or {}).get(_WORKER_INPUT_KEY, ()))


def fail_on_attempts(attempts: list) -> None:
    if attempts:
        pytest.fail(f'connections to the production Tofu were attempted: {attempts}', pytrace=False)


@pytest.fixture(autouse=True)
def production_api_guard():
    global _active_attempts
    attempts: list = list(_attempts_outside_tests)
    _attempts_outside_tests.clear()
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded_connect(self, address):
        if production_target(address):
            attempts.append(address[:2])
            raise ProductionConnectionRefused(
                f'test tried to connect to the production Tofu at {address[0]}:{address[1]}')
        return connect(self, address)

    def guarded_connect_ex(self, address):
        if production_target(address):
            attempts.append(address[:2])
            return errno.ECONNREFUSED
        return connect_ex(self, address)

    # Its own MonkeyPatch: a test's ``monkeypatch.undo()`` never lifts the guard.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, 'connect', guarded_connect)
        patch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
        outer, _active_attempts = _active_attempts, attempts
        try:
            yield attempts
        finally:
            _active_attempts = outer
    fail_on_attempts(attempts)


# What of the environment named production when the running test's call phase began.
_environment_at_call: dict[str, str] | None = None


def remember_environment() -> None:
    """``pytest_runtest_call``: after setup, so fixtures (``monkeypatch`` included) have set theirs."""
    global _environment_at_call
    _environment_at_call = _production_variables(os.environ)


def check_environment() -> None:
    """After ``pytest_runtest_teardown``, once every fixture restored what it set: a variable naming
    production that the test left would aim every later test's children at production. Restore it
    and fail the test that left it."""
    global _environment_at_call
    before, _environment_at_call = _environment_at_call, None
    if before is None:  # the call phase never ran
        return
    left = {name: value for name, value in _production_variables(os.environ).items()
            if before.get(name) != value}
    for name in left:
        if name in before:
            os.environ[name] = before[name]
        else:
            del os.environ[name]
    if left:
        pytest.fail(f'the test left the environment naming the production Tofu: {left} '
                    "(restored; later tests' children would have aimed at it)", pytrace=False)
