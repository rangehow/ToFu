"""Demand-driven execution shards of one resident execution generation.

Problem: every session ran its model rounds in the one resident execution
interpreter. Per-round work is Python CPU under one GIL, so that host saturated
at about one core: the 2026-11-17 harness baseline measured 2.0 s per round at
20 concurrent sessions against 0.09 s for one (docs/HARNESS_PERFORMANCE.md).

Decision: the resident execution host (the primary) starts execution shards on
demand. A shard is the same headless application in the ``execution_shard``
role (bootstrap_pkg/process_roles.py): it claims durable conversation and
automation jobs from the same authority and owns no recovery, scheduler or
inbound singleton. Claims are process-bound (lib/storage/worker_process.py),
so a shard's death is proved by the kernel and its jobs are taken over exactly
as a crashed primary's would be.

- One generation. A shard runs the bytes its primary's frozen
  ``PinnedImportSource`` retained, exported to a private snapshot that one
  fork server (``execution_shard_boot.py --fork-server``, spawned from
  ``PinnedHelperSource``) loads whole: a merge that rewrote the checkout after
  the primary booted never mixes revisions across processes. Its plan must
  equal the primary's (checked against the launch environment as it was
  before the image's imports changed ``os.environ``).
- Fast boot. The fork server is single-threaded: it imports the application
  and the modules the primary has imported (``preload_shard_image``), refuses
  to fork if anything started a thread, and forks one shard per request,
  passing the shard's lease, manager and report descriptors over SCM_RIGHTS.
  A forked shard skips interpreter start, imports and warm-up (boot measured
  about 3-6 s under host load instead of 9-15 s). The primary's first job
  starts the fork server, so it is ready before a second concurrent job needs
  a shard; it retires after ``TOFU_EXECUTOR_IDLE_SECONDS`` without work.
- Placement at the claim boundary. A shard probes only while it runs nothing
  (one job per shard); the primary probes while it runs nothing or no shard
  is idle. A claim the primary owns while it already runs a job is placed
  before any handler effect (``place``): the primary enters it at once only
  when its own job has ended meanwhile; otherwise the claim stays parked
  under its lease and one shard is started for it, and the first shard to
  report idle is reserved for it while the unentered claim is handed back to
  the queue (``WorkerJobSuspension``, available now), where that shard claims
  it. A refused or stopping pool enters parked claims itself, so no job waits
  for capacity that cannot exist. No count, window or delay is configured.
- Warm. A shard forked after the primary imported more modules imports those
  in the fork server first, so later shards share them too.
- Retention. A shard idle for ``TOFU_EXECUTOR_IDLE_SECONDS`` (the existing
  retention of idle executor capacity) closes its gate and exits by itself,
  so retirement never races a claim.
- Control. Command controls reach the primary as before; it answers locally,
  then asks its shards. An unreachable shard makes the answer unavailable,
  never "not found" (contracts/command_control_v1.json). Process-local state
  every Turn depends on is repeated on every ready shard. MCP is not: a shard
  runs no MCP servers and relays to the primary's bridge
  (``lib/mcp/bridge_relay.py``), so ``mcp_reconcile`` applies once, here.
- Lifecycle. The primary is each shard's manager (ResidentService): pipe EOF
  stops a shard whose primary died; the primary's stop signals every shard
  and joins it within the existing native-service stop budget.
- A shard that exits before it was ready stops further spawning for this
  primary incarnation: the primary keeps serving every job itself.

Alternatives considered: sizing a worker pool from the CPU count (forbidden:
hardware never limits work, docs/RESOURCE_CONTROL.md); forking the multi-
threaded primary (inherits held locks; the fork server is a separate
single-threaded image); a prewarmed pool of idle shards (a count knob);
running a burst's extra claims on the primary (the 2026-11-17 behavior: ~2 s
per round while shards boot); migrating a running job between processes (its
entered effects are owned by one process); shards importing the checkout (a
later merge splits one generation across processes); moving only persistence
off the interpreter (at most 1.3x, measured).

Entry points: ShardPool (primary), ShardGate (shard), ExecutionShardService.
"""
from __future__ import annotations

