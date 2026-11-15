"""Opt-in browser crash stress: real app + stub LLM streaming a long,
Coordinator-like conversation while ``scripts/browser_crash_repro/soak.py``
records the tab (heap, renderer memory, blocked frames, crash code).

WHY
---
The owner's Chrome tab dies with the sad tab 「错误代码：5」 (renderer
SIGTRAP: Chrome's own OOM/CHECK crash) while it shows the live Coordinator.
This is the isolated, repeatable half of that investigation: the same
transport, reducer and renderers as production, a transcript and sidebar of
the owner's scale, a stream faster than production so growth shows up sooner,
and the page CPU throttled 6x, the factor at which the live Coordinator
reproduced the owner's freeze shape. Every changed projection re-renders the
whole sidebar (``core/turn-projection.ts`` -> ``renderConversationList``), so
the 200 seeded rows are part of the load, not decoration. Any fix runs through
it before and after (``scripts/browser_crash_repro/README.md``).

Only the token source and ``web_search`` execution are stubbed (the seams the
hermetic e2e journeys use); turns go through the real v4 command API, durable
worker, event log, SSE multiplexer and browser renderers.

Opt in with ``TOFU_CRASH_REPRO_STRESS=1`` and ``TOFU_CRASH_REPRO_CHROME``;
results land in ``TOFU_CRASH_REPRO_OUTPUT`` (``summary.json`` per run).
"""
from __future__ import annotations

import lib.tasks_pkg.llm_fallback._transport as _fallback_transport

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOAK = ROOT / 'scripts' / 'browser_crash_repro' / 'soak.py'
DURATION_S = float(os.environ.get('TOFU_CRASH_REPRO_DURATION_S', '7200'))
# Scale of the owner's Coordinator conversation on 2026-11-09: 363 messages
# (about 180 turns), each reply a dozen tool rounds preceded by streamed prose.
SEED_TURNS = 180
ROUNDS_PER_TURN = 12
DELTAS_PER_ROUND = 120
# Faster than production (one delta per 10 ms) so growth shows up sooner.
DELTA_INTERVAL_S = 0.01
# The owner's sidebar on 2026-11-09 held 200 conversation rows; every changed
# projection re-renders all of them, so the per-event cost scales with this.
SIDEBAR_CONVERSATIONS = 200
SIDEBAR_SEED_MARKER = '[sidebar-seed]'
# At 6x CPU throttling the live Coordinator reproduced the owner's freeze shape
# (frames of 1-3 s whose slowest single script is a few hundred ms).
CPU_THROTTLE = 6
# Seeding builds the transcript as fast as the worker settles; only the measured
# phase streams at the paced rate.
_PACE = {'interval_s': 0.0}

pytestmark = [
    pytest.mark.visual,
    pytest.mark.skipif(os.environ.get('TOFU_CRASH_REPRO_STRESS') != '1',
                       reason='opt-in stress: set TOFU_CRASH_REPRO_STRESS=1 and TOFU_CRASH_REPRO_CHROME'),
]

_FRAGMENTS = [
    '## 进展 Progress\n\n', '- 检查 `lib/storage/` 与 **turn projection** 的边界；\n',
    '- Verified the reducer receipts against the snapshot cursor.\n',
    '```python\nfor row in rows:\n    total += row["ms"]\n```\n',
    '| lane | status | ms |\n|---|---|---|\n| perf | running | 120 |\n',
    'The worker reported a 409 while the attempt was settling; retrying once. ',
    '结论：当前 checkpoint 的数据与预期一致，继续下一步。\n\n',
]


def _stream_round(task, append_event, build_event, event_type, round_index):
    produced = []
    for index in range(DELTAS_PER_ROUND):
        if task.get('aborted'):
            break
        piece = _FRAGMENTS[(round_index + index) % len(_FRAGMENTS)]
        with task['content_lock']:
            task['content'] += piece
        append_event(task, build_event(event_type, content=piece))
        produced.append(piece)
        if _PACE['interval_s']:
            time.sleep(_PACE['interval_s'])
    return ''.join(produced)


