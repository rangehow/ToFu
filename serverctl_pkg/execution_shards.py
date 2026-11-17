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
  ``PinnedImportSource`` retained, exported once to a private snapshot and
  served by ``execution_shard_boot.py`` (spawned from ``PinnedHelperSource``):
  a merge that rewrote the checkout after the primary booted never mixes
  revisions across processes. Its plan must equal the primary's.
- Demand. Every job the primary claims while it already runs one (no shard
  was idle) starts one shard for that concurrency. No count is configured:
  the processes follow the concurrent jobs. Jobs never migrate: a burst that
  arrives before its shards are ready runs on the primary; later turns of
  those sessions find the shards idle.
- Placement. Each process's ``DurableWorkerService`` asks its claim gate before
  a probe. A shard probes only while it runs nothing (one job per shard); the
  primary probes while it runs nothing or no shard is idle, so a job is never
  left waiting for capacity that does not exist. Shards report idle/busy over
  a private pipe; its EOF is the shard's exit.
- Warm. The snapshot names the application modules the primary had imported;
  a shard imports them before it admits work, so its first job is as warm as
  the primary's (measured: second-Turn TTFR at 20 sessions 2.7 s -> 0.46 s).
  The export runs outside the pool lock the coordinator takes on every pass.
- Overflow is literal: a next Turn claimed while the previous job's tail
  (receipts after settlement) still runs counts too. The extra shard serves
  later arrivals and retires when idle; no window or delay filters it.
- Retention. A shard idle for ``TOFU_EXECUTOR_IDLE_SECONDS`` (the existing
  retention of idle executor capacity) closes its gate and exits by itself,
  so retirement never races a claim.
- Control. Command controls reach the primary as before; it answers locally,
  then asks its shards. An unreachable shard makes the answer unavailable,
  never "not found" (contracts/command_control_v1.json). Process-local state
  every Turn depends on (the MCP bridge: ``mcp_reconcile``) is repeated on
  every ready shard; a shard still starting reads the owner's stored MCP rows
  in its own boot.
- Lifecycle. The primary is each shard's manager (ResidentService): pipe EOF
  stops a shard whose primary died; the primary's stop signals every shard
  and joins it within the existing native-service stop budget.
- A shard that exits before it was ready stops further spawning for this
  primary incarnation: the primary keeps serving every job itself.

Alternatives considered: sizing a worker pool from the CPU count (forbidden:
hardware never limits work, docs/RESOURCE_CONTROL.md); forking the multi-
threaded primary (inherits held locks); shards importing the checkout (a later
merge splits one generation across processes); moving only persistence off
the interpreter (at most 1.3x, measured).

