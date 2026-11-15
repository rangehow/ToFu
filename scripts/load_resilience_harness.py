#!/usr/bin/env python3
"""Opt-in fault-injection harness: does an isolated Tofu instance stay responsive under load?

Responsibility: start ONE isolated native storage authority through the
production ``StorageSupervisor``/``StorageRuntime`` (its own data directory,
project lease, loopback port and pinned engine copy) and drive a scripted
conversation through the production Turn lifecycle. Every Turn records streamed
events, runs one real tool call through the production ``run_command`` launch
boundary and settles. Faults are injected deterministically, never by loading
the host:

  baseline  no fault
  sigstop   SIGSTOP the engine for longer than the liveness budget, then SIGCONT
            (a frozen engine that makes no progress)
  slow      every engine socket receive waits LR_SOCKET_DELAY_MS first: the
            engine answers every request, slower than the probe deadline
  volume    engine file calls under its data directory block in read(2) until
            released (a stalled data volume)
  lease     a forked helper inherits the engine's project lease and ignores
            termination signals, then the engine is frozen (the 2026-11-11
            unreapable-engine chain)
  cpu       CPU-bound competitors launched through the run_command boundary,
            confined with the instance to a small CPU island

Entry:
    python3 scripts/load_resilience_harness.py --root DIR --engine PATH
        [--scenarios baseline,slow,sigstop,volume,lease,cpu] [--nice 10]
        [--cpus 126,127] [--competitors 8] [--fault-seconds 150]

Outputs ``DIR/results/<run-id>/report.json`` and ``report.md``: per phase
(warmup, fault, recovery) the Turn latency split into storage/control and tool
time, per-operation storage latency, liveness probe failures, engine recycles,
restarts and breaker trips.

Safety: DIR is refused when it looks like a live Tofu data root; the measuring
child gets a scrubbed environment whose data root, HOME and TMPDIR live under
DIR; the engine binary is pinned by copy and SHA-256. The child and everything
it starts (engine, tool calls, competitors) run at ``--nice`` (default 10, so
they always yield to a production server at nice 0) inside the ``--cpus``
island. An unprivileged process cannot rank below nice 19, so the instance
sits above that floor to make CPU ordering against agent commands observable.
Decision record: docs/RESOURCE_CONTROL.md, "Tofu's control plane runs ahead of
agent batch work".

Dependencies: scripts.tofudb_load_test (isolation helpers), lib.storage,
lib.turn_lifecycle, lib.durable_worker, lib.project_mod.run_command, a C
compiler for tests/fixtures/load_resilience/stallshim.c.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

USER_ID = 1
SHIM_SOURCE = PROJECT_ROOT / 'tests/fixtures/load_resilience/stallshim.c'
SCENARIOS = ('baseline', 'slow', 'sigstop', 'volume', 'lease', 'cpu')
TOOL_COMMAND = 'python3 -c "print(sum(range(300000)))"'
COMPETITOR_COMMAND = 'exec python3 -c "while True: pass"'

# Supervisor/runtime log lines that are the evidence of each liveness outcome.
EVENT_PATTERNS = {
    'probe_failed': re.compile(r'liveness probe failed'),
    'probe_slow': re.compile(r'answered its liveness probe after'),
    # Not the recycle line, which says "no thread is blocked in the kernel".
    'withheld': re.compile(r'withholding recycle|and is blocked in the kernel|keeping the attachment'),
    'recycle': re.compile(r'terminating so the exit path|forcing a bounded recycle|stopped answering'),
    'unreapable': re.compile(r'unreapable'),
    'crash': re.compile(r'exited unexpectedly'),
    'restart_attempt': re.compile(r'restart attempt \d+ starting'),
    'restart_failed': re.compile(r'restart attempt \d+ failed'),
    'restart_succeeded': re.compile(r'restart succeeded'),
    'breaker': re.compile(r'circuit breaker tripped'),
    'deferred': re.compile(r'[Dd]eferr'),
}


# ── parent ──────────────────────────────────────────────────────────────────

def _compile_shim(root: Path) -> Path:
    compiler = shutil.which('cc')
    if not compiler:
        raise SystemExit('a C compiler (cc) is required for the fault shim')
    shim = root / 'fault' / 'stallshim.so'
    shim.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run([compiler, '-shared', '-fPIC', '-O2', str(SHIM_SOURCE), '-o', str(shim), '-ldl'],
                   check=True, timeout=120)
    return shim


def _fault_environment(root: Path, shim: Path, socket_delay_ms: int) -> dict[str, str]:
    fault = root / 'fault'
    return {
        'LD_PRELOAD': str(shim),
        'LR_SOCKET_TRIGGER': str(fault / 'socket.trigger'),
        'LR_SOCKET_DELAY_MS': str(socket_delay_ms),
        'LR_VOLUME_TRIGGER': str(fault / 'volume.trigger'),
        'LR_VOLUME_FIFO': str(fault / 'volume.fifo'),
        'LR_VOLUME_DIR': str(root / 'project' / 'data'),
        'LR_LEASE_TRIGGER': str(fault / 'lease.trigger'),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', required=True, help='private working directory (created 0700)')
    parser.add_argument('--engine', default=os.environ.get('TOFUDB_BIN', ''),
                        help='source-matched tofu-db binary to pin (default: $TOFUDB_BIN)')
    parser.add_argument('--scenarios', default='baseline,slow,sigstop,volume,cpu')
    parser.add_argument('--warmup-seconds', type=float, default=20.0)
    parser.add_argument('--fault-seconds', type=float, default=150.0)
    parser.add_argument('--recovery-seconds', type=float, default=45.0)
    parser.add_argument('--nice', type=int, default=10)
    parser.add_argument('--cpus', default='', help='CPU island, e.g. 126,127 (default: last two allowed CPUs)')
    parser.add_argument('--competitors', type=int, default=8)
    parser.add_argument('--socket-delay-ms', type=int, default=1000)
    parser.add_argument('--run-id', default='')
    parser.add_argument('--label', default='', help='free-form label recorded in the report (e.g. before/after)')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child_main(args)
    from scripts.tofudb_load_test import _child_environment, _pin_engine, _refuse_live_root
    unknown = sorted(set(args.scenarios.split(',')) - set(SCENARIOS))
    if unknown:
        raise SystemExit(f'unknown scenarios: {unknown}; choose from {SCENARIOS}')
    if not 0 <= args.nice <= 19:
        raise SystemExit('--nice must be within 0..19')
    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    _refuse_live_root(root)
    (root / '.tofudb-load-test').touch()
    if not args.engine or not Path(args.engine).is_file():
        raise SystemExit('--engine (or $TOFUDB_BIN) must name a source-matched tofu-db binary')
    engine, digest = _pin_engine(Path(args.engine), root)
    shim = _compile_shim(root)
    allowed = sorted(os.sched_getaffinity(0))
    island = ([int(cpu) for cpu in args.cpus.split(',') if cpu.strip()] if args.cpus else allowed[-2:])
    if not set(island) <= set(allowed):
        raise SystemExit(f'--cpus {island} is outside the allowed CPU set')
    run_id = args.run_id or time.strftime('%Y%m%d-%H%M%S')
    env = _child_environment(root.resolve(), engine, digest, 'normal')
    env.update(_fault_environment(root.resolve(), shim, args.socket_delay_ms))
    # Liveness settings of the code under test pass through, so a run can pin them.
    env.update({key: value for key, value in os.environ.items()
                if key.startswith('TOFU_STORAGE_LIVENESS_')})
    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--root', str(root.resolve()),
               '--scenarios', args.scenarios, '--warmup-seconds', str(args.warmup_seconds),
               '--fault-seconds', str(args.fault_seconds), '--recovery-seconds', str(args.recovery_seconds),
               '--nice', str(args.nice), '--cpus', ','.join(map(str, island)),
               '--competitors', str(args.competitors), '--socket-delay-ms', str(args.socket_delay_ms),
               '--run-id', run_id, '--label', args.label, '--engine', str(engine)]
    return subprocess.run(command, env=env, cwd=str(root.resolve())).returncode


# ── child: measurement ──────────────────────────────────────────────────────

class EventLog(logging.Handler):
    """Timestamped storage lifecycle evidence from the production loggers."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        # logging.Handler.handle() already holds self.lock around emit().
        self.events_lock = threading.Lock()
        self.events: list[tuple[float, str, str]] = []

    def emit(self, record):
        if not record.name.startswith('tofu.storage'):
            return
        message = record.getMessage()
        with self.events_lock:
            self.events.append((time.monotonic(), record.levelname, message[:400]))

    def count(self, start: float, end: float) -> dict[str, int]:
        with self.events_lock:
            window = [message for at, _level, message in self.events if start <= at < end]
        return {name: sum(1 for message in window if pattern.search(message))
                for name, pattern in EVENT_PATTERNS.items()}

    def lines(self, start: float, end: float, limit: int = 40) -> list[str]:
        with self.events_lock:
            window = [f'{at - start:+8.1f}s {level} {message}' for at, level, message in self.events
                      if start <= at < end and level in ('WARNING', 'ERROR', 'CRITICAL')]
        return window[:limit // 2] + (['…'] if len(window) > limit else []) + window[-limit // 2:] \
            if len(window) > limit else window


class Samples:
    def __init__(self):
        self.lock = threading.Lock()
        self.ops: list[tuple[float, str, float, str]] = []
        self.turns: list[dict] = []

    def timed(self, op, fn, *args, **kwargs):
        started = time.monotonic()
        outcome = 'ok'
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            outcome = str(getattr(exc, 'code', '') or type(exc).__name__)
            raise
        finally:
            with self.lock:
                self.ops.append((started, op, time.monotonic() - started, outcome))


def _quantiles(values: list[float]) -> dict:
    if not values:
        return {'n': 0}
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]
    return {'n': len(ordered), 'p50_ms': round(pick(.5) * 1000, 1), 'p90_ms': round(pick(.9) * 1000, 1),
            'p99_ms': round(pick(.99) * 1000, 1), 'max_ms': round(ordered[-1] * 1000, 1)}


