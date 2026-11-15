#!/usr/bin/env python3
"""Production-shaped load test of the real Agent execution path.

Responsibility: measure what users wait on while Agents run -- storage latency
as the Agent process observes it, and that process's CPU and GIL saturation.
It starts the real resident execution host (``serverctl_pkg.execution_host``,
the same headless application lifespan, durable worker, task runtime, agent
loop, tool execution and event persistence production runs) against ONE
isolated storage authority, submits Turns the way the API host does
(``conversation_turn_commands.create_turn``: create the Turn pair, dispatch the
accepted attempt to the durable worker) and answers every model request from a
local deterministic Anthropic Messages endpoint paced like production.

Entry:
    python3 scripts/agent_loop_load_test.py --root DIR [--scales 1,3,5]
        [--duration 300] [--engine PATH] [--backend tofudb|sqlite]
        [--production-metrics BEFORE.prom AFTER.prom]

Processes (all under DIR, all started and stopped here):
    parent  -- creates DIR 0700, pins the engine, launches the driver child
               with a scrubbed environment.
    driver  -- owns the authority (``lib.storage.StorageSupervisor``), publishes
               its connection file, plays the API host and the browser
               (Turn submission, attempt status), reads the execution host's
               storage RPC histogram through the private execution control.
    fake model -- ``--fake-model`` child; streams thinking, text and one tool
               call per round (run_command/read_files/search_files on a scratch
               workspace) with production's measured pacing.
    execution host -- ``serverctl_pkg.execution_host`` launched through the
               production ``ExecutionService`` owner; the measured Agent process.

Inputs: ``PRODUCTION_PACING`` (measured 2026-11-12; sources beside each value
and in docs/STORAGE_PERFORMANCE_CHARTER.md "Agent-loop load test") plus the
storage harness's ``PRODUCTION_PROFILE`` (running attempts, tool result bytes).
Outputs: ``DIR/results/<run-id>/report.json`` and ``report.md``; per phase a
raw ``py-spy --gil`` profile ``gil-<scale>x.folded``.

Safety: identical to scripts/tofudb_load_test.py, whose helpers are imported:
DIR is private (0700) and refused inside a live data directory, every child
runs with HOME/TMPDIR/data under DIR, the engine binary is pinned by copy and
SHA-256, and the run stops every process it started. Production is only read
(``--production-metrics`` takes saved ``/metrics`` snapshots).

Dependencies: scripts/tofudb_load_test.py, serverctl_pkg.execution_service,
lib.conversation_sync, lib.turn_lifecycle, lib.model_routing, lib.storage,
lib.tasks_pkg.command_control_runtime, lib.observability; py-spy (optional).
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import http.server
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tofudb_load_test as storage_harness  # noqa: E402  (same directory; helpers reused, not copied)
from tofudb_load_test import PRODUCTION_PROFILE, _lognormal_bytes, _text  # noqa: E402

PROJECT_ROOT = storage_harness.PROJECT_ROOT
USER_ID = 1
MARKER = 'agent-loop-load'
MODEL_ID = 'agent-loop-load-model'
CREATOR_ID = 'tofu-agent-loop-load'

# Model pacing measured 2026-11-12 from the apiRounds of production's 60 most
# recently updated conversations (13,294 rounds, read-only turn.list through
# the authenticated storage client; ``usage._dispatch`` timestamps). Values are
# (median, p90) and describe the workload; none tunes the harness.
PRODUCTION_PACING = {
    # usage._dispatch.protocol: 13,290 anthropic, 4 openai.
    'protocol': 'anthropic',
    # apiRounds per assistant Turn (121 Turns).
    'rounds_per_turn': (62, 264),
    # usage._dispatch.ttft_ms: request start to first content.
    'ttft_ms': (11_474, 28_348),
    # usage.output_tokens per round.
    'output_tokens_per_round': (568, 2_865),
    # sum(output_tokens) / sum(stream_completed - first_content): 1,217 tokens in 9.32 s.
    'output_tokens_per_s': 1_217 / 9.318,
    # usage._chunks_received per round over output tokens: 137 / 568.
    'chunks_per_output_token': 137 / 568,
    # usage._reasoning_chars and _tool_argument_chars per round.
    'reasoning_chars_per_round': (244, 2_092),
    'tool_argument_chars_per_round': (443, 2_053),
    # usage._content_chars per round: median 0, mean 49, p90 122.
    'content_chars_per_round': (49, 122),
    # toolRounds by name among the cheap real tools (8,866 / 1,516 / 167); one
    # call per round (median 1, mean 1.1).
    'tool_mix': {'run_command': 8_866, 'read_files': 1_516, 'search_files': 167},
    # Inspector-export result sizes, shared with the storage-only harness.
    'tool_result_bytes': PRODUCTION_PROFILE['tool_result_bytes'],
}

# Chat Turn configuration as the browser submits it (production attempt config
# of a chat conversation, 2026-11-12), with the load model and scratch workspace.
_CHAT_CONFIG = {
    'chatMode': 'chat', 'autoApply': True, 'autoTranslate': False, 'autopilot': False,
    'browserEnabled': False, 'codeExecEnabled': True, 'desktopEnabled': False, 'fetchEnabled': False,
    'humanGuidanceEnabled': False, 'imageGenEnabled': False, 'memoryEnabled': False,
    'planMode': False, 'schedulerEnabled': False, 'searchMode': 'off', 'systemPrompt': '',
    'systemPromptMode': 'append', 'tools': {'executionScope': 'available', 'programmaticCalling': 'on',
                                            'toolSearch': 'local'},
}
_TERMINAL_ATTEMPT = frozenset({'completed', 'truncated', 'failed', 'interrupted'})
_CORPUS_FILES = 64
# py-spy's own default sampling rate; the GIL busy fraction divides by it.
PY_SPY_RATE_HZ = 100


def _contract(name: str) -> dict:
    return json.loads((PROJECT_ROOT / 'contracts' / name).read_text())


# ── fake model (separate process) ─────────────────────────────────────────────

def _round_plan(nonce: str, round_index: int, target_rounds: int, *, draining: bool, tools: set[str]) -> dict:
    """Deterministic content of one model round."""
    rng = random.Random(f'{nonce}:{round_index}')
    pacing = PRODUCTION_PACING
    final = draining or round_index + 1 >= target_rounds
    plan = {
        'final': final,
        'ttft_s': _lognormal_bytes(rng, *pacing['ttft_ms']) / 1000,
        'output_tokens': _lognormal_bytes(rng, *pacing['output_tokens_per_round']),
        'thinking': _text(rng, _lognormal_bytes(rng, *pacing['reasoning_chars_per_round'])),
        'text': _text(rng, _lognormal_bytes(rng, *pacing['content_chars_per_round'])),
        'tool': None,
    }
    if not final:
        mix = {name: weight for name, weight in pacing['tool_mix'].items() if name in tools}
        if not mix:
            plan['final'] = True
            return plan
        name = rng.choices(list(mix), weights=list(mix.values()))[0]
        size = _lognormal_bytes(rng, *pacing['tool_result_bytes'])
        if name == 'run_command':
            padding = max(0, _lognormal_bytes(rng, *pacing['tool_argument_chars_per_round']) - 60)
            arguments = {'command': f'head -c {size} corpus/blob.txt', 'description': _text(rng, padding)}
        elif name == 'read_files':
            arguments = {'path': f'corpus/file-{rng.randrange(_CORPUS_FILES):02d}.txt'}
        else:
            arguments = {'pattern': rng.choice(('storage authority', 'synthetic sentence', 'browser sees')),
                         'path': 'corpus'}
        plan['tool'] = {'id': f'toolu_{nonce[:12]}_{round_index:04d}', 'name': name, 'input': arguments}
    return plan


def _load_marker(messages: list) -> tuple[int, str, int] | None:
    """(rounds, nonce, rounds done) from the newest load-test user message."""
    found = None
    for index, message in enumerate(messages):
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        texts = [content] if isinstance(content, str) else [
            block.get('text', '') for block in content or [] if isinstance(block, dict)]
        for text in texts:
            match = re.search(rf'\[{MARKER} rounds=(\d+) turn=([0-9a-f]+)\]', text or '')
            if match:
                found = (int(match.group(1)), match.group(2), index)
    if found is None:
        return None
    rounds, nonce, index = found
    done = sum(1 for message in messages[index + 1:] if message.get('role') == 'assistant')
    return rounds, nonce, done


def _tool_result_ids(messages: list) -> list[str]:
    if not messages or messages[-1].get('role') != 'user' or not isinstance(messages[-1].get('content'), list):
        return []
    return [block.get('tool_use_id') for block in messages[-1]['content']
            if isinstance(block, dict) and block.get('type') == 'tool_result']


class FakeModelState:
    def __init__(self):
        self.lock = threading.Lock()
        self.draining = False
        self.streams: dict[str, tuple[float, float]] = {}  # tool_use id -> (first token, last byte)
        self.samples = collections.defaultdict(list)
        self.counters = collections.Counter()

    def round_latency(self, tool_ids: list[str], arrived: float):
        with self.lock:
            for tool_id in tool_ids:
                timing = self.streams.pop(tool_id, None)
                if timing:
                    self.samples['first_token_to_next_request_s'].append(arrived - timing[0])
                    self.samples['last_token_to_next_request_s'].append(arrived - timing[1])

    def snapshot(self, reset: bool) -> dict:
        with self.lock:
            out = {'samples': {key: list(values) for key, values in self.samples.items()},
                   'counters': dict(self.counters), 'draining': self.draining}
            if reset:
                self.samples.clear()
                self.counters.clear()
        return out


def _sse(event: str, payload: dict) -> bytes:
    return f'event: {event}\ndata: {json.dumps(payload, separators=(",", ":"))}\n\n'.encode()


def _fake_model_handler(state: FakeModelState):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *args):
            pass

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith('/control/stats'):
                return self._json(200, state.snapshot(reset='reset=1' in self.path))
            return self._json(200, {'data': [{'id': MODEL_ID, 'type': 'model'}]})

        def do_POST(self):
            arrived = time.monotonic()
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            if self.path.startswith('/control/drain'):
                with state.lock:
                    state.draining = bool(body.get('draining'))
                return self._json(200, {'draining': state.draining})
            if not self.path.endswith('/messages'):
                return self._json(404, {'type': 'error', 'error': {'type': 'not_found_error', 'message': self.path}})
            messages = body.get('messages') or []
            marker = _load_marker(messages)
            state.round_latency(_tool_result_ids(messages), arrived)
            tools = {tool.get('name') for tool in body.get('tools') or [] if isinstance(tool, dict)}
            with state.lock:
                draining = state.draining
                state.counters['requests'] += 1
                state.counters['requests:load' if marker else 'requests:auxiliary'] += 1
            if marker:
                rounds, nonce, done = marker
                plan = _round_plan(nonce, done, rounds, draining=draining, tools=tools)
            else:
                # Auxiliary calls (titles, summaries): one short final answer.
                plan = _round_plan(hashlib.sha256(json.dumps(messages)[:4096].encode()).hexdigest()[:16],
                                   0, 1, draining=True, tools=set())
                plan['ttft_s'] = min(plan['ttft_s'], 1.0)
            input_tokens = len(json.dumps(messages)) // 4
            if not body.get('stream'):
                return self._json(200, self._message(plan, input_tokens))
            self._stream(plan, input_tokens, arrived)

        def _message(self, plan, input_tokens):
            content = [{'type': 'thinking', 'thinking': plan['thinking'], 'signature': _signature(plan)},
                       {'type': 'text', 'text': plan['text'] or 'Done.'}]
            if plan['tool']:
                content.append({'type': 'tool_use', **plan['tool']})
            return {'id': f'msg_{uuid.uuid4().hex[:20]}', 'type': 'message', 'role': 'assistant', 'model': MODEL_ID,
                    'content': content, 'stop_reason': 'tool_use' if plan['tool'] else 'end_turn',
                    'stop_sequence': None, 'usage': {'input_tokens': input_tokens,
                                                     'output_tokens': plan['output_tokens']}}

        def _stream(self, plan, input_tokens, arrived):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            write = self.wfile.write
            try:
                write(_sse('message_start', {'type': 'message_start', 'message': {
                    'id': f'msg_{uuid.uuid4().hex[:20]}', 'type': 'message', 'role': 'assistant', 'model': MODEL_ID,
                    'content': [], 'stop_reason': None, 'stop_sequence': None,
                    'usage': {'input_tokens': input_tokens, 'output_tokens': 1,
                              'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}}))
                self.wfile.flush()
                time.sleep(max(0.0, plan['ttft_s'] - (time.monotonic() - arrived)))
                first_token = time.monotonic()
                tool_json = json.dumps(plan['tool']['input']) if plan['tool'] else ''
                pieces = [('thinking', plan['thinking']), ('text', plan['text'] or ('Done.' if plan['final'] else '')),
                          ('tool', tool_json)]
                total_chars = sum(len(text) for _, text in pieces) or 1
                chunks = max(3, round(plan['output_tokens'] * PRODUCTION_PACING['chunks_per_output_token']))
                gap = plan['output_tokens'] / PRODUCTION_PACING['output_tokens_per_s'] / chunks
                for index, (kind, text) in enumerate(pieces):
                    if not text:
                        continue
                    start = {'thinking': {'type': 'thinking', 'thinking': '', 'signature': ''},
                             'text': {'type': 'text', 'text': ''},
                             'tool': {'type': 'tool_use', 'id': (plan['tool'] or {}).get('id'),
                                      'name': (plan['tool'] or {}).get('name'), 'input': {}}}[kind]
                    write(_sse('content_block_start', {'type': 'content_block_start', 'index': index,
                                                       'content_block': start}))
                    parts = max(1, round(chunks * len(text) / total_chars))
                    step = math.ceil(len(text) / parts)
                    for offset in range(0, len(text), step):
                        piece = text[offset:offset + step]
                        delta = {'thinking': {'type': 'thinking_delta', 'thinking': piece},
                                 'text': {'type': 'text_delta', 'text': piece},
                                 'tool': {'type': 'input_json_delta', 'partial_json': piece}}[kind]
                        write(_sse('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                           'delta': delta}))
                        self.wfile.flush()
                        time.sleep(gap)
                    if kind == 'thinking':
                        write(_sse('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                           'delta': {'type': 'signature_delta',
                                                                     'signature': _signature(plan)}}))
                    write(_sse('content_block_stop', {'type': 'content_block_stop', 'index': index}))
                write(_sse('message_delta', {'type': 'message_delta',
                                             'delta': {'stop_reason': 'tool_use' if plan['tool'] else 'end_turn',
                                                       'stop_sequence': None},
                                             'usage': {'output_tokens': plan['output_tokens']}}))
                write(_sse('message_stop', {'type': 'message_stop'}))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                with state.lock:
                    state.counters['client_disconnects'] += 1
                return
            with state.lock:
                state.counters['rounds_streamed'] += 1
                if plan['tool']:
                    state.streams[plan['tool']['id']] = (first_token, time.monotonic())
                    state.counters[f"tool:{plan['tool']['name']}"] += 1
                else:
                    state.counters['final_rounds'] += 1
    return Handler


def _signature(plan) -> str:
    return base64.b64encode(hashlib.sha256(plan['thinking'].encode()).digest() * 8).decode()


def fake_model_main(args) -> int:
    state = FakeModelState()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _fake_model_handler(state))
    server.daemon_threads = True
    Path(args.port_file).write_text(str(server.server_address[1]))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    server.serve_forever()
    return 0


# ── accounting ────────────────────────────────────────────────────────────────

def histogram_delta(before: list[dict], after: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Interval delta of ``storage_rpc_snapshot`` series keyed by (kind, operation, outcome)."""
    def key(series):
        labels = series['labels']
        return labels.get('kind', ''), labels.get('operation', ''), labels.get('outcome', '')
    old = {key(series): series for series in before}
    out = {}
    for series in after:
        previous = old.get(key(series))
        counts = list(series['counts'])
        count, total = series['count'], series['sum']
        if previous is not None:
            counts = [a - b for a, b in zip(counts, previous['counts'])]
            count -= previous['count']
            total -= previous['sum']
        if count > 0:
            out[key(series)] = {'buckets': list(series['buckets']), 'counts': counts, 'count': count, 'sum': total}
    return out


