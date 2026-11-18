#!/usr/bin/env python3
"""Harness overhead benchmark: Tofu's two processes against a zero-latency model.

Responsibility: measure what the harness itself costs per model round, with
the model removed. The deterministic stub provider (``stub_provider.py``)
answers every request instantly from a script, so every millisecond between a
finished model response and the next model request is harness time: request
assembly, context/contract build, tool dispatch and execution, result
normalization, persistence and event fan-out.

One command (``python3 benchmarks/harness_perf/run.py [--smoke]``) boots, in a
private root and against an isolated storage authority (never the live store):
    storage  -- the native TofuDB engine (pinned copy of $TOFUDB_BIN) owned by
                ``lib.storage.StorageSupervisor`` in the driver;
    execution host -- ``serverctl_pkg.execution_host`` via the production
                ``ExecutionService`` owner;
    API host -- ``server.py`` with ``TOFU_PROCESS_ROLE=api`` on a free port,
                attached to the same authority, as ``server_manager`` launches it;
    stub provider -- a separate process; the owner's only model route points at it.
and drives Turns through the public HTTP API (``POST /api/v4/conversations/
<id>/turns``) as the browser does. Metrics (each with n, median, p95):
    cold_start          storage, execution host and API host launch -> ready
    ttfr                POST of the user message -> first provider request
    round_overhead      provider response finished -> next provider request,
                        per scenario: ``noop`` (todo_write), ``shell``
                        (run_command ``true``); this is the tool round trip
    settle              last provider response finished -> attempt terminal
    request size        body bytes, prompt chars and ~tokens per round
    prefix stability    common prefix with the previous request of the same
                        session over the previous request's length (1.0 means
                        append-only, i.e. a full prompt-cache hit)
    resources           RSS and CPU of API host, execution host and engine at
                        idle and under ``--sessions`` concurrent sessions
    profile             py-spy (raw folded + flamegraph SVG) of both hosts
                        during the concurrent phase, attributed to stages
The reference harnesses (codex, claude-code, opencode, pi) run the same
scripted scenarios against the same stub (``references.py``) when
``--references`` is given.

Outputs: ``<out>/<run-id>/report.json``, ``report.md``, ``*.folded``, ``*.svg``.
Default ``<out>`` is ``output/harness_perf`` in the checkout.

Safety: the private root is 0700 and refused inside a live data directory
(``scripts/tofudb_load_test.py`` helpers); every child gets a scrubbed
environment whose HOME/TMPDIR/data live under the root; the run stops every
process it started. Nothing of the running Tofu is touched or restarted.

Dependencies: scripts/tofudb_load_test.py, scripts/agent_loop_load_test.py
(storage, model route and execution-host bring-up, reused by import),
serverctl_pkg.execution_service, lib.turn_lifecycle; py-spy (optional).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
sys.path.insert(0, str(HERE))
import tofudb_load_test as storage_harness  # noqa: E402

USER_ID = 1
TERMINAL = frozenset({'completed', 'truncated', 'failed', 'interrupted', 'cancelled'})

# Run shapes. Repetition counts and session counts are measurement design, not
# product settings; smoke keeps the whole run inside the CI budget (2 min).
SHAPES = {
    'smoke': {'chat_turns': 3, 'tool_turns': 1, 'true_turns': 3, 'rounds': 8, 'sessions': 4,
              'concurrent_turns': 1, 'idle_s': 3.0, 'references': ('chat', 'noop', 'shell', 'true')},
    'full': {'chat_turns': 10, 'tool_turns': 3, 'true_turns': 10, 'rounds': 20, 'sessions': 20,
             'concurrent_turns': 2, 'idle_s': 15.0, 'references': ('chat', 'noop', 'shell', 'true')},
}

# Stage attribution of profiler samples: the first owner whose frame occurs
# anywhere in the stack takes the sample (most specific first).
STAGES = (
    ('request assembly (llm request/build_body/protocol adapt)',
     ('lib/llm/request', 'lib/llm_dispatch/', 'build_body', 'build_round_request', 'lib/model_routing/')),
    ('context/contract build (composer, compaction, tokens)',
     ('lib/tasks_pkg/context_composer/', 'lib/tasks_pkg/compaction/', 'lib/token_counter/', 'prompt_admission',
      'lib/tasks_pkg/context_')),
    ('provider stream ingest (SSE parse, accumulate)', ('lib/llm/',)),
    ('tool dispatch & execution', ('lib/tools/', 'tool_dispatch', 'lib/project_mod/', 'lib/tasks_pkg/handlers')),
    ('result normalization', ('lib/tool_results/', 'tool_result')),
    ('persistence (storage client, turn lifecycle, events)',
     ('lib/storage/', 'lib/turn_lifecycle', 'lib/storage_authority_sql/', 'event_log', 'lib/durable_worker/')),
    ('event fan-out (sync, delivery, push, SSE)',
     ('lib/conversation_sync/', 'delivery', 'lib/push', 'lib/http_transport/', 'sse')),
    ('task runtime / agent loop', ('lib/tasks_pkg/', 'lib/agent_core/', 'lib/agent_loop/')),
    ('HTTP serving (hypercorn/quart)', ('hypercorn/', 'quart/', 'routes/')),
    ('logging', ('lib/log', 'logging/')),
)


# ── statistics ─────────────────────────────────────────────────────────────

def quantiles(values) -> dict:
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'n': 0}

    def q(p):
        position = p * (len(values) - 1)
        low = int(position)
        high = min(low + 1, len(values) - 1)
        return values[low] + (values[high] - values[low]) * (position - low)
    # Raw samples let runs be pooled for a comparison of n >= 30.
    return {'n': len(values), 'median': round(q(0.5), 3), 'p95': round(q(0.95), 3),
            'min': round(values[0], 3), 'max': round(values[-1], 3), 'mean': round(statistics.fmean(values), 3),
            'samples': [round(value, 3) for value in values]}


def session_metrics(records: list[dict], posted_ns: int | None = None, settled_ns: int | None = None) -> dict:
    """Per-session derived metrics from stub records (sorted by arrival)."""
    records = sorted(records, key=lambda r: r['arrived_ns'])
    gaps = [(b['arrived_ns'] - a['finished_ns']) / 1e6 for a, b in zip(records, records[1:])]
    out = {'requests': len(records), 'gaps_ms': gaps,
           'tools': [r['tool'] for r in records],
           'offered_tools': records[0].get('offered_tools') if records else None,
           'final_round_reached': bool(records) and records[-1]['tool'] is None,
           'body_bytes': [r['body_bytes'] for r in records],
           'prompt_chars': [r['prompt_chars'] for r in records],
           'prefix_ratio': [round(r['prefix_chars'] / r['previous_prompt_chars'], 4)
                            for r in records if r.get('previous_prompt_chars')]}
    if posted_ns is not None and records:
        out['ttfr_ms'] = (records[0]['arrived_ns'] - posted_ns) / 1e6
    if settled_ns is not None and records:
        out['settle_ms'] = (settled_ns - records[-1]['finished_ns']) / 1e6
    return out


# ── process observation ─────────────────────────────────────────────────────

def proc_sample(pid: int) -> dict:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        status = Path(f'/proc/{pid}/status').read_text()
        return {'cpu_s': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
                'rss_bytes': int(re.search(r'VmRSS:\s+(\d+)', status).group(1)) * 1024,
                'threads': int(re.search(r'Threads:\s+(\d+)', status).group(1))}
    except (OSError, AttributeError, IndexError):
        return {}


def shard_pids(primary: int) -> list[int]:
    try:
        from serverctl_pkg.execution_shards import live_shard_pids
    except ImportError:
        return []  # A tree measured before execution shards existed.
    return live_shard_pids(primary)


def shard_parent_pids(primary: int) -> list[int]:
    """Processes whose reaped children are shards: the primary and its fork server."""
    try:
        from serverctl_pkg.execution_shards import fork_server_pids
    except ImportError:
        return [primary]  # Shards were the primary's own children.
    return [primary, *fork_server_pids(primary)]


def reaped_children_cpu(pid: int) -> float:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return (int(fields[13]) + int(fields[14])) / os.sysconf('SC_CLK_TCK')
    except (OSError, IndexError, ValueError):
        return 0.0


class ResourceWindow:
    """CPU cores and RSS of named processes over one window, sampled every 0.25 s.

    ``execution_shards`` sums the execution host's on-demand shard processes
    and their fork server: live processes' own CPU plus what reaped shards
    left in their parents' children CPU; RSS is the sum over live shards per
    sample (the fork server's preloaded image is shared copy-on-write).
    """

    def __init__(self, pids: dict[str, int]):
        self.pids = pids
        self.stop = threading.Event()
        self.rss = collections.defaultdict(list)
        self.shard_cpu: dict[int, float] = {}
        self.shard_peak = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name='harness-perf-rss')

    def _sample_shards(self):
        primary = self.pids.get('execution_host')
        if not primary:
            return
        total_rss = 0
        live = shard_pids(primary)
        for server in shard_parent_pids(primary)[1:]:
            sample = proc_sample(server)
            if sample:
                # Its own CPU plus the shards it has reaped.
                self.shard_cpu[server] = (sample['cpu_s'] + reaped_children_cpu(server)
                                          - self.shard_start.get(server, 0.0))
        for pid in live:
            sample = proc_sample(pid)
            if sample:
                self.shard_cpu[pid] = sample['cpu_s'] - self.shard_start.get(pid, 0.0)
                total_rss += sample['rss_bytes']
        self.shard_peak = max(self.shard_peak, len(live))
        self.rss['execution_shards'].append(total_rss)

    def _run(self):
        while not self.stop.wait(0.25):
            for name, pid in self.pids.items():
                sample = proc_sample(pid)
                if sample:
                    self.rss[name].append(sample['rss_bytes'])
            self._sample_shards()

    def __enter__(self):
        self.started = time.monotonic()
        self.before = {name: proc_sample(pid) for name, pid in self.pids.items()}
        primary = self.pids.get('execution_host')
        self.shard_start = {pid: proc_sample(pid).get('cpu_s', 0.0) for pid in shard_pids(primary)} if primary else {}
        for server in (shard_parent_pids(primary)[1:] if primary else ()):
            self.shard_start[server] = proc_sample(server).get('cpu_s', 0.0) + reaped_children_cpu(server)
        self.reaped_before = reaped_children_cpu(primary) if primary else 0.0
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        self._sample_shards()
        elapsed = time.monotonic() - self.started
        after = {name: proc_sample(pid) for name, pid in self.pids.items()}
        self.result = {'elapsed_s': round(elapsed, 2), 'processes': {}}
        for name in self.pids:
            cpu = after[name].get('cpu_s', 0) - self.before[name].get('cpu_s', 0)
            rss = self.rss.get(name) or [after[name].get('rss_bytes', 0)]
            self.result['processes'][name] = {
                'cpu_cores': round(cpu / elapsed, 3) if elapsed else None,
                'rss_mb_mean': round(statistics.fmean(rss) / 2**20, 1), 'rss_mb_max': round(max(rss) / 2**20, 1),
                'threads': after[name].get('threads')}
        primary = self.pids.get('execution_host')
        if primary:
            cpu = sum(self.shard_cpu.values()) + reaped_children_cpu(primary) - self.reaped_before
            rss = self.rss.get('execution_shards') or [0]
            self.result['processes']['execution_shards'] = {
                'cpu_cores': round(cpu / elapsed, 3) if elapsed else None,
                'rss_mb_mean': round(statistics.fmean(rss) / 2**20, 1), 'rss_mb_max': round(max(rss) / 2**20, 1),
                'threads': None, 'peak_processes': self.shard_peak}


def attribute_profile(folded: str) -> dict:
    """Stage shares and top self frames of a ``py-spy --format raw`` profile."""
    stages = collections.Counter()
    leaves = collections.Counter()
    inclusive = collections.Counter()
    total = 0
    for line in folded.splitlines():
        stack, _, count = line.rpartition(' ')
        if not stack or not count.isdigit():
            continue
        count = int(count)
        total += count
        stage = next((name for name, needles in STAGES if any(n in stack for n in needles)), 'other')
        stages[stage] += count
        frames = [re.sub(r':\d+\)$', ')', f) for f in stack.split(';')]
        ours = [f for f in frames if '(lib/' in f or '(routes/' in f or '(serverctl_pkg/' in f]
        if ours:
            leaves[ours[-1]] += count
        for frame in set(ours):
            inclusive[frame] += count
    share = lambda n: round(n / total, 4) if total else 0
    return {'samples': total,
            'stages': {name: share(n) for name, n in stages.most_common()},
            'top_self_frames': [[f, share(n)] for f, n in leaves.most_common(20)],
            'top_inclusive_frames': [[f, share(n)] for f, n in inclusive.most_common(25)]}


# ── stub provider process ───────────────────────────────────────────────────

class Stub:
    def __init__(self, run_dir: Path):
        port_file = run_dir / 'stub.port'
        log = (run_dir / 'stub.log').open('ab')
        self.process = subprocess.Popen([sys.executable, str(HERE / 'stub_provider.py'), '--port-file',
                                         str(port_file)], stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        deadline = time.monotonic() + 30
        while not (port_file.exists() and port_file.read_text()):
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise SystemExit('stub provider did not start; see stub.log')
            time.sleep(0.02)
        self.port = int(port_file.read_text())
        self.base_url = f'http://127.0.0.1:{self.port}'

    def records(self) -> list[dict]:
        with urllib.request.urlopen(f'{self.base_url}/control/records', timeout=30) as response:
            return json.loads(response.read())

    def reset(self):
        urllib.request.urlopen(f'{self.base_url}/control/reset', timeout=30).read()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


# ── Tofu deployment under test ──────────────────────────────────────────────

def write_noop_files(directory: Path):
    """The one-line files the stub's ``noop`` read rounds name (f<k>.txt)."""
    for k in range(max(shape['rounds'] for shape in SHAPES.values()) + 1):
        (directory / f'f{k}.txt').write_text(f'noop {k}\n')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class TofuDeployment:
    """Storage + execution host + API host, as server_manager composes them."""

    def __init__(self, root: Path, stub: Stub, args):
        import agent_loop_load_test as agent_loop
        self.agent_loop = agent_loop
        self.root = root
        self.args = args
        driver_args = argparse.Namespace(backend='tofudb', startup_timeout=args.startup_timeout,
                                         random_seed=20261117)
        self.driver = agent_loop.Driver(driver_args, root)
        self.driver.model_port = stub.port
        self.api_process = None
        self.cold_start = {}

    def start(self):
        from lib.storage.connection_file import write_connection_document
        started = time.monotonic()
        status = self.driver.start_storage()
        self.cold_start['storage_s'] = round(time.monotonic() - started, 3)
        self.engine_pid = status.get('pid')
        run = self.root / 'run'
        run.mkdir(mode=0o700, exist_ok=True)
        self.connection_file = run / 'storage-connection.json'
        document = self.driver.storage.runtime._supervisor.connection_document(service_id=uuid.uuid4().hex)
        write_connection_document(self.connection_file, document)
        self.driver.connection_file = self.connection_file
        # Attachment checks projectPath against each process's
        # TOFU_PROJECT_PATH: the code checkout for the execution host, the
        # private project for the API host (see api_environment).
        self.api_connection_file = run / 'storage-connection-api.json'
        write_connection_document(self.api_connection_file,
                                  {**document, 'projectPath': str((self.root / 'project').resolve())})
        self.driver._install_model_route()
        workspace = self.root / 'workspace'
        workspace.mkdir(mode=0o700, exist_ok=True)
        write_noop_files(workspace)
        started = time.monotonic()
        self.driver._start_execution_host()
        self.cold_start['execution_host_s'] = round(time.monotonic() - started, 3)
        self.execution_pid = self.driver.execution_pid
        self.start_api_host()

    def api_environment(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith('TOFU_DB_')}
        # TOFU_RUN_SERVER: a scratch server owns its process (no handoff to a
        # lifecycle manager); the manager port names a free port so nothing
        # can reach the operator's running manager.
        env.update({'TOFU_STORAGE_CONNECTION_FILE': str(self.api_connection_file), 'TOFU_PROCESS_ROLE': 'api',
                    'PORT': str(self.port), 'BIND_HOST': '127.0.0.1', 'TOFU_RUN_SERVER': '1',
                    'TOFU_SUPERVISOR_PORT': str(_free_port()),
                    # The default heartbeat directory is host-global: the live
                    # Tofu's wedge evidence would name this host's pid instead.
                    'TOFU_HEARTBEAT_DIR': str(self.root / 'run' / 'heartbeat'),
                    # The API host finds its execution host through the
                    # ExecutionService record under <project>/data; the driver
                    # registered it under the private root, never the checkout.
                    'TOFU_PROJECT_PATH': str(self.root / 'project')})
        return env

    def start_api_host(self):
        self.port = _free_port()
        log = (self.root / 'run' / 'api-host.log').open('ab')
        started = time.monotonic()
        self.api_process = subprocess.Popen([sys.executable, 'server.py'], cwd=str(PROJECT_ROOT),
                                            env=self.api_environment(), stdin=subprocess.DEVNULL, stdout=log,
                                            stderr=subprocess.STDOUT, start_new_session=True)
        deadline = started + float(self.args.startup_timeout)
        while True:
            if self.api_process.poll() is not None:
                raise SystemExit(f'API host exited {self.api_process.returncode}; see {self.root}/run/api-host.log')
            try:
                status, payload = self.http('GET', '/api/ready', timeout=2)
                if status == 200 and payload.get('pid') == self.api_process.pid:
                    self.ready_payload = payload
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise SystemExit('API host did not become ready')
            time.sleep(0.05)
        self.cold_start.setdefault('api_host_s', []).append(round(time.monotonic() - started, 3))
        self.api_pid = self.api_process.pid

    def stop_api_host(self):
        if self.api_process and self.api_process.poll() is None:
            self.api_process.send_signal(signal.SIGTERM)
            try:
                self.api_process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(self.api_process.pid, signal.SIGKILL)
                self.api_process.wait()

    def close(self):
        self.stop_api_host()
        # A --references-only run never starts storage; there is nothing to stop.
        if getattr(self.driver.storage, 'runtime', None) is not None:
            self.driver.close()

    # HTTP -------------------------------------------------------------------
    def http(self, method: str, path: str, body: dict | None = None, timeout: float = 120):
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b'{}')

    # Turns ------------------------------------------------------------------
    def run_turn(self, conversation_id: str, prompt: str, *, first: bool, poll_s: float) -> dict:
        from lib.turn_lifecycle import get_attempt
        config = {**self.agent_loop._CHAT_CONFIG, 'model': self.agent_loop.MODEL_ID,
                  'projectPath': str(self.root / 'workspace')}
        body = {'commandId': f'hp-{uuid.uuid4().hex}', 'config': config, 'message': {'text': prompt}}
        if first:
            body['conversation'] = {'allowCreate': True, 'title': 'harness perf', 'settings': {}}
        posted_ns = time.monotonic_ns()
        status, payload = self.http('POST', f'/api/v4/conversations/{conversation_id}/turns', body)
        accepted_ns = time.monotonic_ns()
        value = payload.get('data', payload) if isinstance(payload, dict) else {}
        attempt_id = ((value.get('attempt') or {}).get('attemptId')) if isinstance(value, dict) else None
        if status >= 300 or not attempt_id:
            raise RuntimeError(f'turn refused: HTTP {status} {json.dumps(payload)[:600]}')
        attempt = {}
        deadline = time.monotonic() + float(self.args.turn_timeout)
        while attempt.get('status') not in TERMINAL:
            if time.monotonic() > deadline:
                raise RuntimeError(f'turn {attempt_id} did not settle: {attempt.get("status")}')
            time.sleep(poll_s)
            attempt = get_attempt(attempt_id, user_id=USER_ID) or {}
        return {'posted_ns': posted_ns, 'accepted_ms': (accepted_ns - posted_ns) / 1e6,
                'observed_settled_ns': time.monotonic_ns(), 'observed_settled_wall': time.time(),
                'attempt_id': attempt_id,
                'status': attempt.get('status'), 'attempt': attempt}