class Instance:
    def __init__(self, args, root: Path):
        self.args = args
        self.root = root
        self.samples = Samples()
        self.events = EventLog()
        self.stop_driver = threading.Event()
        self.fault = root / 'fault'

    def start(self):
        logging.getLogger().addHandler(self.events)
        logging.getLogger().setLevel(logging.INFO)
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.storage.service import install_runtime_for_test
        supervisor = StorageSupervisor(project_root=self.root / 'project', backend='tofudb',
                                       startup_timeout=600.0)
        self.runtime = StorageRuntime(supervisor, auto_restart=True)
        install_runtime_for_test(self.runtime)
        self.runtime.start()
        return self.runtime.status()

    def stop(self):
        from lib.storage.service import install_runtime_for_test
        from serverctl_pkg.restart_budget import NATIVE_DAEMON_STOP_SECONDS
        self.release_all_faults()
        try:
            # The production host's drain budget: a shorter stop kills the
            # engine inside its closing checkpoint.
            self.runtime.stop(timeout=NATIVE_DAEMON_STOP_SECONDS)
        except Exception as exc:
            print(f'runtime stop failed: {type(exc).__name__}: {exc}', flush=True)
        install_runtime_for_test(None)

    def engine_pid(self) -> int | None:
        pid = self.runtime.status().get('pid')
        return pid if isinstance(pid, int) and pid > 0 else None

    # one scripted Turn -------------------------------------------------------
    def turn(self, conv_id: str, index: int) -> dict:
        """Create one Turn, then run claimed jobs until that Turn has settled.

        ``claim_next`` hands out the oldest queued job, which may be a Turn an
        earlier failure left queued; it is run to settlement too, so no lane
        stays busy and the measured latency includes any backlog ahead of it.
        """
        from lib.durable_worker import StorageWorkerJobStore
        from lib.identity import PrincipalContext
        from lib.turn_lifecycle import create_turn_pair, dispatch_attempt_to_worker
        timed = self.samples.timed
        policy = json.loads((PROJECT_ROOT / 'contracts/durable_worker_runtime_v1.json').read_text())
        store = StorageWorkerJobStore()
        principal = PrincipalContext.user(subject_id='load-resilience', owner_user_id=USER_ID,
                                          scopes={'conversation.execute'})
        started = time.monotonic()
        created = timed('turn.create_pair', create_turn_pair, conv_id, command_id=f'lr:{uuid.uuid4().hex}',
                        input_projection={'content': f'scripted turn {index}'},
                        config={'model': 'load-resilience-model'}, user_id=USER_ID,
                        conversation_defaults={'allowCreate': True, 'title': 'load resilience', 'settings': {}})
        attempt_id = created['attempt']['attemptId']
        timed('turn.attempt.dispatch_worker', dispatch_attempt_to_worker, principal, attempt_id)
        tool_seconds = 0.0
        while True:
            worker_id = f'load-resilience/{uuid.uuid4().hex[:8]}'
            document = timed('worker_job.claim_next', store.claim_next, worker_id=worker_id,
                             task_kinds=('conversation-attempt',), now_ms=int(time.time() * 1000),
                             lease_ms=policy['workerLeaseMilliseconds'])
            if not document:
                raise RuntimeError('claim_empty')
            tool_seconds += self.run_job(document, worker_id, store, principal, index)
            if document['payload']['attemptId'] == attempt_id:
                return {'seconds': time.monotonic() - started, 'tool_seconds': tool_seconds}

    def run_job(self, document, worker_id, store, principal, index) -> float:
        """Stream events, run one real tool call and settle; return tool seconds."""
        from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim, WorkerJobOutcome
        from lib.project_mod.run_command import _run_command_simple
        from lib.turn_lifecycle import mark_task_started, record_task_event
        timed = self.samples.timed
        claim = WorkerJobClaim.from_document(document, expected_worker_id=worker_id)
        job = document['payload']
        task_id = claim.task_id
        timed('turn.attempt.start', mark_task_started, job['attemptId'], task_id, user_id=USER_ID,
              worker_claim={'worker_id': worker_id, 'fencing_token': claim.fencing_token})
        task = {'_attemptId': job['attemptId'], 'id': task_id, 'status': 'running', '_userId': USER_ID,
                '_principalContext': principal.to_payload(), 'content': '', 'thinking': '',
                'toolRounds': [], 'apiRounds': [], 'model': 'load-resilience-model',
                'config': {'model': 'load-resilience-model'},
                '_conversationWorkerContext': WorkerExecutionContext(claim)}
        sequence = 0

        def event(wire):
            nonlocal sequence
            wire = dict(wire, seq=sequence)
            timed('turn.event.record', record_task_event, task, wire,
                  task_event={'task_id': task_id, 'sequence': sequence, 'event': wire})
            sequence += 1

        for chunk in ('Reading ', 'the ', 'project. '):
            task['content'] += chunk
            event({'type': 'delta', 'content': chunk})
        tool_started = time.monotonic()
        result = _run_command_simple(TOOL_COMMAND, TOOL_COMMAND, 120, str(self.root / 'project'),
                                     child_env=dict(os.environ, LD_PRELOAD=''))
        tool_seconds = time.monotonic() - tool_started
        task['toolRounds'].append({'id': f'call_{index}', 'name': 'run_command', 'status': 'done',
                                   'arguments': {'command': TOOL_COMMAND}, 'result': result[-2000:],
                                   'roundNum': 1})
        event({'type': 'round_end', 'roundNum': 1, 'reason': 'tool_calls'})
        for chunk in ('Done', '.'):
            task['content'] += chunk
            event({'type': 'delta', 'content': chunk})
        timed('turn.event.record:terminal', record_task_event, task, {'type': 'done', 'seq': sequence},
              task_event={'task_id': task_id, 'sequence': sequence, 'event': {'type': 'done', 'seq': sequence}})
        timed('worker_job.complete', store.complete, claim, now_ms=int(time.time() * 1000),
              outcome=WorkerJobOutcome(status='succeeded', replay_cursor=sequence))
        return tool_seconds

    def driver(self):
        conv_id = f'lr{uuid.uuid4().hex[:12]}'
        index = 0
        while not self.stop_driver.is_set():
            index += 1
            started = time.monotonic()
            try:
                outcome = self.turn(conv_id, index)
                record = {'start': started, 'ok': True, **outcome}
            except Exception as exc:
                record = {'start': started, 'ok': False, 'seconds': time.monotonic() - started,
                          'error': str(getattr(exc, 'code', '') or type(exc).__name__)}
                # A failed Turn may keep its lane until its claim expires; the
                # next Turn starts a fresh lane and settles whatever it claims.
                conv_id = f'lr{uuid.uuid4().hex[:12]}'
                self.stop_driver.wait(0.5)
            with self.samples.lock:
                self.samples.turns.append(record)

    # faults ------------------------------------------------------------------
    def _touch(self, name: str):
        (self.fault / name).touch()

    def _remove(self, name: str):
        try:
            (self.fault / name).unlink()
        except FileNotFoundError:
            pass

    def release_volume(self):
        self._remove('volume.trigger')
        fifo = self.fault / 'volume.fifo'
        if not fifo.exists():
            return
        descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            for _ in range(20):
                try:
                    os.write(descriptor, b'\0' * 4096)
                except BlockingIOError:
                    pass
                time.sleep(0.05)
        finally:
            os.close(descriptor)

    def release_all_faults(self):
        self._remove('socket.trigger')
        self.release_volume()
        self._touch('lease.trigger.release')
        self._remove('lease.trigger')
        for pid in getattr(self, 'stopped_pids', ()):
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def competitors(self, count: int, seconds: float) -> list[threading.Thread]:
        from lib.project_mod.run_command import _run_command_simple
        threads = []
        for index in range(count):
            thread = threading.Thread(
                target=_run_command_simple, name=f'lr-competitor-{index}', daemon=True,
                args=(COMPETITOR_COMMAND, COMPETITOR_COMMAND, seconds, str(self.root / 'project')),
                kwargs={'child_env': dict(os.environ, LD_PRELOAD='')})
            thread.start()
            threads.append(thread)
        return threads

    def inject(self, scenario: str, seconds: float) -> dict:
        """Hold one fault for ``seconds``; return what was injected."""
        detail = {'scenario': scenario}
        deadline = time.monotonic() + seconds
        if scenario == 'baseline':
            time.sleep(seconds)
        elif scenario == 'slow':
            self._touch('socket.trigger')
            time.sleep(seconds)
            self._remove('socket.trigger')
        elif scenario == 'volume':
            fifo = self.fault / 'volume.fifo'
            if not fifo.exists():
                os.mkfifo(fifo, 0o600)
            self._touch('volume.trigger')
            time.sleep(max(0.0, deadline - time.monotonic()))
            detail['engine_threads_at_release'] = self.thread_census()
            self.release_volume()
        elif scenario in ('sigstop', 'lease'):
            pid = self.engine_pid()
            detail['frozen_pid'] = pid
            if scenario == 'lease':
                # An earlier scenario's cleanup leaves a release marker behind.
                self._remove('lease.trigger.release')
                self._remove('lease.trigger.held')
                self._touch('lease.trigger')
                held = self.fault / 'lease.trigger.held'
                wait_until = time.monotonic() + 30
                while not held.exists() and time.monotonic() < wait_until:
                    time.sleep(0.1)
                detail['lease_holder'] = held.exists()
            if pid:
                self.stopped_pids = [pid]
                os.kill(pid, signal.SIGSTOP)
            time.sleep(max(0.0, deadline - time.monotonic()))
            if pid:
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            if scenario == 'lease':
                self._touch('lease.trigger.release')
                self._remove('lease.trigger')
        elif scenario == 'cpu':
            threads = self.competitors(self.args.competitors, seconds)
            time.sleep(1.0)
            detail['competitor_priorities'] = self.competitor_priorities()
            time.sleep(max(0.0, deadline - time.monotonic()))
            for thread in threads:
                thread.join(timeout=30)
        return detail

    def competitor_priorities(self) -> list[dict]:
        import psutil
        found = []
        for process in psutil.Process().children(recursive=True):
            try:
                if 'while True' in ' '.join(process.cmdline()):
                    oom = Path(f'/proc/{process.pid}/oom_score_adj').read_text().strip()
                    found.append({'nice': process.nice(), 'ioclass': int(process.ionice().ioclass),
                                  'oom_score_adj': int(oom)})
            except (psutil.Error, OSError):
                continue
        return found

    def thread_census(self) -> dict:
        pid = self.engine_pid()
        census: dict[str, int] = {}
        if not pid:
            return census
        try:
            tids = os.listdir(f'/proc/{pid}/task')
        except OSError:
            return census
        for tid in tids:
            try:
                state = Path(f'/proc/{pid}/task/{tid}/stat').read_text().rsplit(')', 1)[1].split()[0]
                call = Path(f'/proc/{pid}/task/{tid}/syscall').read_text().split()[0]
            except (OSError, IndexError):
                continue
            key = f'{state}:{call}'
            census[key] = census.get(key, 0) + 1
        return dict(sorted(census.items(), key=lambda item: -item[1])[:8])

    # phases ------------------------------------------------------------------
    def phase_report(self, start: float, end: float) -> dict:
        with self.samples.lock:
            turns = [t for t in self.samples.turns if start <= t['start'] < end]
            ops = [o for o in self.samples.ops if start <= o[0] < end]
        ok = [t for t in turns if t['ok']]
        operations = {}
        for name in sorted({o[1] for o in ops}):
            values = [o[2] for o in ops if o[1] == name and o[3] == 'ok']
            errors: dict[str, int] = {}
            for o in ops:
                if o[1] == name and o[3] != 'ok':
                    errors[o[3]] = errors.get(o[3], 0) + 1
            operations[name] = {**_quantiles(values), 'errors': errors}
        failures: dict[str, int] = {}
        for t in turns:
            if not t['ok']:
                failures[t['error']] = failures.get(t['error'], 0) + 1
        return {
            'seconds': round(end - start, 1),
            'turns_ok': len(ok), 'turns_failed': len(turns) - len(ok), 'turn_failures': failures,
            'turn': _quantiles([t['seconds'] for t in ok]),
            'turn_control_plane': _quantiles([t['seconds'] - t['tool_seconds'] for t in ok]),
            'tool': _quantiles([t['tool_seconds'] for t in ok]),
            'longest_gap_between_completed_turns_s': _longest_gap(ok, start, end),
            'operations': operations,
            'events': self.events.count(start, end),
        }

    def run_scenario(self, scenario: str) -> dict:
        self.stop_driver.clear()
        driver = threading.Thread(target=self.driver, name='lr-driver', daemon=True)
        load_before = os.getloadavg()[0]
        pid_before = self.engine_pid()
        started = time.monotonic()
        driver.start()
        time.sleep(self.args.warmup_seconds)
        fault_started = time.monotonic()
        detail = self.inject(scenario, self.args.fault_seconds)
        fault_ended = time.monotonic()
        time.sleep(self.args.recovery_seconds)
        ended = time.monotonic()
        self.stop_driver.set()
        driver.join(timeout=300)
        status = self.runtime.status()
        return {
            'scenario': scenario, 'detail': detail,
            'host_loadavg1': [round(load_before, 1), round(os.getloadavg()[0], 1)],
            'engine_pid': [pid_before, self.engine_pid()],
            'final_storage_state': status.get('state'), 'final_ready': status.get('ready'),
            'phases': {
                'warmup': self.phase_report(started, fault_started),
                'fault': self.phase_report(fault_started, fault_ended),
                'recovery': self.phase_report(fault_ended, ended),
            },
            'storage_log': self.events.lines(fault_started, ended),
        }

    def settle_between_scenarios(self) -> bool:
        """Wait (bounded by the recovery window) until storage is ready again."""
        until = time.monotonic() + max(60.0, self.args.recovery_seconds)
        while time.monotonic() < until:
            if self.runtime.status().get('ready'):
                return True
            time.sleep(1.0)
        return bool(self.runtime.status().get('ready'))