def histogram_quantile(buckets: list[float], cumulative: list[int], count: int, q: float):
    """Prometheus-style linear interpolation; ``None`` beyond the last finite bucket."""
    if count <= 0:
        return None
    rank = q * count
    lower, below = 0.0, 0
    for upper, seen in zip(buckets, cumulative):
        if seen >= rank:
            inside = seen - below
            return lower + (upper - lower) * ((rank - below) / inside if inside else 1.0)
        lower, below = upper, seen
    return None


def operation_table(delta: dict, elapsed: float) -> dict[str, dict]:
    """Per-operation rate, mean and p50/p99 in ms from a histogram delta."""
    table = {}
    for (kind, operation, outcome), series in sorted(delta.items()):
        name = f'{kind}:{operation}' + ('' if outcome == 'success' else f':{outcome}')
        last = series['buckets'][-1] * 1000

        def ms(q):
            value = histogram_quantile(series['buckets'], series['counts'], series['count'], q)
            return f'>{last:g}' if value is None else round(value * 1000, 2)
        table[name] = {'n': series['count'], 'per_s': round(series['count'] / elapsed, 3),
                       'mean_ms': round(series['sum'] / series['count'] * 1000, 2), 'p50_ms': ms(0.5),
                       'p99_ms': ms(0.99)}
    return table