import logging
from collections import deque
import json
import marshal
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid

from serverctl_pkg.resident_service import ResidentService, process_matches

logger = logging.getLogger(__name__)

SHARD_ROLE = 'execution_shard'
RECORD_FORMAT = 'tofu.execution-shard/v1'
HOST_MODULE = 'serverctl_pkg.execution_host'
# One byte per state transition, shard to primary.
REPORT_IDLE, REPORT_BUSY, REPORT_RETIRING = b'i', b'b', b'r'
_SNAPSHOT_PREFIX = 'source-'
# execution_shard_boot.FORK_SERVER_FLAG; that file stays standard-library only.
FORK_SERVER_FLAG = '--fork-server'
# The descriptors ResidentService passes a host, in its pass_fds order.
_DESCRIPTOR_ARGUMENTS = ('--lease-fd', '--manager-fd', '--report-fd')


def import_primary_modules(names) -> None:
    """Import what the primary's jobs had imported, before a shard admits one."""
    import importlib
    import logging
    failed = 0
    for name in names:
        if name in ('__main__', HOST_MODULE):
            continue  # The host module runs as __main__ in a shard; never load a second copy.
        try:
            importlib.import_module(name)
        except Exception:
            failed += 1  # As in the primary, a lazy import fails on use, not here.
    if failed:
        logging.getLogger(__name__).info('execution shard: %d of %d primary modules did not import',
                                         failed, len(names))


_image_environment: dict[str, str] | None = None


def image_environment() -> dict[str, str] | None:
    """The launch environment of a shard forked from the preloaded image, else None.

    Importing the application changes ``os.environ`` (e.g. the Chromium
    library paths), and a shard checks its launch policy against the primary's
    as it was before any import.
    """
    return None if _image_environment is None else dict(_image_environment)


def preload_shard_image(source) -> None:
    """The fork server's image: the application and the primary's modules, no threads."""
    global _image_environment
    _image_environment = dict(os.environ)
    from runtime_guards import load_deployment_configuration
    if load_deployment_configuration().process_role != SHARD_ROLE:
        raise RuntimeError('An execution shard fork server requires the execution_shard process role')
    import server  # noqa: F401
    import_primary_modules(source.loaded_modules)


def _children(pid: int) -> list[int]:
    children = []
    for listing in Path(f'/proc/{pid}/task').glob('*/children'):
        try:
            children.extend(int(child) for child in listing.read_text().split())
        except (OSError, ValueError):
            continue  # A thread or the process exited while listed.
    return children


def fork_server_pids(primary: int) -> list[int]:
    """The primary's running shard fork server (at most one)."""
    servers = []
    for pid in _children(primary):
        try:
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        except OSError:
            continue  # Exited since it was listed.
        if HOST_MODULE.encode() in argv and FORK_SERVER_FLAG.encode() in argv:
            servers.append(pid)
    return servers


def live_shard_pids(primary: int) -> list[int]:
    """Running shard hosts of one primary: every child of its fork server.

    A forked shard keeps the fork server's command line, so it is named by
    ancestry, not argv.
    """
    return [pid for server in fork_server_pids(primary) for pid in _children(server)]