def _prompt(scenario: str, rounds: int, session: str) -> str:
    return f'Benchmark turn. [harness-perf scenario={scenario} rounds={rounds} session={session}]'


def _settled_ns(turn: dict) -> int:
    """Durable settle instant on the monotonic clock when the attempt records one.

    Otherwise the poll observation, which over-states settle by up to one
    poll interval.
    """
    attempt = turn['attempt']
    for key in ('settledAt', 'completedAt', 'finishedAt', 'endedAt'):
        value = attempt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            wall = value / 1000 if value > 1e11 else value
            turn['settle_source'] = key
            return int(turn['observed_settled_ns'] - (turn['observed_settled_wall'] - wall) * 1e9)
    turn['settle_source'] = 'poll'
    return turn['observed_settled_ns']


# ── phases ──────────────────────────────────────────────────────────────────

def sequential_phase(deployment: TofuDeployment, stub: Stub, scenario: str, turns: int, rounds: int,
                     poll_s: float) -> dict:
    sessions = []
    conversation_id = f'hp{scenario}{uuid.uuid4().hex[:10]}'
    for index in range(turns):
        session = f'{scenario}-{uuid.uuid4().hex[:12]}'
        turn = deployment.run_turn(conversation_id, _prompt(scenario, rounds, session), first=index == 0,
                                   poll_s=poll_s)
        records = [r for r in stub.records() if r['session'] == session]
        metrics = session_metrics(records, turn['posted_ns'], _settled_ns(turn))
        metrics.update(status=turn['status'], accepted_ms=turn['accepted_ms'], index=index,
                       settle_source=turn['settle_source'])
        sessions.append(metrics)
    return summarize(scenario, sessions)