def parse_prometheus_histogram(text: str, name: str) -> list[dict]:
    """``name`` series of a /metrics document in ``storage_rpc_snapshot`` form."""
    series: dict[tuple, dict] = {}
    pattern = re.compile(rf'^{re.escape(name)}_(bucket|sum|count)\{{([^}}]*)\}} (\S+)$')
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        part, raw_labels, value = match.groups()
        labels = dict(re.findall(r'(\w+)="([^"]*)"', raw_labels))
        bound = labels.pop('le', None)
        entry = series.setdefault(tuple(sorted(labels.items())),
                                  {'labels': labels, 'bounds': {}, 'sum': 0.0, 'count': 0})
        if part == 'bucket':
            if bound != '+Inf':
                entry['bounds'][float(bound)] = int(float(value))
        elif part == 'sum':
            entry['sum'] = float(value)
        else:
            entry['count'] = int(float(value))
    out = []
    for entry in series.values():
        bounds = sorted(entry['bounds'])
        out.append({'labels': entry['labels'], 'buckets': bounds, 'counts': [entry['bounds'][b] for b in bounds],
                    'sum': entry['sum'], 'count': entry['count']})
    return out


def _sample_quantiles(values: list[float]) -> dict:
    if not values:
        return {'n': 0}
    ordered = sorted(values)
    q = lambda p: ordered[min(len(ordered) - 1, int(p * (len(ordered) - 1) + 0.5))]
    return {'n': len(ordered), 'p50_ms': round(q(.5) * 1000, 1), 'p90_ms': round(q(.9) * 1000, 1),
            'p99_ms': round(q(.99) * 1000, 1), 'max_ms': round(ordered[-1] * 1000, 1)}