def _longest_gap(turns: list[dict], start: float, end: float) -> float:
    marks = sorted([start] + [t['start'] + t['seconds'] for t in turns] + [end])
    return round(max(b - a for a, b in zip(marks, marks[1:])), 1)


def _lower_self(nice: int, island: list[int]) -> None:
    # Every later thread and child (engine, tool calls, competitors) inherits both.
    os.sched_setaffinity(0, island)
    os.setpriority(os.PRIO_PROCESS, 0, nice)


def _markdown(report: dict) -> str:
    lines = [f"# Load resilience run {report['run_id']} ({report.get('label') or 'unlabelled'})", '',
             f"git {report['git']} · nice {report['nice']} · CPU island {report['cpus']} · engine "
             f"{report['engine_sha256'][:12]}", '']
    for run in report['scenarios']:
        lines += [f"## {run['scenario']}", '',
                  f"host loadavg1 {run['host_loadavg1']} · engine pid {run['engine_pid']} · final "
                  f"{run['final_storage_state']} ready={run['final_ready']} · {json.dumps(run['detail'])[:300]}", '',
                  '| phase | turns ok/failed | turn p50/p90/max ms | control-plane p50/max ms | tool p50/max ms '
                  '| longest gap s | probe failed | slow probes | withheld | recycles | restarts ok/failed | breaker |',
                  '|---|---|---|---|---|---|---|---|---|---|---|---|']
        for name, phase in run['phases'].items():
            turn, control, tool, events = (phase['turn'], phase['turn_control_plane'], phase['tool'],
                                           phase['events'])
            lines.append(
                f"| {name} | {phase['turns_ok']}/{phase['turns_failed']} | {turn.get('p50_ms', '-')}/"
                f"{turn.get('p90_ms', '-')}/{turn.get('max_ms', '-')} | {control.get('p50_ms', '-')}/"
                f"{control.get('max_ms', '-')} | {tool.get('p50_ms', '-')}/{tool.get('max_ms', '-')} | "
                f"{phase['longest_gap_between_completed_turns_s']} | {events['probe_failed']} | "
                f"{events['probe_slow']} | {events['withheld']} | {events['recycle']} | "
                f"{events['restart_succeeded']}/{events['restart_failed']} | {events['breaker']} |")
        record = run['phases']['fault']['operations'].get('turn.event.record', {})
        lines += ['', f"fault-phase turn.event.record: {json.dumps(record)}", '']
    return '\n'.join(lines) + '\n'