def summarize(scenario: str, sessions: list[dict]) -> dict:
    # Round 0's request carries the user message; per-round series start there.
    return {
        'scenario': scenario, 'turns': len(sessions),
        'statuses': dict(collections.Counter(s.get('status') for s in sessions)),
        'all_reached_final_round': all(s['final_round_reached'] for s in sessions),
        'ttfr_ms': quantiles(s.get('ttfr_ms') for s in sessions),
        # Gap after a tool call: response done -> tool -> result -> next request.
        'round_overhead_ms': quantiles(g for s in sessions for g in s['gaps_ms']),
        # The first tool round of a Turn pays one-time per-Turn work; later ones are steady state.
        'first_round_overhead_ms': quantiles(s['gaps_ms'][0] for s in sessions if s['gaps_ms']),
        'steady_round_overhead_ms': quantiles(g for s in sessions for g in s['gaps_ms'][1:]),
        'settle_ms': quantiles(s.get('settle_ms') for s in sessions),
        'accept_ms': quantiles(s.get('accepted_ms') for s in sessions),
        'settle_source': dict(collections.Counter(s.get('settle_source') for s in sessions)),
        'requests_per_turn': quantiles(s['requests'] for s in sessions),
        'body_bytes_round0': quantiles(s['body_bytes'][0] for s in sessions if s['body_bytes']),
        'body_bytes_growth_per_round': quantiles(
            (s['body_bytes'][-1] - s['body_bytes'][0]) / (len(s['body_bytes']) - 1)
            for s in sessions if len(s['body_bytes']) > 1),
        'approx_tokens_round0': quantiles(s['prompt_chars'][0] / 4 for s in sessions if s['prompt_chars']),
        'prefix_ratio': quantiles(r for s in sessions for r in s['prefix_ratio']),
        'prefix_breaks': sum(1 for s in sessions for r in s['prefix_ratio'] if r < 0.999),
        'tools_seen': dict(collections.Counter(t for s in sessions for t in s['tools'])),
        # Requests beyond the script (rounds + final) are harness-initiated rounds.
        'extra_requests': sum(max(0, len(s['tools']) - 1 - sum(1 for t in s['tools'] if t)) for s in sessions),
        'offered_tools_round0': next((s['offered_tools'] for s in sessions if s.get('offered_tools')), None),
    }


