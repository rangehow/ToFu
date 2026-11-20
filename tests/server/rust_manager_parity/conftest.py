"""Black-box parity harness for Tofu's lifecycle manager.

Every test runs against both implementations of the manager HTTP protocol:
the Python manager (``supervisor.py`` + ``server_manager.py``) and the Rust
``tofu-manager manager`` binary (``TOFU_MANAGER_BINARY`` or
``<repo>/target/debug/tofu-manager``; skipped when absent). Tests observe only
the public surface: HTTP JSON, ``data/server-manager-state.json`` and
``logs/server-manager.log``.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tests.server.rust_manager_parity.fake_project import build_project
from tests.support.owned_processes import retire_processes_under
from tofu_db._loopback import bind_ephemeral_loopback

REPO = Path(__file__).resolve().parents[3]
WAIT_SECONDS = 60.0
REQUEST_SECONDS = 180.0
SOURCE = 'parity'
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_RUST_SKIP_MARKER = 'Rust manager binary not found'


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Never let the Python half passing hide the Rust half not running."""
    skipped = [report for report in terminalreporter.stats.get('skipped', [])
               if _RUST_SKIP_MARKER in str(report.longrepr)]
    if not skipped:
        return
    binary = Path(os.environ.get('TOFU_MANAGER_BINARY')
                  or REPO / 'target' / 'debug' / 'tofu-manager')
    terminalreporter.write_sep('=', 'Rust parity coverage: UNVERIFIED')
    terminalreporter.write_line(
        f'{len(skipped)} Rust-backed parity cases were skipped: the Rust manager binary '
        f'was not found at {binary}.')
    terminalreporter.write_line(
        'These cases are UNVERIFIED: a Python-only pass does not cover them. Build '
        'packages/tofu-manager (cargo build) or set TOFU_MANAGER_BINARY to run them.')


def free_port() -> int:
    with socket.socket() as sock:
        bind_ephemeral_loopback(sock)
        return int(sock.getsockname()[1])


def pid_alive(pid: int) -> bool:
    """Alive and not a zombie (an unreaped child of the manager is dead)."""
    try:
        raw = Path(f'/proc/{pid}/stat').read_bytes()
    except OSError:
        return False
    return raw[raw.rfind(b')') + 2:].split()[0] != b'Z'


def _request(url: str, *, body: dict | None = None,
             timeout: float = REQUEST_SECONDS) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method='GET' if body is None else 'POST',
        headers={'Content-Type': 'application/json'} if body is not None else {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b'{}')
        except ValueError:
            return error.code, {'raw': raw.decode(errors='replace')}


def wait_for(predicate, description: str, timeout: float = WAIT_SECONDS, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f'timed out after {timeout:.0f}s waiting for {description}; last={last!r}')


