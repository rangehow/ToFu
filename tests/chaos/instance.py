"""An isolated two-process Tofu instance for fault injection.

Responsibility: start, fault and stop one private Tofu deployment in the
production shape -- a resident storage host (``NativeStorageService``), the
resident execution host (``ExecutionService`` running
``serverctl_pkg.execution_host``) and a separate API host (``server.py`` with
``TOFU_PROCESS_ROLE=api``) -- and prove afterwards that nothing it started
outlives it. The manager's own roles (launch, joined stop, recovery after an
execution-host exit, API restart that keeps the execution host) are played
through the same ``ResidentService`` owners the manager uses.

Isolation (docs/TESTING_STRATEGY.md, "Scratch servers share nothing with the
live one"): every process gets an allowlisted environment whose HOME, TMPDIR,
data root, heartbeat directory and state records live under the instance root
on a host-local disk, binds 127.0.0.1 on a free port, and is tagged with
``TOFU_CHAOS_INSTANCE`` so a leak scan finds every descendant by its
environment rather than by a process tree that a double fork escapes.

Entry: ``ChaosInstance(root).start()``; ``stop()``; ``leaked_processes()``;
``held_locks()``; fault ports ``kill_execution_host``, ``recover_execution_host``,
``restart_api``, ``kill_storage`` / ``restart_storage``, ``freeze``/``thaw``.
Dependencies: serverctl_pkg resident owners, tests.chaos.fake_provider.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

CHECKOUT = Path(__file__).resolve().parents[2]
INSTANCE_TAG = 'TOFU_CHAOS_INSTANCE'
_KEPT = frozenset({'PATH', 'USER', 'LOGNAME', 'SHELL', 'TERM', 'TZ', 'LANG', 'LANGUAGE'})
# Readiness deadlines of the resident owners (serverctl_pkg/restart_budget.py)
# bound every wait here; none is a tuning value of the product.
from serverctl_pkg.restart_budget import (  # noqa: E402
    NATIVE_SERVICE_PREPARATION_SECONDS, NATIVE_SERVICE_STOP_SECONDS,
)


def free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _tagged_pids(tag: str) -> list[int]:
    needle = f'{INSTANCE_TAG}={tag}'.encode()
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if needle in (entry / 'environ').read_bytes().split(b'\0'):
                state = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                if state != 'Z':
                    found.append(int(entry.name))
        except OSError:
            continue
    return sorted(found)


class ChaosInstance:
    """One private deployment; every path and port is its own."""

    def __init__(self, root: Path, *, backend: str = 'sqlite', python: str = sys.executable,
                 faultfs: bool = True):
        self.root = Path(root).resolve()
        self.faultfs: Path | None = None
        self._want_faultfs = faultfs
        self.faultfs_control = self.root / 'faultfs.control'
        self.backend = backend
        self.python = python
        self.tag = uuid.uuid4().hex
        self.state_root = self.root / 'state'
        self.port = 0
        self.api: subprocess.Popen | None = None
        self.api_generation = 0
        self._storage = self._execution = None

    # environment -----------------------------------------------------------
    def _base_environment(self) -> dict[str, str]:
        for name in ('home', 'tmp', 'data', 'heartbeat', 'state/data', 'state/contracts', 'workspace'):
            (self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k in _KEPT or k.startswith('LC_')}
        if self.faultfs is not None:
            # The benchmark's medium injector over the data root: DolphinFS's
            # ENOENT for I/O on open-but-unlinked files always, plus ENOSPC
            # and barrier latency while armed through the control file.
            env.update({'LD_PRELOAD': str(self.faultfs), 'FAULTFS_ROOT': str(self.root / 'data'),
                        'FAULTFS_CONTROL': str(self.faultfs_control), 'FAULTFS_UNLINKED_ENOENT': '1',
                        'FAULTFS_STATS': str(self.root / 'faultfs.stats')})
        env.update({
            INSTANCE_TAG: self.tag,
            'HOME': str(self.root / 'home'), 'TMPDIR': str(self.root / 'tmp'),
            'XDG_CACHE_HOME': str(self.root / 'home' / '.cache'),
            'PYTHONPATH': str(CHECKOUT), 'PYTHONUNBUFFERED': '1', 'PYTHONNOUSERSITE': '1',
            'TOFU_DATA_DIR': str(self.root / 'data'), 'TOFU_HEARTBEAT_DIR': str(self.root / 'heartbeat'),
            'TOFU_DEPLOYMENT_MODE': 'personal', 'TOFU_PERSONAL_STORAGE_BACKEND': self.backend,
            # The instance never reaches a network: loopback endpoints only.
            'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
        })
        return env

    def _seed_caches(self) -> None:
        # tiktoken downloads its BPE into $TMPDIR/data-gym-cache when absent;
        # an offline instance must not retry that download per token count.
        host_cache = Path('/tmp/data-gym-cache')
        target = self.root / 'tmp' / 'data-gym-cache'
        if host_cache.is_dir() and not target.exists():
            shutil.copytree(host_cache, target)

    def _build_faultfs(self) -> Path:
        source = CHECKOUT / 'benchmarks' / 'agent_storage' / 'faultfs.c'
        target = self.root / 'faultfs.so'
        subprocess.run(['cc', '-O2', '-shared', '-fPIC', '-o', str(target), str(source), '-ldl'],
                       check=True, capture_output=True)
        return target

    def set_medium(self, *, fsync_us: int = 0, enospc: bool = False) -> None:
        """Arm the data root's medium faults; every process re-reads within 5 ms."""
        staged = self.faultfs_control.with_suffix('.tmp')
        staged.write_text(f'fsync_us={int(fsync_us)} enospc={1 if enospc else 0}\n')
        staged.replace(self.faultfs_control)

    def medium_stats(self) -> dict:
        """faultfs's shared counters (struct faultfs_stats, ten u64 fields)."""
        import struct
        path = self.root / 'faultfs.stats'
        if not path.exists():
            return {}
        names = ('barriers', 'injected_ns', 'enospc_returns', 'out_of_scope', 'medium_ns',
                 'writes', 'write_ns', 'write_bytes', 'reads', 'read_ns')
        return dict(zip(names, struct.unpack_from('10Q', path.read_bytes())))

    # lifecycle -------------------------------------------------------------
    def start(self) -> 'ChaosInstance':
        from serverctl_pkg.execution_service import ExecutionService
        from serverctl_pkg.native_storage_service import NativeStorageService
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._want_faultfs:
            self.faultfs = self._build_faultfs()
            self.set_medium(fsync_us=0, enospc=False)
        self.environment = self._base_environment()
        self._seed_caches()
        contract = self.state_root / 'contracts' / 'native_storage_connection_v2.json'
        if not contract.exists():
            contract.symlink_to(CHECKOUT / 'contracts' / 'native_storage_connection_v2.json')
        self.port = free_loopback_port()
        self._storage = NativeStorageService(str(self.state_root), self.python)
        self._execution = ExecutionService(str(self.state_root), self.python)
        self.start_storage()
        self.start_execution()
        self.start_api()
        return self

    def storage_environment(self) -> dict[str, str]:
        return {**self.environment, 'TOFU_PROJECT_PATH': str(self.state_root)}

    def start_storage(self) -> None:
        env = self.storage_environment()
        plan = self._storage.prepare(env)
        self.connection_file = self._storage.ensure(plan, env)
        self._wait(lambda: self._storage.record().get('state') in ('ready', 'failed'),
                   'storage host readiness')
        record = self._storage.record()
        if record.get('state') != 'ready':
            raise RuntimeError(f'storage host failed: {record.get("error")}')

    def execution_environment(self) -> dict[str, str]:
        # The execution host admits only the source tree it runs from.
        return {**self.environment, 'TOFU_PROJECT_PATH': str(CHECKOUT),
                'TOFU_STORAGE_CONNECTION_FILE': self.connection_file, 'PORT': str(self.port)}

    def start_execution(self) -> None:
        env = self.execution_environment()
        self.execution_plan = self._execution.prepare(env)
        self._execution.ensure(self.execution_plan, env)
        self.wait_execution_ready()

    def wait_execution_ready(self) -> dict:
        self._wait(lambda: self._execution.record().get('state') in ('ready', 'failed'),
                   'execution host readiness')
        record = self._execution.record()
        if record.get('state') != 'ready':
            raise RuntimeError(f'execution host failed: {record.get("error")}')
        return record

    def api_environment(self) -> dict[str, str]:
        return {**self.environment, 'TOFU_RUN_SERVER': '1', 'BIND_HOST': '127.0.0.1',
                'PORT': str(self.port), 'TOFU_PROCESS_ROLE': 'api',
                # The API host locates its execution owner's record here.
                'TOFU_PROJECT_PATH': str(self.state_root),
                'TOFU_STORAGE_CONNECTION_FILE': self.connection_file}

    def start_api(self) -> None:
        if not (CHECKOUT / 'static' / 'vite' / 'manifest.json').is_file():
            # The api role serves the shipped graph; CI downloads it, make test-api builds it.
            raise RuntimeError('the chaos API host needs the built frontend graph: '
                               'run `npm run build:frontend` (or make frontend-build)')
        self.api_generation += 1
        log = (self.root / f'api-{self.api_generation}.log').open('ab')
        self.api = subprocess.Popen(
            [self.python, 'server.py', '--host', '127.0.0.1', '--port', str(self.port)],
            cwd=str(CHECKOUT), env=self.api_environment(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self._wait(self._api_ready, 'API host readiness')

    def _api_ready(self) -> bool:
        if self.api is not None and self.api.poll() is not None:
            raise RuntimeError(f'API host exited {self.api.returncode}; see {self.root}/api-*.log')
        try:
            with urllib.request.urlopen(f'{self.base_url}/api/ready', timeout=5) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    @property
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def stop_api(self, sig: int = signal.SIGTERM) -> None:
        if self.api is None or self.api.poll() is not None:
            return
        os.killpg(self.api.pid, sig)
        try:
            self.api.wait(timeout=NATIVE_SERVICE_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(self.api.pid, signal.SIGKILL)
            self.api.wait()

    def restart_api(self, sig: int = signal.SIGTERM) -> None:
        """The manager's API restart: the execution host is preserved."""
        self.stop_api(sig)
        self.start_api()

    def execution_record(self) -> dict:
        return self._execution.record()

    def kill_execution_host(self) -> int:
        record = self._execution.record()
        pid = record['pid']
        os.kill(pid, signal.SIGKILL)
        self._wait(lambda: not self._execution.host_matches(record), 'execution host death')
        return pid

    def restart_execution_host(self) -> float:
        """An explicit restart's drain: SIGTERM and a joined stop within the
        manager's budget, with no kill allowed (a host that needs one raises),
        then the next generation. Returns the drain's seconds."""
        started = time.monotonic()
        self._execution.stop()
        drained = time.monotonic() - started
        env = self.execution_environment()
        self._execution.ensure(self._execution.prepare(env), env)
        self.wait_execution_ready()
        return drained

    def recover_execution_host(self) -> dict:
        """``ExecutionService.recover``'s replacement step: joined stop, then ensure."""
        self._execution.stop(retire_failed_close=True)
        env = self.execution_environment()
        self._execution.ensure(self._execution.prepare(env), env)
        return self.wait_execution_ready()

    def storage_record(self) -> dict:
        return self._storage.record()

    def freeze_storage(self) -> list[int]:
        """SIGSTOP the storage host's process group: an engine that stops answering."""
        pids = self._storage_pids()
        for pid in pids:
            os.kill(pid, signal.SIGSTOP)
        return pids

    def thaw_storage(self, pids: list[int]) -> None:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def _storage_pids(self) -> list[int]:
        host = self._storage.record().get('pid')
        out = []
        for pid in _tagged_pids(self.tag):
            try:
                if os.getpgid(pid) == os.getpgid(host):
                    out.append(pid)
            except (ProcessLookupError, TypeError):
                continue
        return out

    def stop(self) -> None:
        errors = []
        for step in (self.stop_api, lambda: self._execution and self._execution.stop(retire_failed_close=True),
                     lambda: self._storage and self._storage.stop(NATIVE_SERVICE_STOP_SECONDS,
                                                                  retire_failed_close=True)):
            try:
                step()
            except Exception as exc:  # every owner still stops; the first error is reported
                errors.append(f'{type(exc).__name__}: {exc}')
        if errors:
            raise RuntimeError('; '.join(errors))

    # leak accounting -------------------------------------------------------
    def leaked_processes(self) -> list[dict]:
        out = []
        for pid in _tagged_pids(self.tag):
            try:
                cmd = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            except OSError:
                continue
            out.append({'pid': pid, 'cmdline': cmd[:300]})
        return out

    def kill_leaked(self) -> None:
        for pid in _tagged_pids(self.tag):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def held_locks(self) -> list[str]:
        """Lease files under the instance that some process still holds."""
        held = []
        for path in [*self.root.rglob('*.lock'), *self.root.rglob('.*.lock')]:
            if not path.is_file():
                continue
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(str(path.relative_to(self.root)))
            finally:
                os.close(descriptor)
        return sorted(set(held))

    @staticmethod
    def _wait(predicate, what: str, timeout: float = NATIVE_SERVICE_PREPARATION_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError(f'{what} did not settle within {timeout:.0f}s')
            time.sleep(0.1)


def main() -> int:
    """Manual probe: start an instance under ROOT, print its URL, stop on Enter."""
    root = Path(sys.argv[1])
    instance = ChaosInstance(root)
    try:
        instance.start()
        print(json.dumps({'url': instance.base_url, 'execution': instance.execution_record().get('pid')}),
              flush=True)
        if sys.stdin.isatty():
            input()
    finally:
        instance.stop()
        print(json.dumps({'leaked': instance.leaked_processes(), 'locks': instance.held_locks()}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