# Harness-initiated corrections that add rounds or stop a Turn; a scripted
# scenario must trigger none of them.
INTERVENTIONS = ('Intent-stall nudge', 'Tool-progress correction', 'FORCE STOP', 'SUSPICIOUS COMPLETION')
TRACE_SUMMARY = re.compile(r'\[TurnTraceSummary\] .*?conv=(\S+) status=(\S+) (.*)$')


def turn_trace_breakdown(app_log: Path) -> dict:
    """Tofu's own per-Turn clock split (``[TurnTraceSummary]``, docs/LOGGING.md) per phase.

    Conversation id prefixes name the phase (``hp<scenario>`` sequential,
    ``hpc`` concurrent). Values are medians in ms over the phase's Turns.
    """
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    interventions = collections.defaultdict(collections.Counter)
    try:
        lines = app_log.read_text(errors='replace').splitlines()
    except OSError:
        return {}
    for line in lines:
        marker = next((m for m in INTERVENTIONS if m in line), None)
        if marker:
            conversation = (re.search(r'conv=(\S+)', line) or [None, ''])[1]
            interventions[_phase_of(conversation) or 'other'][marker] += 1
        match = TRACE_SUMMARY.search(line)
        if not match:
            continue
        conversation, _status, fields = match.groups()
        phase = _phase_of(conversation)
        if phase is None:
            continue
        for key, value in re.findall(r'(\w+)=(-?\d+(?:\.\d+)?)\b', fields):
            groups[phase][key].append(float(value))
    out = {phase: {key: round(statistics.median(values), 1) for key, values in fields.items()}
           for phase, fields in groups.items()}
    for phase, counts in interventions.items():
        out.setdefault(phase, {})['interventions'] = dict(counts)
    return out


