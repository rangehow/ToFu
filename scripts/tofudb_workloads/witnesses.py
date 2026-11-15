"""Charter witnesses: joined measurements that one ordinary phase cannot provide.

Entry: CharterWitnesses.run(name) for each witness named in
contracts/storage_performance_charter_v1.json. Every witness measures the run's
own pinned artifact on the run's own volume through explicit ports:
WitnessPorts.fresh_engine starts a disposable authority beside the measured
ones, describe names its transport, artifact and volume, process_ids names its
separate authority processes, and HostControls owns CPU affinity, stressor
processes and /proc observations. Engines own SIGKILL, reopen and second-
authority admission. Acceptance stays in scripts/tofudb_charter_assess.py.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable
import uuid

from scripts.tofudb_workloads.catalog import build_workloads
from scripts.tofudb_workloads.history import HistoryWorkloads
from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts
from scripts.tofudb_workloads.records import RecordWorkloads
from scripts.tofudb_workloads.workers import WorkloadWorkers

FORMAT = 'tofu.storage-charter-witnesses/v1'
# Mirrored blocks cancel linear drift between the two cohorts.
COHORT_ORDER = ('idle', 'pressure', 'pressure', 'idle')
_STRESSOR = (
    'import sys\n'
    'half = int(sys.argv[1]) // 2\n'
    'a = bytearray(half)\n'
    'b = bytearray(b"\\x5a") * half\n'
    'while True:\n'
    '    a[:] = b\n'
    '    b[:] = a\n'
)


def _cpu_list(text: str) -> set[int]:
    cpus = set()
    for part in text.strip().split(','):
        if '-' in part:
            low, high = part.split('-')
            cpus.update(range(int(low), int(high) + 1))
        elif part:
            cpus.add(int(part))
    return cpus


def _cache_bytes(text: str) -> int:
    units = {'K': 1 << 10, 'M': 1 << 20, 'G': 1 << 30}
    text = text.strip()
    if text[-1:] in units:
        return int(text[:-1]) * units[text[-1]]
    return int(text)


class HostControls:
    """Linux affinity, harness-owned stressors and /proc observations."""

    def allowed_cpus(self) -> list[int]:
        return sorted(os.sched_getaffinity(0))

    @staticmethod
    def _tasks(pid: int) -> list[int]:
        try:
            return [int(name) for name in os.listdir(f'/proc/{pid}/task')]
        except OSError:
            return []

    def affinities(self, pids) -> dict[int, list[int]]:
        observed = {}
        for pid in pids:
            for task in self._tasks(pid):
                try:
                    observed[task] = sorted(os.sched_getaffinity(task))
                except OSError:
                    continue  # An exited thread has no affinity left to report.
        return observed

    def confine(self, pids, cpus) -> None:
        for pid in pids:
            for task in self._tasks(pid):
                try:
                    os.sched_setaffinity(task, cpus)
                except ProcessLookupError:
                    continue

    def restore(self, affinities: dict[int, list[int]], default) -> None:
        for task in self._tasks(os.getpid()):
            try:
                os.sched_setaffinity(task, affinities.get(task, default))
            except ProcessLookupError:
                continue

    def siblings(self, cpus) -> list[int]:
        found = set()
        for cpu in cpus:
            try:
                found |= _cpu_list(Path(
                    f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list').read_text())
            except (OSError, ValueError):
                continue
        return sorted(found - set(cpus))

    def last_level_caches(self, cpus) -> dict[str, int]:
        """Distinct last-level data caches serving the CPU set, keyed by sharing CPUs."""
        caches = {}
        for cpu in cpus:
            best = None
            for index in Path(f'/sys/devices/system/cpu/cpu{cpu}/cache').glob('index*'):
                try:
                    kind = (index / 'type').read_text().strip()
                    level = int((index / 'level').read_text())
                    size = _cache_bytes((index / 'size').read_text())
                    shared = (index / 'shared_cpu_list').read_text().strip()
                except (OSError, ValueError):
                    continue
                if kind != 'Instruction' and (best is None or level > best[0]):
                    best = (level, shared, size)
            if best is None:
                return {}
            caches[best[1]] = best[2]
        return caches

    def start_stressors(self, cpus, total_bytes: int) -> list[subprocess.Popen]:
        """One CPU-and-memory stressor per CPU, confined to the set; the harness owns every byte."""
        processes = []
        try:
            for _ in cpus:
                process = subprocess.Popen(
                    [sys.executable, '-c', _STRESSOR, str(total_bytes // len(cpus))],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                processes.append(process)
                os.sched_setaffinity(process.pid, cpus)
        except BaseException:
            self.stop_stressors(processes)
            raise
        return processes

    def stop_stressors(self, processes) -> None:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait()

    def process_cpu_seconds(self, pid: int) -> float | None:
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
        except (OSError, IndexError, ValueError):
            return None

    def cpu_times(self, cpus) -> dict[int, tuple[int, int]]:
        """(busy, total) jiffies per CPU; guest time is already inside user time."""
        wanted = {f'cpu{cpu}': cpu for cpu in cpus}
        result = {}
        for line in Path('/proc/stat').read_text().splitlines():
            name, *values = line.split()
            if name in wanted:
                numbers = [int(value) for value in values[:8]]
                total = sum(numbers)
                result[wanted[name]] = (total - numbers[3] - numbers[4], total)
        return result


@dataclass(frozen=True)
class WitnessPorts:
    fresh_engine: Callable[..., object]
    describe: Callable[[object], dict]
    process_ids: Callable[[object], list[int]]
    measure: Callable[..., dict]
    minimum_tail_samples: int
    host: HostControls = field(default_factory=HostControls)


@dataclass(frozen=True)
class CharterWitnesses:
    inputs: WorkloadInputs
    ports: WorkloadPorts
    witness: WitnessPorts
    engine_order: tuple[str, ...]
    specs: dict

    def run(self, name: str) -> dict:
        runners = {
            'history_sweep': self.history_sweep,
            'volume_recovery': self.volume_recovery,
            'distracted_cohorts': self.distracted_cohorts,
            'horizontal_scaling': self.horizontal_scaling,
        }
        return {'format': FORMAT, 'engine_order': list(self.engine_order),
                **runners[name](self.specs[name])}

    def _fresh(self, **scale) -> WorkloadInputs:
        return replace(self.inputs, namespace=f'witness_{uuid.uuid4().hex[:8]}',
                       scale={**self.inputs.scale, **scale})

    def _phases(self, inputs: WorkloadInputs) -> dict[str, tuple]:
        workloads = build_workloads(inputs, self.ports, search_word=f'wq{uuid.uuid4().hex[:10]}')
        return {entry[0]: entry for entry in workloads}

    @contextmanager
    def _started(self, name: str, **options):
        engine = self.witness.fresh_engine(name, **options)
        try:
            yield engine
        finally:
            engine.stop()

    def _summary(self, samples: list[float]) -> dict:
        summary = self.ports.summarize(samples)
        summary.pop('samples_ms', None)
        summary.pop('sample_order', None)
        return summary

    def _each_engine(self, work) -> dict:
        """One engine's failure is its own observation, never the loss of the other's evidence."""
        engines = {}
        for name in self.engine_order:
            try:
                engines[name] = work(name)
            except Exception as exc:  # noqa: BLE001 - recorded for the assessor
                engines[name] = {'error': f'{type(exc).__name__}: {exc}'[:500]}
        return engines

    # ── history_read: matched corpus sweep ────────────────────────────────
    def history_sweep(self, spec) -> dict:
        """Grow each phase's own corpus through every length; engines alternate at each length."""
        history = HistoryWorkloads(self._fresh(), self.ports)
        sweeps = history.history_sweep_phases()
        multipliers = spec['length_multipliers']
        lengths = {
            workload: [self.inputs.scale[sweeps[workload][0]] * multiplier for multiplier in multipliers]
            for workload in spec['workloads']}
        engines = {name: {'authorities': [], 'points': []} for name in self.engine_order}
        failed: dict[str, str] = {}
        with ExitStack() as stack:
            started = {}
            for name in self.engine_order:
                try:
                    started[name] = engine = stack.enter_context(self._started(name))
                    engines[name]['authorities'].append(self.witness.describe(engine))
                    for workload in spec['workloads']:
                        sweeps[workload][1](engine)
                except Exception as exc:  # noqa: BLE001 - that engine's own observation
                    failed[name] = f'{type(exc).__name__}: {exc}'[:500]
            for index in range(len(multipliers)):
                for name, engine in started.items():
                    if name in failed:
                        continue  # A failed leg stops; the other engine's sweep continues.
                    try:
                        for workload in spec['workloads']:
                            start = lengths[workload][index - 1] if index else 0
                            sweeps[workload][2](engine, start, lengths[workload][index])
                        measured = {
                            workload: self.witness.measure(engine, None, sweeps[workload][3],
                                                           self.inputs.scale['reads'])
                            for workload in spec['workloads']}
                    except Exception as exc:  # noqa: BLE001 - that engine's own observation
                        failed[name] = f'{type(exc).__name__}: {exc}'[:500]
                        continue
                    engines[name]['points'].append({'length_index': index, 'workloads': measured})
        engines.update({name: {'error': error} for name, error in failed.items()})
        return {
            'workloads': list(spec['workloads']), 'lengths': lengths,
            'length_parameters': {workload: sweeps[workload][0] for workload in spec['workloads']},
            'corpus': ('one disposable authority per engine whose phase corpora grow through every '
                       'length; each length holds exactly the corpus the phase seeds at that length; '
                       'engines alternate at each length'),
            'engines': engines,
        }

    # ── degraded_fuse: actual-volume lock and process-crash recovery ─────
    def volume_recovery(self, spec) -> dict:
        """Second-authority refusal, then SIGKILL mid-write and exact acknowledged reopen."""
        def recover(name):
            with self._started(name, daemon=True) as engine:
                entry = {'authorities': [self.witness.describe(engine)],
                         'second_authority': engine.second_authority_attempt()}
                entry['rounds'] = self._crash_rounds(engine)
                return entry
        return {
            'write': 'record.put of unique keys; every value read back exactly after reopen',
            'rounds': self.inputs.scale['maintenance_rounds'],
            'acknowledged_before_kill': self.inputs.scale['crash_appends'],
            'engines': self._each_engine(recover),
        }

    def _crash_rounds(self, engine) -> list[dict]:
        inputs = self._fresh()
        acknowledged: dict[str, int] = {}
        rounds = []
        next_index = 0
        for _ in range(self.inputs.scale['maintenance_rounds']):
            result, next_index = self._crash_round(engine, inputs, acknowledged, next_index)
            rounds.append(result)
            if not result.get('reopened'):
                break  # Nothing further can be observed on an authority that did not reopen.
        return rounds

    def _put(self, inputs: WorkloadInputs, index: int):
        return ('record.put', {'namespace': inputs.namespace, 'key': f'crash/{index:06d}',
                               'value': self.ports.record_value(index)}, True)

    def _record_state(self, engine, inputs: WorkloadInputs, index: int) -> str:
        value = self.ports.execute(engine, 'record.get', {
            'namespace': inputs.namespace, 'key': f'crash/{index:06d}'}, False)
        if value is None:
            return 'absent'
        expected = json.dumps(self.ports.record_value(index), sort_keys=True)
        if isinstance(value, dict) and json.dumps(value.get('value'), sort_keys=True) == expected:
            return 'present'
        return 'changed'

    def _crash_round(self, engine, inputs, acknowledged, first_index):
        """Kill while a write is outstanding: the writer marks it under the same lock."""
        appends = self.inputs.scale['crash_appends']
        state = threading.Condition()
        progress = {'acked': 0, 'inflight': None, 'error': None, 'failed': None}

        def writer():
            index = first_index
            while True:
                operation, payload, command = self._put(inputs, index)
                with state:
                    progress['inflight'] = index
                    state.notify_all()
                try:
                    self.ports.execute(engine, operation, payload, command)
                except Exception as exc:  # noqa: BLE001 - the SIGKILL ends this writer
                    with state:
                        progress.update(error=f'{type(exc).__name__}: {exc}'[:240],
                                        failed=index, inflight=None)
                        state.notify_all()
                    return
                with state:
                    acknowledged[payload['key']] = index
                    progress['acked'] += 1
                    progress['inflight'] = None
                    state.notify_all()
                index += 1

        thread = threading.Thread(target=writer, name='perf-crash-writer', daemon=True)
        thread.start()
        with state:
            landed = state.wait_for(lambda: progress['error'] is not None or (
                progress['acked'] >= appends and progress['inflight'] is not None),
                timeout=120.0 + 0.1 * appends)
            if progress['error'] is not None or not landed:
                raise self.ports.error(
                    f'crash writer stopped before SIGKILL: {progress["error"] or "no progress"}')
            outstanding = progress['inflight']
            acknowledged_at_kill = len(acknowledged)
            killed_pid = engine.sigkill_authority()
        thread.join(timeout=120.0)
        if thread.is_alive():
            raise self.ports.error('crash writer did not observe the authority loss')
        result = {
            'signal': 'SIGKILL', 'killed_pid': killed_pid, 'harness_pid': os.getpid(),
            'separate_process': type(killed_pid) is int and killed_pid != os.getpid(),
            'acknowledged_before_kill': acknowledged_at_kill,
            'unacknowledged_at_kill': outstanding is not None,
            'outstanding_acknowledged_after_kill': f'crash/{outstanding:06d}' in acknowledged,
            'writer_error': progress['error'],
        }
        next_index = progress['failed'] + 1
        try:
            result['reopen_ms'] = round(engine.reopen_after_sigkill(), 3)
        except Exception as exc:  # noqa: BLE001 - a failed recovery is the observation
            result.update(reopened=False, reopen_error=f'{type(exc).__name__}: {exc}'[:500])
            return result, next_index
        result['reopened'] = True
        try:
            lost = [key for key, index in acknowledged.items()
                    if self._record_state(engine, inputs, index) != 'present']
            outcome = self._record_state(engine, inputs, progress['failed'])
        except Exception as exc:  # noqa: BLE001 - unreadable recovered state is the observation
            result['verification_error'] = f'{type(exc).__name__}: {exc}'[:500]
            return result, next_index
        result.update(acknowledged_total=len(acknowledged), acknowledged_lost=len(lost),
                      lost_keys=lost[:16],
                      unacknowledged_outcomes={name: int(outcome == name)
                                               for name in ('present', 'absent', 'changed')})
        operation, payload, command = self._put(inputs, next_index)
        try:
            self.ports.execute(engine, operation, payload, command)
            result['writable_after_recovery'] = (
                self._record_state(engine, inputs, next_index) == 'present')
        except Exception as exc:  # noqa: BLE001
            result.update(writable_after_recovery=False,
                          write_error=f'{type(exc).__name__}: {exc}'[:500])
            return result, next_index + 1
        acknowledged[payload['key']] = next_index
        return result, next_index + 1

    # ── distracted_hardware: matched idle-versus-pressure cohorts ────────
    def distracted_cohorts(self, spec) -> dict:
        """Benchmark and stressors share one small CPU set; blocks run idle/pressure/pressure/idle."""
        host = self.witness.host
        allowed = host.allowed_cpus()
        cpus = allowed[-spec['cpus']:]
        caches = host.last_level_caches(cpus)
        pressure_bytes = sum(caches.values())
        siblings = host.siblings(cpus)
        per_block = -(-self.witness.minimum_tail_samples // COHORT_ORDER.count('idle'))
        original = host.affinities([os.getpid()])

        def cohorts(name):
            with self._started(name) as engine:
                return self._cohorts(engine, spec, cpus, siblings, pressure_bytes, per_block)
        try:
            host.confine([os.getpid()], cpus)
            engines = self._each_engine(cohorts)
        finally:
            host.restore(original, allowed)
        return {
            'cpus': cpus, 'allowed_cpus': allowed, 'smt_siblings': siblings,
            'memory_pressure': {
                'bytes': pressure_bytes, 'last_level_caches': caches,
                'scope': ('stressors stream harness-owned buffers totalling the last-level cache '
                          'of the CPU set: cache and memory-bandwidth contention on the same '
                          'cores; RAM capacity of the shared host is not reduced'),
            },
            'cohort_order': list(COHORT_ORDER), 'iterations_per_block': per_block,
            'workloads': list(spec['workloads']), 'engines': engines,
        }

    def _cohorts(self, engine, spec, cpus, siblings, pressure_bytes, per_block) -> dict:
        host = self.witness.host
        pids = [os.getpid(), *self.witness.process_ids(engine)]
        blocks = []
        for condition in COHORT_ORDER:
            phases = self._phases(self._fresh())
            stressors = host.start_stressors(cpus, pressure_bytes) if condition == 'pressure' else []
            try:
                host.confine(pids, cpus)
                stress_before = [host.process_cpu_seconds(process.pid) for process in stressors]
                busy_before = host.cpu_times([*cpus, *siblings])
                measured = {}
                for workload in spec['workloads']:
                    _, setup, operation = phases[workload][:3]
                    measured[workload] = self.witness.measure(engine, setup, operation, per_block)
                busy_after = host.cpu_times([*cpus, *siblings])
                benchmark = host.affinities(pids)
                rows = []
                for process, before in zip(stressors, stress_before):
                    after = host.process_cpu_seconds(process.pid)
                    rows.append({
                        'pid': process.pid, 'alive_at_end': process.poll() is None,
                        'affinity': host.affinities([process.pid]).get(process.pid),
                        'cpu_seconds': (round(after - before, 3)
                                        if after is not None and before is not None else None),
                    })
            finally:
                host.stop_stressors(stressors)
            busy = {}
            for cpu, (used, total) in busy_after.items():
                start = busy_before.get(cpu)
                if start is not None and total > start[1]:
                    busy[str(cpu)] = round((used - start[0]) / (total - start[1]), 4)
            blocks.append({
                'condition': condition, 'workloads': measured,
                'benchmark_tasks': len(benchmark),
                'benchmark_confined': bool(benchmark) and all(
                    set(affinity) <= set(cpus) for affinity in benchmark.values()),
                'stressors': rows, 'cpu_busy_fraction': busy,
            })
        cohorts = {
            workload: {
                condition: self._summary([
                    sample for block in blocks if block['condition'] == condition
                    for sample in block['workloads'][workload]['samples_ms']])
                for condition in ('idle', 'pressure')}
            for workload in spec['workloads']}
        return {'authorities': [self.witness.describe(engine)], 'process_count': len(pids),
                'blocks': blocks, 'cohorts': cohorts}

    # ── horizontal_reads: writer-only versus writer plus N readers ───────
    def horizontal_scaling(self, spec) -> dict:
        """Mirrored reader-count blocks on one authority; each N pools its two blocks."""
        sweep = list(spec['readers'])
        order = sweep + sweep[::-1]
        writes = self.inputs.scale['concurrent_writes']

        def scaling(name):
            records = RecordWorkloads(self._fresh(), self.ports)
            with self._started(name) as engine:
                entry = {'authorities': [self.witness.describe(engine)]}
                records.record_put_setup(engine)
                records.conv_seed_setup('hz')(engine)
                blocks = [{'readers': readers,
                           **self._writer_block(engine, records, readers, writes, index)}
                          for index, readers in enumerate(order)]
            pooled = {}
            for readers in sweep:
                matching = [block for block in blocks if block['readers'] == readers]
                completed = sum(block['writes'] for block in matching)
                seconds = sum(block['writer_seconds'] for block in matching)
                reader_samples = [sample for block in matching for sample in block.pop('reader_samples')]
                pooled[str(readers)] = {
                    'writes': completed, 'writer_seconds': round(seconds, 6),
                    'writer_ops_per_sec': round(completed / seconds, 3) if seconds else 0.0,
                    'writer': self._summary([sample for block in matching
                                             for sample in block['writer']['samples_ms']]),
                    'reads_completed': len(reader_samples),
                    'reads_overlapping_writer': sum(block['reads_overlapping_writer'] for block in matching),
                    'reader': self._summary(reader_samples) if reader_samples else None,
                }
            return {**entry, 'blocks': blocks, 'readers': pooled}
        engines = self._each_engine(scaling)
        return {
            'readers': sweep, 'block_order': order, 'writes_per_block': writes,
            'writer_operation': 'record.put',
            'reader_operations': ['record.get', 'conversation.get', 'conversation.list'],
            'engines': engines,
        }

    def _writer_block(self, engine, records: RecordWorkloads, readers: int, writes: int,
                      block: int) -> dict:
        """Readers are already reading when the writer's clock starts and stop after it ends."""
        errors: list[str] = []
        workers = WorkloadWorkers(errors)
        writer_done = threading.Event()
        checked_in = threading.Condition()
        arrivals = [0]
        writer_samples: list[float] = []
        reads: list[list[tuple[int, float]]] = [[] for _ in range(readers)]
        window: dict[str, int] = {}
        inputs = records.inputs

        def check_in() -> None:
            with checked_in:
                arrivals[0] += 1
                checked_in.notify_all()

        def writer() -> None:
            def run(session):
                # Every reader checks in after its first read or its failure.
                with checked_in:
                    checked_in.wait_for(lambda: arrivals[0] >= readers)
                window['began'] = time.perf_counter_ns()
                for i in range(writes):
                    if workers.cancelled.is_set():
                        return
                    started = time.perf_counter_ns()
                    self.ports.execute(session, 'record.put', {
                        'namespace': inputs.namespace, 'key': f'hz/{block:02d}/{i:06d}',
                        'value': self.ports.record_value(i)}, True)
                    writer_samples.append((time.perf_counter_ns() - started) / 1e6)
                window['ended'] = time.perf_counter_ns()
            try:
                workers.run_worker(engine, 'writer', run)
            finally:
                writer_done.set()

        def reader(index: int) -> None:
            arrived = threading.Event()

            def run(session):
                i = 0
                while not writer_done.is_set() and not workers.cancelled.is_set():
                    pick = (index + i) % 3
                    if pick == 0:
                        operation, payload, command = records.record_get_op(i)
                    elif pick == 1:
                        operation, payload, command = ('conversation.get', {
                            'conv_id': inputs.conversation_id('hz', i % inputs.scale['seed_convs']),
                            'user_id': inputs.user}, False)
                    else:
                        operation, payload, command = records.conv_list_op(i)
                    started = time.perf_counter_ns()
                    self.ports.execute(session, operation, payload, command)
                    reads[index].append((started, (time.perf_counter_ns() - started) / 1e6))
                    if not arrived.is_set():
                        arrived.set()
                        check_in()
                    i += 1
            try:
                workers.run_worker(engine, f'reader {index}', run)
            finally:
                if not arrived.is_set():
                    check_in()

        for index in range(readers):
            workers.add(f'perf-horizontal-reader-{index}', reader, index)
        # Started last: a reader that never started implies the writer never started.
        workers.add('perf-horizontal-writer', writer)
        workers.run(120.0 + 0.1 * writes)
        if errors:
            raise self.ports.error('; '.join(errors))
        if len(writer_samples) != writes or 'ended' not in window:
            raise self.ports.error(f'writer completed {len(writer_samples)}/{writes} writes')
        seconds = (window['ended'] - window['began']) / 1e9
        flat = [read for per_reader in reads for read in per_reader]
        return {
            'writes': writes, 'writer_seconds': round(seconds, 6),
            'writer_ops_per_sec': round(writes / seconds, 3),
            'writer': self.ports.summarize(writer_samples),
            'reads_completed': len(flat),
            'reads_overlapping_writer': sum(
                1 for started, _ in flat if window['began'] <= started < window['ended']),
            'reader_samples': [latency for _, latency in flat],
        }