def child_main(args) -> int:
    root = Path(args.root).resolve()
    island = [int(cpu) for cpu in args.cpus.split(',')]
    _lower_self(args.nice, island)
    instance = Instance(args, root)
    git = subprocess.run(['git', '-C', str(PROJECT_ROOT), 'rev-parse', '--short', 'HEAD'],
                         capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(PROJECT_ROOT), '--no-optional-locks', 'status', '--porcelain',
                            '--untracked-files=no'], capture_output=True, text=True).stdout.strip()
    report = {'run_id': args.run_id, 'label': args.label, 'git': git + ('+dirty' if dirty else ''),
              'nice': args.nice, 'cpus': island, 'engine_sha256': os.environ['TOFUDB_EXPECTED_SHA256'],
              'socket_delay_ms': args.socket_delay_ms, 'competitors': args.competitors, 'scenarios': []}
    out = root / 'results' / args.run_id
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = instance.start()
    report['storage_status'] = {key: status.get(key) for key in ('backend', 'pid', 'port', 'state')}
    print(f"storage ready: {report['storage_status']}", flush=True)
    try:
        for scenario in args.scenarios.split(','):
            if not instance.settle_between_scenarios():
                report['scenarios'].append({'scenario': scenario, 'skipped': 'storage not ready'})
                continue
            run = instance.run_scenario(scenario)
            instance.release_all_faults()
            report['scenarios'].append(run)
            fault = run['phases']['fault']
            print(f"{scenario}: fault turns {fault['turns_ok']}/{fault['turns_failed']} "
                  f"turn p50 {fault['turn'].get('p50_ms')} ms, events {fault['events']}, "
                  f"final {run['final_storage_state']}", flush=True)
            (out / 'report.json').write_text(json.dumps(report, indent=1))
            (out / 'report.md').write_text(_markdown(report))
    finally:
        instance.stop()
    print(f'report: {out / "report.md"}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