class ExecutionShardService(ResidentService):
    """One shard host, managed by the primary execution host."""
    service_name = 'execution shard'
    state_name = 'execution-shard'
    record_format = RECORD_FORMAT
    host_module = HOST_MODULE

    def __init__(self, checkout: Path, python: str, directory: Path, *, fork=None):
        super().__init__(str(checkout), python)
        self.directory = Path(directory)
        self.state_path = self.directory / f'{self.state_name}.json'
        self.lock_path = self.directory / f'.{self.state_name}.lock'
        self._fork = fork
        self.report_read: int | None = None

    def spawn(self, argv, *, cwd, env, log, pass_fds) -> int:
        # The fork server already is ``launch_command``'s image; it receives
        # only the host arguments and this host's descriptors.
        return self._fork(argv[argv.index(self.host_module) + 1:], pass_fds)

    def host_matches(self, record: dict) -> bool:
        """A forked shard keeps its fork server's command line; pid and start time name it."""
        if record.get('host') != socket.gethostname():
            return False
        if not process_matches(record.get('pid'), record.get('processStartTime')):
            return False
        try:
            argv = Path(f'/proc/{record["pid"]}/cmdline').read_bytes().split(b'\0')
        except OSError:
            return False
        return self.host_module.encode() in argv and FORK_SERVER_FLAG.encode() in argv

    def process_environment(self, environment: dict[str, str]) -> dict[str, str]:
        env = dict(environment)
        env['TOFU_PROCESS_ROLE'] = SHARD_ROLE
        return env

    def launch_python(self, plan: dict) -> str:
        # The primary's admitted interpreter; the shard re-derives the same plan.
        return plan.get('python') or self.python

    def launch_command(self, plan: dict) -> list[str]:
        # Never executed: spawn() hands the host arguments to the fork server.
        return [self.host_module]

    def launch_extras(self) -> tuple[list[str], tuple[int, ...]]:
        read, write = os.pipe()
        self.report_read = read
        return ['--report-fd', str(write)], (write,)


class _Shard:
    def __init__(self, service: ExecutionShardService):
        self.service = service
        self.state = 'starting'
        self.launched = time.monotonic()
        self.endpoint: dict | None = None
        self.reaped = False
        self.reap_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        # A parked claim was handed back for this idle shard to claim.
        self.reserved = False