# Named owners of GIL time, matched as stack substrings in the production
# analysis of 2026-11-12 (``/tmp/exec-gil.folded``) so both profiles compare.
GIL_OWNERS = (
    ('project_dispatch', '(lib/project_dispatch/'),
    ('llm request encoding (lib/llm/request.py)', '(lib/llm/request.py'),
    ('llm stream parsing', '(lib/llm/'),
    ('storage client (lib/storage/)', '(lib/storage/'),
    ('turn lifecycle', '(lib/turn_lifecycle'),
    ('task runtime (lib/tasks_pkg/)', '(lib/tasks_pkg/'),
    ('tools (lib/tools/, lib/project_mod/)', '(lib/tools/'),
    ('durable worker', '(lib/durable_worker/'),
)


def gil_attribution(folded: str, *, rate_hz: float, seconds: float) -> dict:
    """Owner shares of a ``py-spy --gil --format raw`` profile.

    Each sample is the thread that held the GIL, so samples over
    ``rate_hz * seconds`` estimates the GIL's busy fraction. An owner takes the
    sample when its frame appears anywhere in the stack (first match wins, in
    ``GIL_OWNERS`` order); ``other`` keeps the rest.
    """
    owners = collections.Counter()
    leaves = collections.Counter()
    total = 0
    for line in folded.splitlines():
        stack, _, count = line.rpartition(' ')
        if not stack or not count.isdigit():
            continue
        count = int(count)
        total += count
        owner = next((name for name, frame in GIL_OWNERS if frame in stack), 'other')
        owners[owner] += count
        frames = [frame for frame in stack.split(';') if '(lib/' in frame or '(serverctl_pkg/' in frame]
        if frames:
            leaves[re.sub(r':\d+\)$', ')', frames[-1])] += count
    return {'samples': total,
            'gil_busy_fraction': round(total / (rate_hz * seconds), 3) if seconds else None,
            'owners': {name: round(n / total, 3) for name, n in owners.most_common()} if total else {},
            'top_frames': [[frame, round(n / total, 3)] for frame, n in leaves.most_common(15)] if total else []}