def _phase_of(conversation: str) -> str | None:
    return next((name for name in ('chat', 'true', 'noop', 'shell') if conversation.startswith(f'hp{name}')),
                'concurrent' if conversation.startswith('hpc') else None)


def concurrent_phase(deployment: TofuDeployment, stub: Stub, sessions: int, turns: int, rounds: int,
                     poll_s: float, run_dir: Path, py_spy: str) -> dict:
    results: list[dict] = []
    errors = collections.Counter()
    lock = threading.Lock()
    barrier = threading.Barrier(sessions)

    def worker(index: int):
        conversation_id = f'hpc{index:02d}{uuid.uuid4().hex[:8]}'
        barrier.wait()
        for turn_index in range(turns):
            session = f'conc-{index}-{uuid.uuid4().hex[:10]}'
            try:
                turn = deployment.run_turn(conversation_id, _prompt('shell', rounds, session),
                                           first=turn_index == 0, poll_s=poll_s)
            except Exception as exc:
                with lock:
                    errors[f'{type(exc).__name__}: {str(exc)[:160]}'] += 1
                continue
            with lock:
                results.append({'session': session, 'turn': turn, 'turn_index': turn_index})
    pids = {'api_host': deployment.api_pid, 'execution_host': deployment.execution_pid,
            'storage_engine': deployment.engine_pid}
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(sessions)]
    profilers = start_profilers(py_spy, pids, run_dir, f'concurrent{sessions}')
    with ResourceWindow(pids) as window:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    stop_profilers(profilers)
    records = collections.defaultdict(list)
    for record in stub.records():
        records[record['session']].append(record)
    per_session = []
    for result in results:
        metrics = session_metrics(records.get(result['session'], []), result['turn']['posted_ns'],
                                  _settled_ns(result['turn']))
        metrics['status'] = result['turn']['status']
        metrics['settle_source'] = result['turn'].get('settle_source')
        metrics['turn_index'] = result['turn_index']
        per_session.append(metrics)
    summary = summarize('shell', per_session)
    # The first Turn of a burst starts before demand-driven execution capacity
    # exists; later Turns show the steady state (docs/HARNESS_PERFORMANCE.md).
    summary['by_turn'] = {
        index: {key: value for key, value in summarize('shell', [m for m in per_session if m['turn_index'] == index]).items()
                if key in ('round_overhead_ms', 'ttfr_ms', 'settle_ms', 'turns')}
        for index in sorted({m['turn_index'] for m in per_session})}
    summary.update(sessions=sessions, errors=dict(errors), resources=window.result,
                   profiles={name: str(path) for name, path in profilers.get('paths', {}).items()})
    return summary