Entry points: ShardPool (primary), ShardGate (shard), ExecutionShardService.
"""
from __future__ import annotations

import logging
import marshal
import os
from pathlib import Path
import shutil
import signal
import threading
import time
import uuid

from serverctl_pkg.resident_service import ResidentService

logger = logging.getLogger(__name__)

SHARD_ROLE = 'execution_shard'
RECORD_FORMAT = 'tofu.execution-shard/v1'
HOST_MODULE = 'serverctl_pkg.execution_host'
# One byte per state transition, shard to primary.
REPORT_IDLE, REPORT_BUSY, REPORT_RETIRING = b'i', b'b', b'r'
_SNAPSHOT_PREFIX = 'source-'


def live_shard_pids(primary: int) -> list[int]:
    """Running shard hosts of one primary: its children that run the host module as shards."""
    children = []
    for listing in Path(f'/proc/{primary}/task').glob('*/children'):
        try:
            children.extend(int(pid) for pid in listing.read_text().split())
        except (OSError, ValueError):
            continue  # A thread or the primary exited while listed.
    shards = []
    for pid in children:
        try:
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        except OSError:
            continue  # Exited since it was listed.
        if HOST_MODULE.encode() in argv and b'--report-fd' in argv:
            shards.append(pid)
    return shards


class ExecutionShardService(ResidentService):
    """One shard host, managed by the primary execution host."""
    service_name = 'execution shard'
    state_name = 'execution-shard'
    record_format = RECORD_FORMAT
    host_module = HOST_MODULE

    def __init__(self, checkout: Path, python: str, directory: Path, *, boot: str, snapshot: Path):
        super().__init__(str(checkout), python)
        self.directory = Path(directory)
        self.state_path = self.directory / f'{self.state_name}.json'
        self.lock_path = self.directory / f'.{self.state_name}.lock'
        self._boot, self._snapshot = boot, snapshot
        self.report_read: int | None = None

    def process_environment(self, environment: dict[str, str]) -> dict[str, str]:
        env = dict(environment)
        env['TOFU_PROCESS_ROLE'] = SHARD_ROLE
        return env

    def launch_python(self, plan: dict) -> str:
        # The primary's admitted interpreter; the shard re-derives the same plan.
        return plan.get('python') or self.python

    def launch_command(self, plan: dict) -> list[str]:
        return [self._boot, str(self._snapshot), self.host_module]

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


class ShardPool:
    """The primary's claim gate, command-control fan-out and shard lifecycle."""

    def __init__(self, *, source, plan: dict, checkout: Path, python: str,
                 launch_environment: dict[str, str], directory: Path):
        from lib.runtime.pinned_helper_source import PinnedHelperSource
        # Read at the primary's boot, like every helper spawned from loaded source.
        self._boot = PinnedHelperSource(Path(__file__).with_name('execution_shard_boot.py'))
        self._source, self._plan = source, plan
        self._checkout, self._python = Path(checkout), python
        self._environment = dict(launch_environment)
        self._directory = Path(directory)
        self._snapshot: Path | None = None
        self._lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._shards: list[_Shard] = []
        self._closed = False
        self._refusal = ''
        self._running = 0
        self._wake = lambda: None
        self._retire_orphans()

    # ── claim gate ─────────────────────────────────────────────────────────
    def bind(self, wake) -> None:
        self._wake = wake

    def admit(self, *, running: int, inflight: int) -> tuple[bool, float | None]:
        with self._lock:
            idle = sum(1 for shard in self._shards if shard.state == 'idle')
            # A job the primary claimed beside another found no idle shard:
            # that concurrency gets a shard of its own for the next arrival.
            overflow = running - max(1, self._running)
            self._running = running
            if overflow > 0 and not self._closed and not self._refusal:
                for _ in range(overflow):
                    shard = _Shard(self._new_service())
                    self._shards.append(shard)
                    # Started under the lock, so join() always finds the launch thread.
                    self._start_thread(shard, self._launch,
                                       f'tofu-execution-shard-launch-{shard.service.directory.name}')
            admitted = running == 0 or idle == 0
        return admitted, None

    # ── command controls ───────────────────────────────────────────────────
    def apply_command_control(self, method, task_id, *, user_id, deadline_unix_ms=None, **params):
        from lib.tasks_pkg.command_controls import apply_command_control, merge_background_activity
        from lib.tasks_pkg.command_control_transport import CommandControlUnavailable, call
        local = apply_command_control(method, task_id, user_id=user_id,
                                      deadline_unix_ms=deadline_unix_ms, **params)
        if method == 'storage_rpc_metrics':
            # Process-wide latency of this incarnation; shards keep their own.
            return local
        endpoints = self._endpoints()
        if method == 'mcp_reconcile':
            # An MCP bridge is process-local: every process that runs Turns
            # repeats the owner's action. A shard that cannot confirm makes the
            # whole answer unconfirmed rather than a silent partial success.
            scheduled = local['scheduled']
            for endpoint in endpoints:
                answer = call(endpoint, method, task_id, user_id=user_id, params=params,
                              deadline_unix_ms=deadline_unix_ms)
                scheduled = scheduled and answer['scheduled']
            return {'scheduled': scheduled}
        if method == 'device_policy_reconcile':
            # The device policy cache is process-local too: every process that
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
            snapshot, self._snapshot = self._snapshot, None
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)

    def _new_service(self) -> ExecutionShardService:
        directory = self._directory / uuid.uuid4().hex
        return ExecutionShardService(self._checkout, self._python, directory,
                                     boot='', snapshot=Path())

    def _start_thread(self, shard, target, name):
        thread = threading.Thread(target=target, args=(shard,), name=name, daemon=True)
        shard.threads.append(thread)
        thread.start()

    def _snapshot_path(self) -> Path:
        # Its own lock: exporting the generation (tens of MB) never holds the
        # pool lock the claim coordinator takes on every pass.
        with self._snapshot_lock:
            with self._lock:
                if self._snapshot is not None:
                    return self._snapshot
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
            with self._lock:
                self._snapshot = target
            return target

    def _launch(self, shard: _Shard) -> None:
        service = shard.service
        try:
            service._snapshot = self._snapshot_path()
            service._boot = self._boot.path()
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
                with self._lock:
                    if shard.state != 'exited':
                        shard.state = state
                self._wake()
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
        self._exited(shard, ready=ready,
                     reason='exited before it was ready' + (f' ({failure})' if failure else ''))

    def _exited(self, shard: _Shard, *, ready: bool, reason: str) -> None:
        with self._lock:
            shard.state = 'exited'
            if shard in self._shards:
                self._shards.remove(shard)
            if not ready and not self._closed and not self._refusal:
                self._refusal = reason
                logger.error('[ExecutionShards] a shard %s; this execution host now serves '
                             'every job itself until it restarts', reason)
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
                service = ExecutionShardService(self._checkout, self._python, entry, boot='', snapshot=Path())
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