def _proc_sample(pid: int) -> dict:
    out = {}
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        out['cpu_s'] = (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
        status = Path(f'/proc/{pid}/status').read_text()
        out['threads'] = int(re.search(r'Threads:\s+(\d+)', status).group(1))
        out['rss_bytes'] = int(re.search(r'VmRSS:\s+(\d+)', status).group(1)) * 1024
    except (OSError, AttributeError, IndexError):
        pass
    return out


def fidelity(phase_ops: dict, production_ops: dict) -> list[dict]:
    """Per-operation rates and means side by side; production-only operations included."""
    rows = []
    for name in sorted(set(phase_ops) | set(production_ops), key=lambda n: -(production_ops.get(n, {}).get('per_s', 0))):
        load, live = phase_ops.get(name, {}), production_ops.get(name, {})
        rows.append({'operation': name, 'production_per_s': live.get('per_s', 0), 'load_per_s': load.get('per_s', 0),
                     'production_mean_ms': live.get('mean_ms'), 'load_mean_ms': load.get('mean_ms')})
    return rows


# ── parent: environment ──────────────────────────────────────────────────────

def _execution_environment(driver_env: dict[str, str], connection_file: Path) -> dict[str, str]:
    """The execution host attaches to the driver's authority; it owns no engine.

    ``TOFU_DB_*`` engine settings belong to the engine the driver launched; the
    application's deployment gate refuses them in an application process.
    """
    env = {key: value for key, value in driver_env.items() if not key.startswith('TOFU_DB_')}
    env['TOFU_STORAGE_CONNECTION_FILE'] = str(connection_file)
    env['TOFU_PROCESS_ROLE'] = 'execution'
    return env


def _driver_environment(root: Path, engine, digest: str, durability: str, backend: str) -> dict[str, str]:
    """The storage harness's scrubbed environment, with production's project layout.

    The execution host admits only the source tree it runs from, so
    ``TOFU_PROJECT_PATH`` names this code checkout, as on the production hosts.
    Runtime state still lives under ROOT: ``TOFU_DATA_DIR`` is the runtime
    base for data/ and logs/.
    """
    env = storage_harness._child_environment(root, engine, digest, durability, backend)
    env['TOFU_PROJECT_PATH'] = str(PROJECT_ROOT)
    return env


# ── driver ───────────────────────────────────────────────────────────────────

class Driver:
    def __init__(self, args, root: Path):
        self.args = args
        self.root = root
        self.storage = storage_harness.Harness(args, root)
        self.stop = threading.Event()
        self.counters = collections.Counter()
        self.lock = threading.Lock()
        self.turn_latency: list[float] = []
        self.processes: list[subprocess.Popen] = []

    # setup -------------------------------------------------------------------
    def start_storage(self) -> dict:
        """The authority as production's native storage host owns it.

        No scratch ``project_root``: the execution host attaches only to a
        document whose project and data directory are its own, which are the
        code checkout (``TOFU_PROJECT_PATH``) and ``TOFU_DATA_DIR`` under ROOT.
        """
        from lib.runtime.runtime_paths import data_root
        from lib.storage import StorageRuntime, StorageSupervisor
        from lib.storage.service import install_runtime_for_test
        supervisor = StorageSupervisor(connection_file='', backend=self.args.backend,
                                       startup_timeout=float(self.args.startup_timeout))
        runtime = StorageRuntime(supervisor, auto_restart=False)
        install_runtime_for_test(runtime)
        if self.args.backend == 'tofudb':
            storage_harness._seed_backup_generation(Path(data_root()), Path(os.environ['TOFUDB_BIN']))
        runtime.start()
        status = runtime.status()
        self.storage.runtime, self.storage.engine_pid = runtime, status.get('pid')
        return status

    def start(self) -> dict:
        status = self.start_storage()
        from lib.storage.connection_file import write_connection_document
        run = self.root / 'run'
        run.mkdir(mode=0o700, exist_ok=True)
        self.connection_file = run / 'storage-connection.json'
        document = self.storage.runtime._supervisor.connection_document(service_id=uuid.uuid4().hex)
        write_connection_document(self.connection_file, document)
        self._write_workspace()
        self.model_port = self._start_fake_model()
        self._install_model_route()
        self._start_execution_host()
        return status

    def _write_workspace(self):
        rng = random.Random(self.args.random_seed + 3)
        corpus = self.root / 'workspace' / 'corpus'
        corpus.mkdir(parents=True, exist_ok=True, mode=0o700)

        def lines(size):
            text = _text(rng, size)
            return '\n'.join(text[i:i + 100] for i in range(0, len(text), 100)) + '\n'
        # head -c reads a prefix: the blob covers far beyond the p90 result size.
        (corpus / 'blob.txt').write_text(lines(PRODUCTION_PACING['tool_result_bytes'][1] * 16))
        for index in range(_CORPUS_FILES):
            (corpus / f'file-{index:02d}.txt').write_text(
                lines(_lognormal_bytes(rng, *PRODUCTION_PACING['tool_result_bytes'])))

    def _start_fake_model(self) -> int:
        port_file = self.root / 'run' / 'fake-model.port'
        port_file.unlink(missing_ok=True)
        log = (self.root / 'run' / 'fake-model.log').open('ab')
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--fake-model',
                                    '--root', str(self.root), '--port-file', str(port_file)],
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 60
        while not port_file.exists() or not port_file.read_text():
            if process.poll() is not None or time.monotonic() > deadline:
                raise SystemExit('fake model endpoint did not start; see run/fake-model.log')
            time.sleep(0.05)
        return int(port_file.read_text())

    def _fake_model(self, path: str, payload: dict | None = None) -> dict:
        import urllib.request
        request = urllib.request.Request(f'http://127.0.0.1:{self.model_port}{path}',
                                         data=None if payload is None else json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    def _install_model_route(self):
        """One owner route: the load model on an Anthropic connection to the fake endpoint."""
        from lib.model_routing import ModelRoutingRepository, OwnerBoundary
        repository = ModelRoutingRepository()
        boundary = OwnerBoundary.create(USER_ID)
        current = repository.get(boundary)
        model_ref = {'creator_id': CREATOR_ID, 'model_id': MODEL_ID}
        capabilities = ['text', 'tools', 'thinking']
        document = {
            'contract_version': 'tofu.model-routing/v2', 'revision': current.revision,
            'creators': [{'creator_id': CREATOR_ID, 'name': 'Agent-loop load test'}],
            'models': [{**model_ref, 'display_name': 'Agent-loop load model', 'capabilities': capabilities,
                        'context_window': 1_000_000, 'quality_rank': 1,
                        # Synthetic registration facts: this model exists only in the isolated authority.
                        'registration_metadata': {'release_date': '2026-11-12', 'checked_at': '2026-11-12',
                                                  'source_urls': ['https://example.invalid/agent-loop-load']},
                        'list_pricing': {'input': 1, 'output': 2, 'currency': 'USD', 'unit': 'per_million_tokens'},
                        'inference_profile': {'reasoning': {'support': 'supported', 'wire_format': 'thinking_type',
                                                            'can_disable': True, 'supported_efforts': []}}}],
            'providers': [{'provider_id': 'agent-load', 'name': 'Agent-loop load provider', 'scope': 'owner'}],
            'provider_accesses': [{'provider_access_id': 'agent-load-access', 'provider_id': 'agent-load',
                                   'enabled': True, 'quota_policy': {}}],
            'connections': [{'connection_id': 'agent-load-anthropic', 'provider_access_id': 'agent-load-access',
                             'base_url': f'http://127.0.0.1:{self.model_port}', 'protocol': 'anthropic',
                             'enabled': True, 'priority': 0, 'extra_headers': {}}],
            'credentials': [{'credential_id': 'agent-load-identity', 'provider_access_id': 'agent-load-access',
                             'kind': 'local_identity', 'secret_reference': '', 'key_hint': '', 'enabled': True,
                             'authorization': {'connection_ids': ['agent-load-anthropic'], 'models': [model_ref]},
                             'quota_policy': {}}],
            'offerings': [{'offering_id': 'agent-load-offering', 'provider_access_id': 'agent-load-access',
                           'identity_state': 'confirmed', 'model': dict(model_ref), 'enabled': True,
                           'capabilities': capabilities, 'context_window': 1_000_000, 'priority': 0}],
            'deployments': [{'deployment_id': 'agent-load-deployment', 'offering_id': 'agent-load-offering',
                             'connection_id': 'agent-load-anthropic', 'wire_model_id': MODEL_ID, 'enabled': True,
                             'identity_confidence': 'high', 'probe_status': 'passed', 'priority': 0}],
        }
        repository.compare_and_swap(boundary, document, expected_revision=current.revision)

    def _start_execution_host(self):
        from serverctl_pkg.execution_service import ExecutionService
        self.execution = ExecutionService(str(self.root / 'project'), sys.executable)
        environment = _execution_environment(dict(os.environ), self.connection_file)
        plan = self.execution.prepare(environment)
        self.execution.ensure(plan, environment)
        deadline = time.monotonic() + float(self.args.startup_timeout)
        while True:
            record = self.execution.record()
            if record.get('state') == 'ready' and self.execution.host_matches(record):
                break
            if record.get('state') == 'failed' or time.monotonic() > deadline:
                raise SystemExit(f"execution host did not become ready: {record.get('state')} "
                                 f"{record.get('error', '')}; see project/logs/server-console.log")
            time.sleep(0.5)
        self.execution_pid = record['pid']
        from lib.tasks_pkg.command_control_runtime import CommandControls
        self.controls = CommandControls(service=self.execution)
        print(f'execution host ready pid={self.execution_pid}', flush=True)

    def close(self):
        execution = getattr(self, 'execution', None)
        if execution is not None:
            from serverctl_pkg.restart_budget import NATIVE_SERVICE_STOP_SECONDS
            try:
                execution.stop(NATIVE_SERVICE_STOP_SECONDS, retire_failed_close=True)
            except Exception as exc:
                print(f'execution host stop: {type(exc).__name__}: {exc}', flush=True)
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.storage.stop_storage()

    # load ---------------------------------------------------------------------
    def bump(self, key, n=1):
        with self.lock:
            self.counters[key] += n

    def agent(self, scale: float, index: int, principal):
        """One browser user keeping one Turn running in its own conversation."""
        from lib.conversation_sync.runtime import conversation_turn_commands
        from lib.turn_lifecycle import get_attempt
        rng = random.Random(f'{self.args.random_seed}:{scale}:{index}')
        conversation_id = f'agentload{int(scale * 10):03d}{index:04d}'
        config = {**_CHAT_CONFIG, 'model': MODEL_ID, 'projectPath': str(self.root / 'workspace')}
        first = True
        while not self.stop.is_set():
            rounds = _lognormal_bytes(rng, *PRODUCTION_PACING['rounds_per_turn'])
            nonce = uuid.UUID(int=rng.getrandbits(128)).hex
            body = {'commandId': f'load-{nonce}', 'config': dict(config),
                    'message': {'text': f'Inspect the corpus. [{MARKER} rounds={rounds} turn={nonce}]'}}
            if first:
                body['conversation'] = {'allowCreate': True, 'title': f'agent loop load {scale:g}x #{index}',
                                        'settings': {}}
            started = time.monotonic()
            try:
                outcome = conversation_turn_commands.create_turn(
                    conversation_id, USER_ID, body, request_started_at=time.time(), principal=principal)
                first = False
                attempt_id = (outcome.value.get('attempt') or {}).get('attemptId')
                if not attempt_id:
                    self.bump(f"turn_refused:{outcome.value.get('aborted') and 'aborted' or 'no_attempt'}")
                    self.stop.wait(1.0)
                    continue
                self.bump('turns_submitted')
                status = None
                # The browser learns settlement from its sync stream; this
                # client reads the attempt row once per second instead.
                while status not in _TERMINAL_ATTEMPT and not self.abandon.is_set():
                    time.sleep(1.0)
                    status = (get_attempt(attempt_id, user_id=USER_ID) or {}).get('status')
                self.bump(f'turns_{status or "unsettled"}')
                if status in _TERMINAL_ATTEMPT:
                    with self.lock:
                        self.turn_latency.append(time.monotonic() - started)
            except Exception as exc:
                self.bump(f'agent_error:{getattr(exc, "code", "") or type(exc).__name__}')
                print(f'agent {index} error: {type(exc).__name__}: {str(exc)[:300]}', flush=True)
                self.stop.wait(1.0)

    def storage_rpc(self) -> list[dict]:
        return self.controls.execution_storage_rpc_metrics(user_id=USER_ID)['histograms']

    def run_phase(self, scale: float, duration: float, out: Path) -> dict:
        from lib.api_keys._context import local_admin_context
        from lib.identity import principal_from_auth_context
        principal = principal_from_auth_context(local_admin_context(), allow_personal_owner=True)
        self.stop.clear()
        self.abandon = threading.Event()
        self.counters = collections.Counter()
        self.turn_latency = []
        self._fake_model('/control/drain', {'draining': False})
        self._fake_model('/control/stats?reset=1')
        agents = max(1, int(round(PRODUCTION_PROFILE['running_attempts'] * scale)))
        threads = [threading.Thread(target=self.agent, args=(scale, index, principal), daemon=True,
                                    name=f'agent-load-{index}') for index in range(agents)]
        rpc_before, proc_before = self.storage_rpc(), _proc_sample(self.execution_pid)
        engine_before, engine_proc_before = self.storage.engine_metrics(), self.storage.engine_proc()
        started = time.monotonic()
        for thread in threads:
            thread.start()
        profile_path = out / f'gil-{scale:g}x.folded'
        profiler = self._start_profiler(profile_path, duration)
        self.stop.wait(duration)
        elapsed = time.monotonic() - started
        rpc_after, proc_after = self.storage_rpc(), _proc_sample(self.execution_pid)
        engine_after, engine_proc_after = self.storage.engine_metrics(), self.storage.engine_proc()
        model = self._fake_model('/control/stats')
        if profiler is not None:
            profiler.wait()
        # Settle every running Turn: the next model request of each answers finally.
        self._fake_model('/control/drain', {'draining': True})
        self.stop.set()
        drain_started = time.monotonic()
        for thread in threads:
            thread.join(timeout=float(self.args.startup_timeout))
        self.abandon.set()
        drained = time.monotonic() - drain_started
        delta = histogram_delta(rpc_before, rpc_after)
        operations = operation_table(delta, elapsed)
        cpu = proc_after.get('cpu_s', 0) - proc_before.get('cpu_s', 0)
        gil = None
        if profile_path.exists():
            gil = gil_attribution(profile_path.read_text(errors='replace'), rate_hz=PY_SPY_RATE_HZ, seconds=elapsed)
        samples = model['samples']
        return {
            'scale': scale, 'agents': agents, 'elapsed_s': round(elapsed, 1), 'drain_s': round(drained, 1),
            'agent_process': {'pid': self.execution_pid, 'cpu_cores': round(cpu / elapsed, 3),
                              'threads': proc_after.get('threads'), 'rss_bytes': proc_after.get('rss_bytes')},
            'gil': gil,
            'storage_rpc_total_per_s': round(sum(s['count'] for s in delta.values()) / elapsed, 2),
            'storage_rpc': operations,
            'rounds': {'first_token_to_next_request': _sample_quantiles(samples.get('first_token_to_next_request_s', [])),
                       'last_token_to_next_request': _sample_quantiles(samples.get('last_token_to_next_request_s', [])),
                       'model_counters': model['counters']},
            'turns': {'counters': dict(self.counters), 'settled_latency': _sample_quantiles(self.turn_latency)},
            'engine': storage_harness._engine_delta(engine_before, engine_after, engine_proc_before,
                                                    engine_proc_after, elapsed, changes=0),
            '_raw': {'storage_rpc_before': rpc_before, 'storage_rpc_after': rpc_after},
        }

    def _start_profiler(self, path: Path, duration: float):
        py_spy = self.args.py_spy
        if not py_spy or not Path(py_spy).is_file():
            print('py-spy unavailable: no GIL attribution', flush=True)
            return None
        return subprocess.Popen([py_spy, 'record', '--pid', str(self.execution_pid), '--gil', '--nonblocking',
                                 '--rate', str(PY_SPY_RATE_HZ), '--duration', str(int(duration)),
                                 '--format', 'raw', '--output', str(path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _markdown(report: dict) -> str:
    lines = [f"# Agent-loop load test {report['run_id']} ({report['backend']})", '',
             f"engine sha256 `{(report.get('engine_sha256') or '-')[:16]}`, root `{report['root']}`", '']
    for phase in report['phases']:
        agent, gil = phase['agent_process'], phase.get('gil') or {}
        rounds = phase['rounds']
        lines += [f"## {phase['scale']:g}x ({phase['agents']} running attempts, {phase['elapsed_s']} s)", '',
                  f"Agent process: {agent['cpu_cores']} cores, {agent['threads']} threads; GIL busy "
                  f"{gil.get('gil_busy_fraction')}; storage RPCs {phase['storage_rpc_total_per_s']}/s", '',
                  f"round latency first token -> next request: {json.dumps(rounds['first_token_to_next_request'])}",
                  f"round latency last token -> next request: {json.dumps(rounds['last_token_to_next_request'])}",
                  f"model: {json.dumps(rounds['model_counters'])}; turns: {json.dumps(phase['turns'])}", '',
                  '| operation (Agent process) | n | per s | mean ms | p50 ms | p99 ms |', '|---|---:|---:|---:|---:|---:|']
        for name, row in sorted(phase['storage_rpc'].items(), key=lambda item: -item[1]['n']):
            lines.append(f"| {name} | {row['n']} | {row['per_s']} | {row['mean_ms']} | {row['p50_ms']} | {row['p99_ms']} |")
        if gil.get('owners'):
            lines += ['', '| GIL owner | share |', '|---|---:|']
            lines += [f'| {name} | {share} |' for name, share in gil['owners'].items()]
        engine = phase['engine']
        lines += ['', f"engine cpu {engine['cpu_cores']} cores; durability {json.dumps(engine['durability'])}"]
        if phase.get('fidelity'):
            lines += ['', '| operation | production per s | load per s | production mean ms | load mean ms |',
                      '|---|---:|---:|---:|---:|']
            lines += [f"| {row['operation']} | {row['production_per_s']} | {row['load_per_s']} | "
                      f"{row['production_mean_ms']} | {row['load_mean_ms']} |" for row in phase['fidelity']]
        lines.append('')
    return '\n'.join(lines) + '\n'


def _production_operations(args) -> tuple[dict, float] | None:
    if not args.production_metrics:
        return None
    before_path, after_path = (Path(p) for p in args.production_metrics)
    name = 'tofu_execution_storage_rpc_seconds'
    before = parse_prometheus_histogram(before_path.read_text(), name)
    after = parse_prometheus_histogram(after_path.read_text(), name)
    elapsed = after_path.stat().st_mtime - before_path.stat().st_mtime
    if args.production_elapsed:
        elapsed = float(args.production_elapsed)
    return operation_table(histogram_delta(before, after), elapsed), elapsed


def driver_main(args) -> int:
    root = Path(args.root).resolve()
    run_id = args.run_id
    out = root / 'results' / run_id
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    driver = Driver(args, root)
    report = {'run_id': run_id, 'backend': args.backend, 'root': str(root),
              'engine_sha256': os.environ.get('TOFUDB_EXPECTED_SHA256', ''), 'pacing': PRODUCTION_PACING,
              'running_attempts_1x': PRODUCTION_PROFILE['running_attempts'], 'phases': []}
    production = _production_operations(args)
    if production:
        report['production_execution_host'] = {'elapsed_s': production[1], 'storage_rpc': production[0]}
    try:
        status = driver.start()
        report['storage_status'] = {key: status.get(key) for key in ('backend', 'pid', 'port', 'state')}
        for scale in [float(s) for s in args.scales.split(',')]:
            phase = driver.run_phase(scale, args.duration, out)
            raw = phase.pop('_raw')
            (out / f'storage-rpc-{scale:g}x.json').write_text(json.dumps(raw))
            if production and scale == 1.0:
                phase['fidelity'] = fidelity(phase['storage_rpc'], production[0])
            report['phases'].append(phase)
            print(f"phase {scale:g}x: agent cpu {phase['agent_process']['cpu_cores']} cores, "
                  f"rpc {phase['storage_rpc_total_per_s']}/s, turns {phase['turns']['counters']}", flush=True)
            (out / 'report.json').write_text(json.dumps(report, indent=1))
            (out / 'report.md').write_text(_markdown(report))
    finally:
        driver.close()
    print(f"report: {out / 'report.md'}", flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', required=True, help='private working directory (created 0700)')
    parser.add_argument('--backend', choices=('tofudb', 'sqlite'), default='tofudb')
    parser.add_argument('--engine', default=os.environ.get('TOFUDB_BIN', ''),
                        help='tofu-db binary to pin (default: $TOFUDB_BIN)')
    parser.add_argument('--scales', default='1,3,5', help='multiples of production running attempts')
    parser.add_argument('--duration', type=float, default=300.0, help='seconds per scale')
    parser.add_argument('--durability', choices=('normal', 'strict', 'auto'), default='normal')
    parser.add_argument('--random-seed', type=int, default=20261112)
    parser.add_argument('--startup-timeout', type=float, default=600.0)
    parser.add_argument('--py-spy', default=shutil.which('py-spy') or '', help='py-spy binary for GIL attribution')
    parser.add_argument('--production-metrics', nargs=2, metavar=('BEFORE', 'AFTER'),
                        help='two saved production /metrics snapshots; the 1x phase is compared with their delta')
    parser.add_argument('--production-elapsed', type=float, help='seconds between the snapshots (default: mtimes)')
    parser.add_argument('--run-id', default='')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--fake-model', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--port-file', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.fake_model:
        return fake_model_main(args)
    if args.child:
        return driver_main(args)
    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    storage_harness._refuse_live_root(root)
    (root / '.tofudb-load-test').touch()
    engine, digest = None, ''
    if args.backend == 'tofudb':
        if not args.engine or not Path(args.engine).is_file():
            raise SystemExit('--engine (or $TOFUDB_BIN) must name a tofu-db binary')
        engine, digest = storage_harness._pin_engine(Path(args.engine), root)
    run_id = args.run_id or time.strftime('%Y%m%d-%H%M%S')
    command = [sys.executable, str(Path(__file__).resolve()), '--child', '--root', str(root.resolve()),
               '--backend', args.backend, '--scales', args.scales, '--duration', str(args.duration),
               '--random-seed', str(args.random_seed), '--startup-timeout', str(args.startup_timeout),
               '--py-spy', args.py_spy, '--run-id', run_id]
    if args.production_metrics:
        command += ['--production-metrics', *[str(Path(p).resolve()) for p in args.production_metrics]]
    if args.production_elapsed:
        command += ['--production-elapsed', str(args.production_elapsed)]
    environment = _driver_environment(root.resolve(), engine, digest, args.durability, args.backend)
    return subprocess.run(command, env=environment, cwd=str(root.resolve())).returncode


if __name__ == '__main__':
    sys.exit(main())