def _rounds_for(body) -> int:
    for message in reversed(body.get('messages', []) if isinstance(body, dict) else []):
        if isinstance(message, dict) and message.get('role') == 'user':
            content = message.get('content')
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            return 1 if SIDEBAR_SEED_MARKER in text else ROUNDS_PER_TURN
    return ROUNDS_PER_TURN


def _stub_factory():
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager._events import append_event

    def _stub(task, body, tag='', on_tool_call_ready=None):
        rounds_per_turn = _rounds_for(body)
        done = task.get('_stress_rounds', 0)
        task['_stress_rounds'] = done + 1
        text = _stream_round(task, append_event, build_event, EventType.DELTA, done)
        usage = {'prompt_tokens': 50, 'completion_tokens': DELTAS_PER_ROUND, 'total_tokens': 50 + DELTAS_PER_ROUND}
        if done + 1 < rounds_per_turn and not task.get('aborted'):
            call = {'id': f'call_stress_{done}', 'index': 0, 'type': 'function',
                    'function': {'name': 'web_search', 'arguments': json.dumps({'query': f'stress round {done}'})}}
            if on_tool_call_ready:
                on_tool_call_ready(call)
            return {'role': 'assistant', 'content': text, 'tool_calls': [call]}, 'tool_calls', usage
        return {'role': 'assistant', 'content': text, 'tool_calls': []}, 'stop', usage

    return _stub


def _stub_search(query, user_question='', freshness='', **kwargs):
    return [{'title': f'Result for {query}', 'url': 'https://example.invalid/stress',
             'snippet': ('Synthetic evidence row. ' * 40).strip(), 'source': 'stub'}
            for _ in range(5)]


@pytest.fixture(scope='module')
def stress_app(flask_app):
    import tofu_search
    import lib.tasks_pkg.handlers.search._core as search_core
    from lib.durable_worker.runtime import ensure_workers_started, stop_workers
    from tests.support.model_routing import install_native_test_model_route

    install_native_test_model_route(owner_user_id=1, capabilities=('text', 'thinking'))
    saved = (_fallback_transport.stream_llm_response, search_core.perform_web_search,
             getattr(tofu_search, 'perform_web_search', None))
    _fallback_transport.stream_llm_response = _stub_factory()
    search_core.perform_web_search = _stub_search
    tofu_search.perform_web_search = _stub_search
    try:
        ensure_workers_started(flask_app)
        yield flask_app
    finally:
        try:
            stop_workers(flask_app)
        finally:
            _fallback_transport.stream_llm_response = saved[0]
            search_core.perform_web_search = saved[1]
            if saved[2] is not None:
                tofu_search.perform_web_search = saved[2]


class TurnDriver:
    """Send turns back to back through the public v4 command API."""

    def __init__(self, base: str, conversation_id: str, title: str = 'Crash repro stress',
                 text: str = '继续推进第 {index} 步，汇报 lane 状态。'):
        import httpx
        self.client = httpx.Client(base_url=base, timeout=60)
        self.conversation_id = conversation_id
        self.title = title
        self.text = text
        self.sent = 0
        self.busy = 0
        self.transport_errors = 0
        self.last_accepted_at = None
        self.error: BaseException | None = None
        self.stop = threading.Event()

    def send(self) -> bool:
        import httpx
        index = self.sent
        try:
            response = self.client.post(f'/api/v4/conversations/{self.conversation_id}/turns', json={
                'commandId': f'stress:{uuid.uuid4().hex}',
                'message': {'text': self.text.format(index=index), 'timestamp': int(time.time() * 1000),
                            '_msgId': f'stress-message:{uuid.uuid4().hex}'},
                'config': {'model': 'stub-model'},
                'conversation': {'allowCreate': True, 'title': self.title, 'settings': {'model': 'stub-model'},
                                 'createdAt': 1},
            })
        except httpx.TransportError:
            # A loaded server may drop or time out one request; the turn may still have
            # been accepted, which the next send observes as 409. Stopping here would
            # end the measured stream silently.
            self.transport_errors += 1
            return False
        if response.status_code == 409:
            self.busy += 1
            return False
        assert response.status_code == 200, response.text[:400]
        self.sent += 1
        self.last_accepted_at = time.time()
        return True

    def run(self, until: float, max_turns: int | None = None):
        try:
            while not self.stop.is_set() and time.time() < until and (max_turns is None or self.sent < max_turns):
                if not self.send():
                    self.stop.wait(0.5)
        except BaseException as error:  # surfaced by the test; a dead driver must not pass
            self.error = error
            raise