def start_profilers(py_spy: str, pids: dict, run_dir: Path, label: str) -> dict:
    if not py_spy or not Path(py_spy).is_file():
        return {}
    processes, paths = [], {}
    for name in ('api_host', 'execution_host'):
        for fmt, suffix in (('raw', 'folded'), ('flamegraph', 'svg')):
            path = run_dir / f'{label}-{name}.{suffix}'
            paths[f'{name}.{suffix}'] = path
            processes.append(subprocess.Popen(
                [py_spy, 'record', '--pid', str(pids[name]), '--nonblocking', '--rate', '100',
                 '--format', fmt, '--output', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return {'processes': processes, 'paths': paths}


def stop_profilers(profilers: dict):
    for process in profilers.get('processes', []):
        process.send_signal(signal.SIGINT)
    for process in profilers.get('processes', []):
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()


# ── report ──────────────────────────────────────────────────────────────────

def host_load() -> dict:
    load1, load5, load15 = os.getloadavg()
    return {'cpus': os.cpu_count(), 'loadavg': [round(load1, 2), round(load5, 2), round(load15, 2)]}


def _q(entry: dict | None, unit='') -> str:
    if not entry or not entry.get('n'):
        return '-'
    return f"{entry['median']:g}{unit} / {entry['p95']:g}{unit} (n={entry['n']})"


def markdown(report: dict) -> str:
    lines = [f"# Harness performance run {report['run_id']} ({report['mode']})", '',
             f"commit `{report['commit']}`, host load before {report['host_load_before']}, "
             f"after {report.get('host_load_after')}", '', '## Tofu', '',
             f"Cold start (s): {json.dumps(report['tofu'].get('cold_start'))}", '',
             '| scenario | TTFR ms | round overhead ms | first tool round ms | steady round ms | settle ms | '
             'POST accept ms | body bytes r0 | growth B/round | prefix ratio | breaks |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    for name, phase in report['tofu']['phases'].items():
        lines.append(f"| {name} | {_q(phase['ttfr_ms'])} | {_q(phase['round_overhead_ms'])} | "
                     f"{_q(phase['first_round_overhead_ms'])} | {_q(phase['steady_round_overhead_ms'])} | "
                     f"{_q(phase['settle_ms'])} | {_q(phase['accept_ms'])} | {_q(phase['body_bytes_round0'])} | "
                     f"{_q(phase['body_bytes_growth_per_round'])} | {_q(phase['prefix_ratio'])} | "
                     f"{phase['prefix_breaks']} |")
        for index, part in (phase.get('by_turn') or {}).items():
            lines.append(f"| {name}, Turn {index + 1} of each session | {_q(part['ttfr_ms'])} | "
                         f"{_q(part['round_overhead_ms'])} | | | {_q(part['settle_ms'])} | | | | | |")
    lines += ['', '| window | process | CPU cores | RSS MB mean / max | threads |', '|---|---|---|---|---|']
    for window_name, window in report['tofu']['resources'].items():
        for name, row in window['processes'].items():
            lines.append(f"| {window_name} | {name} | {row['cpu_cores']} | {row['rss_mb_mean']} / "
                         f"{row['rss_mb_max']} | {row['threads']} |")
    for name, phase in report['tofu']['phases'].items():
        if phase.get('cpu_ms_per_request'):
            lines += ['', f"CPU ms per model request ({name}): {json.dumps(phase['cpu_ms_per_request'])}"]
    trace = report['tofu'].get('turn_trace_ms') or {}
    if trace:
        keys = ['totalMs', 'llmMs', 'toolMs', 'orchestrationMs', 'startupMs', 'roundPrepMs', 'roundSettleMs',
                'finalizeMs', 'rounds']
        lines += ['', 'Tofu TurnTraceSummary medians (ms):', '', '| phase | ' + ' | '.join(keys) + ' |',
                  '|---|' + '---|' * len(keys)]
        lines += [f"| {phase} | " + ' | '.join(str(values.get(k, '-')) for k in keys) + ' |'
                  for phase, values in trace.items()]
        lines += ['', 'Harness interventions (log markers; a scripted run should have none): ' + json.dumps(
            {phase: values['interventions'] for phase, values in trace.items() if values.get('interventions')})]
    for name, profile in report['tofu'].get('profile_attribution', {}).items():
        lines += ['', f'### Profile {name} ({profile["samples"]} samples)', '', '| stage | share |', '|---|---|']
        lines += [f'| {stage} | {share} |' for stage, share in profile['stages'].items()]
        lines += ['', '| top self frame | share |', '|---|---|']
        lines += [f'| `{frame}` | {share} |' for frame, share in profile['top_self_frames'][:12]]
    if report.get('references'):
        lines += ['', '## References', '', '| harness | scenario | TTFR ms (from launch) | round overhead ms | '
                  'exit after last response ms | requests | final | peak RSS MB | CPU s |', '|---|---|---|---|---|---|---|---|---|']
        for row in report['references']:
            lines.append(f"| {row['name']} | {row['scenario']} | {_q(row.get('ttfr_ms'))} | "
                         f"{_q(row.get('round_overhead_ms'))} | {_q(row.get('exit_ms'))} | "
                         f"{row.get('requests', '-')} | {row.get('final', '-')} | {row.get('peak_rss_mb', '-')} | "
                         f"{row.get('cpu_s', '-')} |")
    return '\n'.join(lines) + '\n'


def run_references(args, stub: Stub, shape: dict, run_dir: Path) -> list[dict]:
    import references
    names = [n for n in (args.references or '').split(',') if n]
    if names == ['all']:
        names = list(references.REFERENCES)
    rows = []
    cache = Path(args.reference_cache).expanduser()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    repeats = args.reference_repeats or (1 if args.smoke else 3)
    scratch = Path(args.root) / 'references'
    scratch.mkdir(mode=0o700, exist_ok=True)
    for name in names:
        for scenario in shape['references']:
            sessions, extra = [], collections.defaultdict(list)
            for _ in range(repeats):
                session = f'ref-{name}-{scenario}-{uuid.uuid4().hex[:8]}'
                # Scratch on the private root's local disk: --out may be a network
                # volume, where codex start-up took seconds and opencode died (SIGILL).
                work = Path(tempfile.mkdtemp(prefix=f'{name}-', dir=scratch))
                (work / 'workdir').mkdir()
                write_noop_files(work / 'workdir')
                (work / 'home').mkdir()
                try:
                    result = references.run_reference(
                        name, stub_base_url=stub.base_url,
                        prompt=_prompt(scenario, 1 if scenario == 'true' else shape['rounds'], session),
                        workdir=str(work / 'workdir'), home=str(work / 'home'), cache_dir=str(cache),
                        timeout_s=float(args.turn_timeout))
                except Exception as exc:
                    rows.append({'name': name, 'scenario': scenario, 'error': f'{type(exc).__name__}: {exc}'})
                    break
                records = [r for r in stub.records() if r['session'] == session]
                metrics = session_metrics(records, result['launched_ns'], None)
                if records:
                    metrics['exit_ms'] = (result['exited_ns'] - max(r['finished_ns'] for r in records)) / 1e6
                metrics['status'] = 'completed' if result['returncode'] == 0 else f"rc={result['returncode']}"
                sessions.append(metrics)
                extra['peak_rss'].append(result.get('peak_rss_bytes') or 0)
                extra['cpu_s'].append(result.get('cpu_s') or 0)
                extra['version'] = result.get('version')
                shutil.rmtree(work, ignore_errors=True)
            if not sessions:
                continue
            summary = summarize(scenario, sessions)
            rows.append({'name': name, 'version': extra['version'], 'scenario': scenario,
                         'ttfr_ms': summary['ttfr_ms'], 'round_overhead_ms': summary['round_overhead_ms'],
                         'exit_ms': quantiles(s.get('exit_ms') for s in sessions),
                         'requests': summary['requests_per_turn'].get('median'),
                         'final': summary['all_reached_final_round'], 'statuses': summary['statuses'],
                         'body_bytes_round0': summary['body_bytes_round0'],
                         'body_bytes_growth_per_round': summary['body_bytes_growth_per_round'],
                         'prefix_ratio': summary['prefix_ratio'], 'prefix_breaks': summary['prefix_breaks'],
                         'tools_seen': summary['tools_seen'],
                         'peak_rss_mb': round(max(extra['peak_rss']) / 2**20, 1),
                         'cpu_s': round(statistics.median(extra['cpu_s']), 3),
                         'cpu_ms_per_request_incl_startup': round(
                             statistics.median(extra['cpu_s']) * 1000 / summary['requests_per_turn']['median'], 1)
                         if summary['requests_per_turn'].get('median') else None})
    return rows


def driver_main(args) -> int:
    root = Path(args.root).resolve()
    shape = dict(SHAPES['smoke' if args.smoke else 'full'])
    run_dir = Path(args.out).resolve() / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(['git', 'rev-parse', '--short=12', 'HEAD'], cwd=PROJECT_ROOT, capture_output=True,
                            text=True).stdout.strip()
    report = {'run_id': args.run_id, 'mode': 'smoke' if args.smoke else 'full', 'commit': commit,
              'shape': shape, 'host_load_before': host_load(), 'root': str(root), 'tofu': {'phases': {},
                                                                                         'resources': {}}}
    started = time.monotonic()
    stub = Stub(run_dir)
    deployment = TofuDeployment(root, stub, args)
    poll_s = 0.01

    def save():
        report['elapsed_s'] = round(time.monotonic() - started, 1)
        (run_dir / 'report.json').write_text(json.dumps(report, indent=1, default=str))
        (run_dir / 'report.md').write_text(markdown(report))
    try:
        if not args.references_only:
            deployment.start()
            report['tofu']['cold_start'] = deployment.cold_start
            report['tofu']['pids'] = {'api_host': deployment.api_pid, 'execution_host': deployment.execution_pid,
                                      'storage_engine': deployment.engine_pid}
            # Warm-up turn: first-use imports and caches are cold start, not round overhead.
            sequential_phase(deployment, stub, 'chat', 1, 0, poll_s)
            pids = report['tofu']['pids']
            with ResourceWindow(pids) as idle:
                time.sleep(shape['idle_s'])
            report['tofu']['resources']['idle'] = idle.result
            sequential_profilers = start_profilers(args.py_spy, pids, run_dir, 'sequential')
            for scenario, turns, rounds in (('chat', shape['chat_turns'], 0),
                                            ('true', shape['true_turns'], 1),
                                            ('noop', shape['tool_turns'], shape['rounds']),
                                            ('shell', shape['tool_turns'], shape['rounds'])):
                report['tofu']['phases'][scenario] = sequential_phase(deployment, stub, scenario, turns, rounds,
                                                                      poll_s)
                print(f"{scenario}: {json.dumps(report['tofu']['phases'][scenario]['round_overhead_ms'])}",
                      flush=True)
                save()
            stop_profilers(sequential_profilers)
            concurrent = concurrent_phase(deployment, stub, shape['sessions'], shape['concurrent_turns'],
                                          shape['rounds'], 0.05, run_dir, args.py_spy)
            report['tofu']['phases'][f'shell x{shape["sessions"]} concurrent'] = concurrent
            window = concurrent.pop('resources')
            report['tofu']['resources'][f'{shape["sessions"]} sessions'] = window
            # CPU per model request bounds throughput: one interpreter serves every session.
            requests = sum(concurrent['tools_seen'].values())
            concurrent['cpu_ms_per_request'] = {
                name: round(row['cpu_cores'] * window['elapsed_s'] * 1000 / requests, 1)
                for name, row in window['processes'].items()} if requests else {}
            attribution = {}
            for label, paths in (('sequential', sequential_profilers.get('paths', {})),
                                 (f'concurrent{shape["sessions"]}', concurrent.get('profiles', {}))):
                for name, path in paths.items():
                    if str(name).endswith('.folded') and Path(path).exists():
                        attribution[f'{label} {name[:-len(".folded")]}'] = attribute_profile(
                            Path(path).read_text(errors='replace'))
            report['tofu']['profile_attribution'] = attribution
            report['tofu']['turn_trace_ms'] = turn_trace_breakdown(root / 'logs' / 'app.log')
            save()
            for _ in range(args.api_restarts):
                deployment.stop_api_host()
                deployment.start_api_host()
            report['tofu']['cold_start'] = deployment.cold_start
            save()
        if args.references:
            report['references'] = run_references(args, stub, shape, run_dir)
        report['host_load_after'] = host_load()
        save()
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report.setdefault('host_load_after', host_load())
        try:
            deployment.close()
        finally:
            stub.close()
        save()
    print(f'report: {run_dir / "report.md"}', flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--smoke', action='store_true', help='CI-sized run (under 2 minutes)')
    parser.add_argument('--root', help='private working root (default: a fresh directory under the host temp root)')
    parser.add_argument('--out', default=str(PROJECT_ROOT / 'output' / 'harness_perf'))
    parser.add_argument('--engine', default=os.environ.get('TOFUDB_BIN', ''), help='tofu-db binary (default $TOFUDB_BIN)')
    parser.add_argument('--references', default='', help="comma list of codex,claude-code,opencode,pi or 'all'")
    parser.add_argument('--references-only', action='store_true')
    parser.add_argument('--reference-cache', default=str(Path(tempfile.gettempdir()) / 'harness-perf-refs-cache'))
    parser.add_argument('--reference-repeats', type=int, default=0)
    parser.add_argument('--api-restarts', type=int, default=None, help='extra API-host cold starts (default 0 smoke, 4 full)')
    parser.add_argument('--py-spy', default=shutil.which('py-spy') or '')
    parser.add_argument('--startup-timeout', type=float, default=600.0)
    parser.add_argument('--turn-timeout', type=float, default=300.0)
    parser.add_argument('--run-id', default='')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.api_restarts is None:
        args.api_restarts = 0 if args.smoke else 4
    if args.child:
        return driver_main(args)
    from serverctl_pkg.local_placement import temporary_root
    keep_root = bool(args.root)
    root = Path(args.root).expanduser() if args.root else Path(tempfile.mkdtemp(prefix='harness-perf-',
                                                                                    dir=temporary_root()))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    storage_harness._refuse_live_root(root)
    (root / '.tofudb-load-test').touch()
    if not args.engine or not Path(args.engine).is_file():
        raise SystemExit('--engine (or $TOFUDB_BIN) must name a tofu-db binary')
    engine, digest = storage_harness._pin_engine(Path(args.engine), root)
    args.run_id = args.run_id or time.strftime('%Y%m%d-%H%M%S') + ('-smoke' if args.smoke else '')
    scratch = Path(tempfile.mkdtemp(prefix='harness-perf-tmp-', dir=temporary_root()))
    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--root', str(root.resolve()),
               '--out', args.out, '--references', args.references, '--reference-cache', args.reference_cache,
               '--reference-repeats', str(args.reference_repeats), '--api-restarts', str(args.api_restarts),
               '--py-spy', args.py_spy, '--startup-timeout', str(args.startup_timeout),
               '--turn-timeout', str(args.turn_timeout), '--run-id', args.run_id]
    command += ['--smoke'] if args.smoke else []
    command += ['--references-only'] if args.references_only else []
    try:
        environment = storage_harness._child_environment(root.resolve(), scratch, engine, digest, 'auto', 'tofudb')
        # The execution host admits only the source tree it runs from.
        environment['TOFU_PROJECT_PATH'] = str(PROJECT_ROOT)
        environment['TOFU_SUPERVISOR_PORT'] = str(_free_port())
        # The API host's boot needs headless Chromium; the private HOME has no
        # browser registry, so borrow the operator's (read only).
        browsers = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or str(Path.home() / '.cache' / 'ms-playwright')
        if Path(browsers).is_dir():
            environment['PLAYWRIGHT_BROWSERS_PATH'] = browsers
        return subprocess.run(command, env=environment, cwd=str(root.resolve())).returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if not keep_root:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