class ManagerHarness:
    """One running manager owning one fake project."""

    def __init__(self, impl: str, project: Path, port: int, worker_port: int,
                 process: subprocess.Popen):
        self.impl = impl
        self.project = project
        self.port = port
        self.worker_port = worker_port
        self.process = process
        self.base_url = f'http://127.0.0.1:{port}'
        self.log_path = project / 'logs' / 'server-manager.log'
        self.state_path = project / 'data' / 'server-manager-state.json'

    # --- manager protocol -------------------------------------------------
    def get(self, path: str, *, timeout: float = 30.0) -> tuple[int, dict]:
        return _request(self.base_url + path, timeout=timeout)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return _request(self.base_url + path, body=body)

    def control(self, action: str, **fields) -> tuple[int, dict]:
        return self.post(f'/{action}', {'projectPath': str(self.project), 'source': SOURCE, **fields})

    def status(self, *, probe: bool = False) -> dict:
        query = urllib.parse.urlencode({'projectPath': str(self.project), 'probe': '1' if probe else '0'})
        code, payload = self.get(f'/status?{query}')
        assert code == 200, (code, payload)
        return payload

    def wait_serving(self, *, other_than: int | None = None, timeout: float = WAIT_SECONDS) -> dict:
        """A ready worker the lock names, whose pid differs from ``other_than``."""
        def serving():
            current = self.status(probe=True)
            pid = current.get('pid')
            if (current.get('observed') == 'running' and current.get('ready') is True
                    and isinstance(pid, int) and pid != other_than and pid == self.lock_pid()
                    and self.worker_health_pid() == pid):
                return current
            return None
        return wait_for(serving, f'a serving worker other than {other_than}', timeout)

    def start_serving(self) -> dict:
        code, started = self.control('start')
        assert code == 200 and started.get('ok') is True, (code, started)
        return self.wait_serving()

    # --- observable files -------------------------------------------------
    def lock_pid(self) -> int | None:
        try:
            entry = (self.project / 'data' / '.server.lock').read_text().strip()
        except OSError:
            return None
        pid, _, _host = entry.partition('@')
        return int(pid) if pid.isdigit() else None

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def storage_record(self) -> dict:
        return self._record('native-storage-service.json')

    def execution_record(self) -> dict:
        return self._record('execution-service.json')

    def _record(self, name: str) -> dict:
        try:
            return json.loads((self.project / 'data' / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def last_restart(self) -> dict:
        return self.state().get('lastRestart') or {}

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''

    def log_size(self) -> int:
        return len(self.log_text())

    def wait_log(self, needle: str, *, since: int = 0, timeout: float = WAIT_SECONDS) -> str:
        return wait_for(lambda: needle in self.log_text()[since:] and self.log_text()[since:],
                        f'{needle!r} in {self.log_path}', timeout)

    def events(self) -> list[str]:
        try:
            return (self.project / 'events.log').read_text(encoding='utf-8').splitlines()
        except OSError:
            return []

    def prep_calls(self) -> list[str]:
        try:
            return (self.project / 'prep-calls.log').read_text(encoding='utf-8').split()
        except OSError:
            return []

    # --- the worker itself ------------------------------------------------
    def worker_health_pid(self) -> int | None:
        try:
            code, payload = _request(f'http://127.0.0.1:{self.worker_port}/api/health', timeout=5)
        except OSError:
            return None
        return payload.get('pid') if code == 200 else None

    def worker_ready(self) -> bool:
        try:
            code, payload = _request(f'http://127.0.0.1:{self.worker_port}/api/ready', timeout=5)
        except OSError:
            return False
        return code == 200 and payload.get('ready') is True


@pytest.fixture(scope='session')
def parity_shim(tmp_path_factory) -> str:
    """``parity_shim.c`` built for ``LD_PRELOAD`` into a manager (quota and Nagle probes)."""
    compiler = shutil.which('cc') or shutil.which('gcc')
    if compiler is None:
        pytest.skip('a C compiler is required to build parity_shim.c')
    output = tmp_path_factory.mktemp('parity-shim') / 'parity_shim.so'
    subprocess.run([compiler, '-shared', '-fPIC', '-O2', '-o', str(output),
                    str(Path(__file__).resolve().parent / 'parity_shim.c'), '-ldl'],
                   check=True, capture_output=True)
    return str(output)


@pytest.fixture
def native_storage() -> bool:
    """Override with ``True`` in a module to enable the fake resident storage host."""
    return False


@pytest.fixture
def execution() -> bool:
    """Override with ``True`` in a module to enable the fake resident execution host."""
    return False


@pytest.fixture
def manager_environment() -> dict[str, str]:
    """Override in a module to add environment to the manager process."""
    return {}


@pytest.fixture
def source_root() -> Path:
    """Override in a module: the checkout the manager runs from (its generation closure)."""
    return REPO


@pytest.fixture
def prepare_project():
    """Override in a module: ``callable(project)`` run before the manager starts."""
    return None


@pytest.fixture(params=['python', 'rust'])
def manager_impl(request) -> str:
    return request.param


def _launch_argv(impl: str, source_root: Path = REPO) -> list[str]:
    if impl == 'python':
        return [sys.executable, str(source_root / 'supervisor.py')]
    binary = Path(os.environ.get('TOFU_MANAGER_BINARY') or REPO / 'target' / 'debug' / 'tofu-manager')
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        pytest.skip(f'Rust manager binary not found at {binary}: build packages/tofu-manager '
                    '(cargo build) or set TOFU_MANAGER_BINARY')
    return [str(binary), 'manager', '--source-root', os.path.realpath(source_root)]


def _scrubbed_environment(root: Path) -> dict[str, str]:
    """Keep executable lookup; home, temporary and XDG roots belong to this fixture."""
    environment = {name: os.environ[name] for name in ('PATH',) if name in os.environ}
    home, temporary = root / 'home', root / 'tmp'
    environment.update({
        'HOME': str(home), 'TMPDIR': str(temporary), 'TMP': str(temporary), 'TEMP': str(temporary),
        'XDG_CACHE_HOME': str(home / '.cache'), 'XDG_CONFIG_HOME': str(home / '.config'),
        'XDG_DATA_HOME': str(home / '.local' / 'share'),
        'XDG_STATE_HOME': str(home / '.local' / 'state'), 'PYTHONPATH': str(REPO),
    })
    for name in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        Path(environment[name]).mkdir(mode=0o700, parents=True, exist_ok=True)
    return environment


@pytest.fixture
def manager(manager_impl, native_storage, execution, manager_environment, prepare_project, source_root,
            tmp_path):
    if not sys.platform.startswith('linux'):
        pytest.skip('Linux process identity required')
    argv = _launch_argv(manager_impl, source_root)
    project = build_project(tmp_path, native_storage=native_storage, execution=execution)
    if prepare_project is not None:
        prepare_project(project)
    port, worker_port = free_port(), free_port()
    while worker_port == port:
        worker_port = free_port()
    environment = {
        **_scrubbed_environment(tmp_path),
        'TOFU_SUPERVISOR_PROJECTS': str(project),
        'TOFU_SUPERVISOR_PORT': str(port),
        'TOFU_SUPERVISOR_HOST': '127.0.0.1',
        'TOFU_SUPERVISOR_PYTHON': sys.executable,
        'TOFU_SUPERVISOR_OWNER_PID': str(os.getpid()),
        'PORT': str(worker_port),
        'TOFU_MANAGER_INTERVAL': '0.2',
        'TOFU_HEARTBEAT_DIR': str(tmp_path / 'hb'),
        **{key: value.replace('{project}', str(project)) for key, value in manager_environment.items()},
    }
    log_path = project / 'logs' / 'server-manager.log'
    with log_path.open('ab') as log:
        process = subprocess.Popen(
            argv, cwd=source_root, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    harness = ManagerHarness(manager_impl, project, port, worker_port, process)
    stop_result: object = None
    try:
        def healthy():
            if process.poll() is not None:
                raise AssertionError(
                    f'{manager_impl} manager exited {process.returncode}:\n{harness.log_text()[-4000:]}')
            try:
                code, payload = harness.get('/health', timeout=5)
            except OSError:
                return None
            return payload if code == 200 and payload.get('ok') is True else None
        wait_for(healthy, f'{manager_impl} manager /health')
        yield harness
    finally:
        os.chmod(project / 'data', 0o700)
        (project / 'probe-stop-mode').write_text('clean')
        (project / 'exec-stop-mode').write_text('clean')
        for name in ('quota-full', 'bad-engines'):
            (project / name).unlink(missing_ok=True)
        if process.poll() is None:
            try:
                stop_result = harness.control('stop')
            except OSError as exc:
                stop_result = f'stop request failed: {exc}'
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        leftovers = retire_processes_under(tmp_path)
        assert leftovers == [], f'processes outlived the test: {leftovers}; stop: {stop_result}'