def _seed_sidebar(base: str) -> int:
    from concurrent.futures import ThreadPoolExecutor

    def one(index: int) -> int:
        driver = TurnDriver(base, f'stressrow{index:03d}{uuid.uuid4().hex[:6]}', title=f'侧栏会话 {index}',
                            text=SIDEBAR_SEED_MARKER + ' 第 {index} 行')
        driver.run(until=time.time() + 900, max_turns=1)
        return driver.sent

    with ThreadPoolExecutor(8) as pool:
        return sum(pool.map(one, range(SIDEBAR_CONVERSATIONS)))


@pytest.mark.timeout(int(DURATION_S) + 4 * 3600)
def test_long_streaming_conversation_does_not_crash_the_tab(stress_app, live_server, tmp_path):
    chrome = os.environ['TOFU_CRASH_REPRO_CHROME']
    output = Path(os.environ.get('TOFU_CRASH_REPRO_OUTPUT') or tmp_path) / time.strftime('stress-%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=True)
    conversation_id = f'stress{uuid.uuid4().hex[:12]}'
    driver = TurnDriver(live_server, conversation_id)

    seed_began = time.time()
    sidebar_rows = _seed_sidebar(live_server)
    driver.run(until=time.time() + 6 * 3600, max_turns=SEED_TURNS)
    seeded = {'sidebarConversations': sidebar_rows, 'turns': driver.sent, 'busyRetries': driver.busy,
              'seconds': round(time.time() - seed_began)}

    soak_command = [sys.executable, str(SOAK), '--chrome', chrome, '--url', live_server + '/',
                    '--conversation', conversation_id, '--out', str(output / 'soak'),
                    '--duration', str(DURATION_S), '--sample-every', '30', '--gc-every', '600',
                    '--profile-every', '900', '--profile-first', '240', '--profile-window', '60',
                    '--cpu-throttle', str(CPU_THROTTLE),
                    '--chrome-arg=--lang=zh-CN', '--chrome-arg=--accept-lang=zh-CN,zh']
    for extension in filter(None, os.environ.get('TOFU_CRASH_REPRO_EXTENSIONS', '').split(',')):
        soak_command += ['--extension', extension]
    _PACE['interval_s'] = DELTA_INTERVAL_S
    soak = subprocess.Popen(soak_command, stdout=(output / 'soak.out').open('w'), stderr=subprocess.STDOUT)
    measured_from = time.time()
    streaming = threading.Thread(target=driver.run, kwargs={'until': time.time() + DURATION_S + 60}, daemon=True)
    streaming.start()
    try:
        soak.wait(timeout=DURATION_S + 900)
    finally:
        driver.stop.set()
        streaming.join(timeout=120)
        if soak.poll() is None:
            soak.terminate()
            soak.wait(timeout=60)

    summary = json.loads((output / 'soak' / 'summary.json').read_text())
    record = {'conversation': conversation_id, 'seed': seeded, 'streamedTurns': driver.sent - seeded['turns'],
              'busyRetries': driver.busy, 'transportErrors': driver.transport_errors,
              'lastTurnAcceptedAtS': round(driver.last_accepted_at - measured_from) if driver.last_accepted_at else None,
              'driverError': repr(driver.error) if driver.error else None,
              'roundsPerTurn': ROUNDS_PER_TURN, 'deltasPerRound': DELTAS_PER_ROUND,
              'deltaIntervalS': DELTA_INTERVAL_S, 'cpuThrottle': CPU_THROTTLE, 'summary': summary}
    (output / 'stress.json').write_text(json.dumps(record, indent=1) + '\n')
    print(json.dumps(record, indent=1))
    assert driver.error is None, f'the turn driver died, so the stream stopped early: {driver.error!r}'
    assert not summary['crashes'], f"renderer crashed: {summary['crashes']}"
    assert summary['samples'] > 0