class _ForkServer:
    """The primary's end of one fork server (execution_shard_boot ``--fork-server``)."""

    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str], log: Path):
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open('ab') as output:
                self._process = subprocess.Popen(
                    [*argv, FORK_SERVER_FLAG, str(theirs.fileno())], cwd=cwd, env=env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True, pass_fds=(theirs.fileno(),))
        except BaseException:
            ours.close()
            raise
        finally:
            theirs.close()
        self._channel = ours
        self.pid = self._process.pid

    def await_ready(self) -> None:
        reply = self._receive()
        if reply.get('ready') is not True:
            raise RuntimeError(f"shard fork server refused: {reply.get('error') or reply}")

    def fork(self, arguments: list[str], descriptors: tuple[int, ...], flags: tuple[str, ...],
             modules: list[str] = ()) -> int:
        """Fork one host; each descriptor replaces the value of its flag in ``arguments``."""
        request = {'argv': list(arguments), 'fdArguments': list(flags), 'modules': list(modules)}
        socket.send_fds(self._channel, [json.dumps(request).encode()], list(descriptors))
        reply = self._receive()
        if type(reply.get('pid')) is not int:
            raise RuntimeError(f"shard fork refused: {reply.get('error') or reply}")
        return reply['pid']

    def _receive(self) -> dict:
        data = self._channel.recv(1 << 16)
        if not data:
            raise RuntimeError('the shard fork server exited')
        return json.loads(data)

    def running(self) -> bool:
        return self._process.poll() is None

    def abort(self) -> None:
        """Any thread: wake a receive blocked on this server; it exits on the EOF."""
        try:
            self._channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # Already shut down or closed.

    def close(self) -> None:
        from serverctl_pkg.restart_budget import NATIVE_SERVICE_STOP_SECONDS
        self.abort()
        self._channel.close()
        try:
            self._process.wait(NATIVE_SERVICE_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()


class _Placement:
    """One owned claim's placement: entered here, or released for a shard."""

    def __init__(self):
        self.entered = False
        self.released = False
        self.decided = threading.Event()


class ShardPool:
    """The primary's claim gate, command-control fan-out and shard lifecycle."""

    def __init__(self, *, source, plan: dict, checkout: Path, python: str,
                 launch_environment: dict[str, str], directory: Path, idle_seconds: int = 0,
                 clock=time.monotonic):
        from lib.runtime.pinned_helper_source import PinnedHelperSource
        # Read at the primary's boot, like every helper spawned from loaded source.
        self._boot = PinnedHelperSource(Path(__file__).with_name('execution_shard_boot.py'))
        self._source, self._plan = source, plan
        self._checkout, self._python = Path(checkout), python
        self._environment = dict(launch_environment)
        self._directory = Path(directory)
        self._lock = threading.Lock()
        self._shards: list[_Shard] = []
        self._closed = False
        self._refusal = ''
        # Claims this primary entered, and claims parked until a shard is idle.
        self._entered = 0
        self._parked: deque[_Placement] = deque()
        self._interrupted = False
        # One fork server while shards exist; it holds the preloaded image.
        self._fork_lock = threading.Lock()
        self._fork_server: _ForkServer | None = None
        self._warming: threading.Thread | None = None
        self._retiring: threading.Thread | None = None
        self._idle_seconds, self._clock = idle_seconds, clock
        self._idle_since: float | None = None
        self._wake = lambda: None
        self._retire_orphans()

    # ── claim gate ─────────────────────────────────────────────────────────
    def bind(self, wake) -> None:
        self._wake = wake

    def admit(self, *, running: int, inflight: int) -> tuple[bool, float | None]:
        with self._lock:
            # A free primary claims at once; a busy one leaves arrivals to an
            # idle shard. Every claim it takes beside its own job is placed.
            idle = any(shard.state == 'idle' for shard in self._shards)
            return self._entered == 0 or not idle, self._retire_wait_locked()

    def _retire_wait_locked(self) -> float | None:
        """Seconds until the idle fork server retires; retires it once due.

        It lives while this host has work, plus the executor idle retention
        (``TOFU_EXECUTOR_IDLE_SECONDS``, zero keeps it), like the shards it forks.
        """
        if self._fork_server is None or not self._idle_seconds or self._retiring is not None:
            self._idle_since = None
            return None
        if self._shards or self._entered or self._parked:
            self._idle_since = None
            return None
        now = self._clock()
        if self._idle_since is None:
            self._idle_since = now
        remaining = self._idle_since + self._idle_seconds - now
        if remaining > 0:
            return remaining
        self._idle_since = None
        self._retiring = threading.Thread(target=self._retire_fork_server, daemon=True,
                                          name='tofu-execution-fork-server-retire')
        self._retiring.start()
        return None

    def place(self, context, *, stopping) -> _Placement:
        """Decide where one owned, unentered claim runs; blocks until decided.

        The primary enters a claim while it runs no other (or no shard can
        exist). Otherwise the claim is parked under its lease and one shard is
        started for it; the first of three ends the wait: a shard reports idle
        (the claim is handed back to the queue, which that shard claims from),
        the primary's own job ends (it enters the claim), or shards are refused.
        """
        ticket = _Placement()
        with self._lock:
            if stopping.is_set() or self._interrupted:
                ticket.released = True
                ticket.decided.set()
            elif self._entered == 0 or self._refusal or self._closed:
                self._enter_locked(ticket)
            else:
                self._parked.append(ticket)
                self._idle_since = None
                self._provision_locked()
                self._dispatch_locked()
        ticket.decided.wait()
        return ticket

    def leave(self, ticket: _Placement) -> None:
        """A claim this process entered has ended; a parked one may enter."""
        if not ticket.entered:
            return
        with self._lock:
            self._entered -= 1
            self._dispatch_locked()

    def interrupt(self) -> None:
        """The claim coordinator is stopping: every parked claim returns to the queue."""
        with self._lock:
            self._interrupted = True
            self._dispatch_locked()

    def _enter_locked(self, ticket: _Placement) -> None:
        ticket.entered = True
        self._entered += 1
        self._idle_since = None
        ticket.decided.set()
        if (self._fork_server is None and self._warming is None
                and not self._closed and not self._refusal and not self._interrupted):
            self._warming = threading.Thread(target=self._warm_fork_server, daemon=True,
                                             name='tofu-execution-fork-server-warm')
            self._warming.start()

    def _dispatch_locked(self) -> None:
        while self._parked:
            if self._interrupted:
                ticket = self._parked.popleft()
                ticket.released = True
                ticket.decided.set()
                continue
            if self._entered == 0 or self._refusal or self._closed:
                self._enter_locked(self._parked.popleft())
                continue
            shard = next((shard for shard in self._shards
                          if shard.state == 'idle' and not shard.reserved), None)
            if shard is None:
                return
            # Handed back: the idle shard's coordinator claims from the queue.
            shard.reserved = True
            ticket = self._parked.popleft()
            ticket.released = True
            ticket.decided.set()

    def _provision_locked(self) -> None:
        """One shard starting or idle-unreserved for every parked claim."""
        if self._closed or self._refusal:
            return
        available = sum(1 for shard in self._shards if shard.state == 'starting'
                        or (shard.state == 'idle' and not shard.reserved))
        for _ in range(len(self._parked) - available):
            shard = _Shard(self._new_service())
            self._shards.append(shard)
            # Started under the lock, so join() always finds the launch thread.
            self._start_thread(shard, self._launch,
                               f'tofu-execution-shard-launch-{shard.service.directory.name}')

    # ── command controls ───────────────────────────────────────────────────
    def apply_command_control(self, method, task_id, *, user_id, deadline_unix_ms=None, **params):
        from lib.tasks_pkg.command_controls import apply_command_control, merge_background_activity
        from lib.tasks_pkg.command_control_transport import CommandControlUnavailable, call
        local = apply_command_control(method, task_id, user_id=user_id,
                                      deadline_unix_ms=deadline_unix_ms, **params)
        if method == 'storage_rpc_metrics':
            # Process-wide latency of this incarnation; shards keep their own.
            return local
        if method == 'mcp_reconcile':
            # Shards relay to this host's MCP bridge (lib/mcp/bridge_relay.py).
            return local
        endpoints = self._endpoints()
        if method == 'device_policy_reconcile':
            # The device policy cache is process-local: every process that
            # runs Turns drops it, or the answer is not applied.
            applied = local['applied']
            for endpoint in endpoints:
                answer = call(endpoint, method, task_id, user_id=user_id, params=params,
                              deadline_unix_ms=deadline_unix_ms)
                applied = applied and answer['applied']
            return {'applied': applied}
        if method == 'background_activity':
            views = [local]
            for endpoint in endpoints:
                views.append(call(endpoint, method, task_id, user_id=user_id, params=params,
                                  deadline_unix_ms=deadline_unix_ms))
            return merge_background_activity(*views)
        if local is not None:
            return local
        unavailable = False
        for endpoint in endpoints:
            try:
                result = call(endpoint, method, task_id, user_id=user_id, params=params,
                              deadline_unix_ms=deadline_unix_ms)
            except CommandControlUnavailable:
                unavailable = True
                continue
            if result is not None:
                return result
        if unavailable:
            raise CommandControlUnavailable('An execution shard did not answer')
        return None

    def _endpoints(self) -> list[dict]:
        with self._lock:
            shards = [shard for shard in self._shards if shard.state != 'exited']
        endpoints = []
        for shard in shards:
            endpoint = shard.endpoint
            if endpoint is None:
                # Published but not yet reported: the shard may already hold a job.
                record = shard.service.record()
                if record.get('state') == 'ready' and isinstance(record.get('commandControl'), dict):
                    endpoint = record['commandControl']
            if endpoint is not None:
                endpoints.append(endpoint)
        return endpoints

    def snapshot(self) -> dict:
        with self._lock:
            states = [shard.state for shard in self._shards]
        return {'shards': len(states), 'idle': states.count('idle'), 'busy': states.count('busy'),
                'starting': states.count('starting'), 'refusal': self._refusal}

    # ── lifecycle ─────────────────────────────────────────────────────────
    def begin_stop(self) -> None:
        """Close admission and signal every shard; joins nothing."""
        with self._lock:
            self._closed = True
            shards = list(self._shards)
            self._dispatch_locked()
        server = self._fork_server
        if server is not None:
            server.abort()  # A launch waiting for its image fails at once.
        for shard in shards:
            self._signal(shard, signal.SIGTERM)

    def join(self) -> None:
        """Reap every shard within the native-service stop budget, then drop the snapshot."""
        self.begin_stop()
        with self._lock:
            shards = list(self._shards)
        # A launch in flight finishes first; the shard it started is signalled below.
        for shard in shards:
            shard.threads[0].join()
        self.begin_stop()
        reapers = [threading.Thread(target=self._reap, args=(shard,), daemon=True,
                                    name='tofu-execution-shard-stop') for shard in shards]
        for reaper in reapers:
            reaper.start()
        for shard in shards:
            for thread in shard.threads:
                thread.join()
        for reaper in reapers:
            reaper.join()
        with self._lock:
            helpers = [thread for thread in (self._warming, self._retiring) if thread is not None]
        for thread in helpers:
            thread.join()
        with self._fork_lock:
            server, self._fork_server = self._fork_server, None
        if server is not None:
            server.close()

    def _new_service(self) -> ExecutionShardService:
        directory = self._directory / uuid.uuid4().hex
        return ExecutionShardService(self._checkout, self._python, directory, fork=self._fork)

    def _fork(self, arguments: list[str], descriptors: tuple[int, ...]) -> int:
        """Fork one shard host from the preloaded image."""
        with self._fork_lock:
            server = self._ensure_fork_server_locked()
            return server.fork(arguments, descriptors, _DESCRIPTOR_ARGUMENTS,
                               self._source.loaded_module_names())

    def _ensure_fork_server_locked(self) -> _ForkServer:
        server = self._fork_server
        if server is not None and server.running():
            return server
        if server is not None:
            server.close()
            self._fork_server = None
        with self._lock:
            if self._closed:
                raise RuntimeError('execution shards are stopping')
        # A fresh export names every module the primary has imported by now.
        snapshot = self._export_snapshot()
        try:
            service = self._new_service()
            server = _ForkServer(
                [service.launch_python(self._plan), self._boot.path(), str(snapshot), HOST_MODULE],
                cwd=self._checkout, env=service.process_environment(self._environment),
                log=self._checkout / 'logs' / 'server-console.log')
            self._fork_server = server
            started = time.monotonic()
            server.await_ready()
        except BaseException:
            if self._fork_server is not None:
                self._fork_server.close()
                self._fork_server = None
            raise
        finally:
            # The server loaded it whole; forked shards never read it.
            snapshot.unlink(missing_ok=True)
        logger.info('[ExecutionShards] fork server pid=%s ready after %.1fs',
                    server.pid, time.monotonic() - started)
        return server

    def _warm_fork_server(self) -> None:
        """Boot the image while this host's first job runs, before a second one needs a shard."""
        try:
            with self._fork_lock:
                self._ensure_fork_server_locked()
            with self._lock:
                self._warming = None
        except Exception as exc:
            with self._lock:
                if not self._closed and not self._refusal:
                    self._refusal = f'fork server failed: {type(exc).__name__}: {exc}'[:300]
                    logger.error('[ExecutionShards] %s; this execution host now serves every job '
                                 'itself until it restarts', self._refusal)
                self._warming = None
                self._dispatch_locked()
            self._wake()

    def _retire_fork_server(self) -> None:
        """Idle past the executor idle retention with no shard: the image goes."""
        try:
            with self._fork_lock:
                with self._lock:
                    if self._shards or self._entered or self._parked:
                        return  # Work arrived meanwhile; the next idle period retires it.
                    server, self._fork_server = self._fork_server, None
                if server is not None:
                    server.close()
                    logger.info('[ExecutionShards] fork server pid=%s retired', server.pid)
        finally:
            with self._lock:
                self._retiring = None

    def _start_thread(self, shard, target, name):
        thread = threading.Thread(target=target, args=(shard,), name=name, daemon=True)
        shard.threads.append(thread)
        thread.start()

    def _export_snapshot(self) -> Path:
        """Write the retained generation (tens of MB) to a private file for one fork server."""
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self._directory / f'{_SNAPSHOT_PREFIX}{uuid.uuid4().hex}.marshal'
        staged = target.with_suffix('.staging')
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                marshal.dump(self._source.snapshot(), stream)
            os.replace(staged, target)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return target

    def _launch(self, shard: _Shard) -> None:
        service = shard.service
        try:
            service.directory.mkdir(mode=0o700, parents=True)
            environment = dict(self._environment)
            with self._lock:
                if self._closed:
                    raise RuntimeError('execution shards are stopping')
            service.ensure(self._plan, environment)
        except Exception as exc:
            if service.report_read is not None:
                os.close(service.report_read)
            self._exited(shard, ready=False, reason=f'launch failed: {type(exc).__name__}: {exc}')
            shutil.rmtree(service.directory, ignore_errors=True)
            return
        logger.info('[ExecutionShards] started shard pid=%s (%s)',
                    service.record().get('pid'), self.snapshot())
        self._start_thread(shard, self._observe, f'tofu-execution-shard-{service.directory.name}')

    def _observe(self, shard: _Shard) -> None:
        """Follow the shard's reports; EOF means its process is gone."""
        descriptor = shard.service.report_read
        ready = retiring = False
        try:
            while True:
                data = os.read(descriptor, 64)
                if not data:
                    break
                state = {REPORT_IDLE: 'idle', REPORT_BUSY: 'busy',
                         REPORT_RETIRING: 'retiring'}.get(data[-1:])
                if state is None:
                    continue
                if not ready and state != 'retiring':
                    record = shard.service.record()
                    shard.endpoint = record.get('commandControl') if record.get('state') == 'ready' else None
                    ready = shard.endpoint is not None
                    if ready:
                        logger.info('[ExecutionShards] shard pid=%s ready after %.1fs', record.get('pid'),
                                    time.monotonic() - shard.launched)
                retiring = retiring or state == 'retiring'
                self._reported(shard, state)
        except OSError as exc:
            logger.warning('[ExecutionShards] report channel failed: %s', exc)
        finally:
            os.close(descriptor)
        failure = str(shard.service.record().get('error') or '')[:300]
        self._reap(shard)
        if ready and not retiring:
            with self._lock:
                closed = self._closed
            if not closed:
                logger.warning('[ExecutionShards] shard exited while serving; its claims '
                               'are taken over once its exit is proved')
            # Its foreground commands can no longer deliver output: retire their
            # groups now, as boot does for a dead execution host, before its
            # claims are taken over.
            self._retire_foreground_commands_of_dead_shards()
        self._exited(shard, ready=ready,
                     reason='exited before it was ready' + (f' ({failure})' if failure else ''))

    @staticmethod
    def _retire_foreground_commands_of_dead_shards() -> None:
        from lib.project_mod.foreground_command_records import retire_orphaned_foreground_commands
        try:
            counts = retire_orphaned_foreground_commands()
        except Exception as exc:
            # The next execution-host boot sweep retries every kept record.
            logger.warning('[ExecutionShards] command retirement after a shard exit failed: %s', exc)
            return
        if counts.get('retired') or counts.get('kept'):
            logger.warning('[ExecutionShards] after a shard exit: %s', counts)

    def _reported(self, shard: _Shard, state: str) -> None:
        with self._lock:
            if shard.state != 'exited':
                shard.state = state
                if state != 'idle':
                    # It claimed (or stopped claiming): its reservation is spent.
                    shard.reserved = False
                self._dispatch_locked()
                self._provision_locked()
        self._wake()

    def _exited(self, shard: _Shard, *, ready: bool, reason: str) -> None:
        with self._lock:
            shard.state = 'exited'
            if shard in self._shards:
                self._shards.remove(shard)
            if not ready and not self._closed and not self._refusal:
                self._refusal = reason
                logger.error('[ExecutionShards] a shard %s; this execution host now serves '
                             'every job itself until it restarts', reason)
            self._provision_locked()
            self._dispatch_locked()
        self._wake()

    def _signal(self, shard: _Shard, number: int) -> None:
        record = shard.service.record()
        if record and shard.service.host_matches(record):
            try:
                os.kill(record['pid'], number)
            except ProcessLookupError:
                pass

    def _reap(self, shard: _Shard) -> None:
        from serverctl_pkg.restart_budget import NATIVE_SERVICE_STOP_SECONDS
        with shard.reap_lock:
            if shard.reaped:
                return
            try:
                if shard.service.record():
                    retired = shard.service.stop(NATIVE_SERVICE_STOP_SECONDS, retire_failed_close=True)
                    if retired:
                        logger.warning('[ExecutionShards] shard close: %s', retired)
            except Exception as exc:
                # The process lease outlived the stop budget: keep its directory
                # so the next primary finds the record and its lease.
                logger.error('[ExecutionShards] shard stop failed: %s', exc)
                return
            finally:
                shard.reaped = True
            shutil.rmtree(shard.service.directory, ignore_errors=True)

    def _retire_orphans(self) -> None:
        """Remove what a previous primary left; a still-draining shard keeps its own."""
        if not self._directory.is_dir():
            return
        live = False
        for entry in self._directory.iterdir():
            if entry.is_dir():
                service = ExecutionShardService(self._checkout, self._python, entry)
                record = service.record()
                if record and service.host_matches(record):
                    live = True
                    continue
                shutil.rmtree(entry, ignore_errors=True)
        if not live:
            for entry in self._directory.glob(f'{_SNAPSHOT_PREFIX}*'):
                entry.unlink(missing_ok=True)


class ShardGate:
    """A shard's claim gate: one job at a time, idle reports, self-retirement."""

    def __init__(self, report_fd: int, *, idle_seconds: int, retire, clock=time.monotonic):
        self._fd = report_fd
        self._idle_seconds = idle_seconds
        self._retire = retire
        self._clock = clock
        self._lock = threading.Lock()
        self._open = self._closed = False
        self._idle_since: float | None = None
        self._wanted: bytes | None = None
        self._reported: bytes | None = None
        self._wake = lambda: None

    def bind(self, wake) -> None:
        self._wake = wake

    def open(self) -> None:
        """Admit claims once this shard's control endpoint is published."""
        with self._lock:
            self._open = True
        self._wake()

    def close(self) -> None:
        """Stop claiming; the primary stops counting this shard as idle."""
        with self._lock:
            self._closed = True
            self._report_locked(REPORT_RETIRING)
        self._wake()

    def admit(self, *, running: int, inflight: int) -> tuple[bool, float | None]:
        retire = False
        with self._lock:
            if not self._open or self._closed:
                self._report_locked(self._wanted)
                return False, None
            if inflight:
                self._idle_since = None
                if running:
                    self._report_locked(REPORT_BUSY)
                return running == 0, None
            now = self._clock()
            if self._idle_since is None:
                self._idle_since = now
            if self._idle_seconds and now - self._idle_since >= self._idle_seconds:
                self._closed = retire = True
                self._report_locked(REPORT_RETIRING)
            else:
                self._report_locked(REPORT_IDLE)
        if retire:
            self._retire()
            return False, None
        if not self._idle_seconds:
            return True, None
        return True, max(0.0, self._idle_since + self._idle_seconds - now)

    def _report_locked(self, state: bytes | None) -> None:
        if state is None:
            return
        self._wanted = state
        if self._reported == state or self._fd is None:
            return
        try:
            os.write(self._fd, state)
        except BlockingIOError:
            return  # The latest wanted state is written on the next pass.
        except OSError:
            # The primary is gone; its manager pipe stops this host as well.
            self._fd = None
            return
        self._reported = state
