"""Change-page projection work and serving-loop stalls on a long conversation.

Responsibility: measure, per source tree, what one conversation replay costs
the API host's interpreter: projection work per change page (node visits of
the generic public-value walk, stable-segment derivations, calling-thread
CPU) and the serving loop's stall distribution while several multiplexed
streams catch up through the production route generator.

Fixture: built once by the driver from this tree's producers and shared by
every arm. ``--turns`` Turn pairs; each assistant Turn runs ``--rounds`` tool
rounds through the production task projection (``capture_task_projection_snapshot``
+ ``_task_projection``) with lognormal tool results, and every projection
revision becomes the ``attempt.event`` change the authority stores: the
revision-to-revision ``projectionPatch`` from ``build_projection_patch``. Turn
creation is a ``turn.upsert`` carrying the settled user Turn and the pending
reply. Events are stored as encoded JSON and decoded per read, like the
storage wire.

Arms: each ``--arm NAME=SOURCE_ROOT`` runs in fresh worker processes with
``PYTHONPATH`` set to that tree (export pre-change trees with ``git archive``),
alternating order across ``--pairs``. A worker reports, for that tree:

* ``pages``: the whole log through ``ConversationSyncService.changes`` in
  500-event catch-up pages and 1-event steady-state pages over an in-memory
  repository; visits and derivations are counted in a separate pass so the
  counters do not distort the clocks.
* ``loop``: ``--streams`` concurrent reconnect catch-ups of the log's last
  ``--replay-events`` changes through
  ``routes.conversation_sync_v3._conversation_change_frames`` (multiplexed SSE
  framing, diagnosticPayload=none as the browser asks) on one asyncio loop
  whose default executor is the production ``InstrumentedThreadPoolExecutor``
  ('tofu-sync', production idle hint). ``--busy`` CPU-bound threads compete
  for the GIL and ``--resident-objects`` long-lived containers give cyclic GC
  a production-like heap. A ticker measures loop lag every 5 ms (LoopWatch's
  measure, finer grained); thread creations on the loop thread and gen-2
  collections are recorded.

Every arm must produce the same SHA-256 over the replayed frame data before
any clock is reported. Clocks are host observations of these components;
storage RPCs, HTTP transport, the browser and the execution host are excluded.

Usage:
    python3 benchmarks/conversation_change_page_stall.py --work-dir /tmp/DIR \
        --arm base=/tmp/base-tree --arm current=. [--turns 300] [--rounds 6]
        [--streams 4] [--busy 2] [--resident-objects 1000000]
        [--replay-events 3000] [--pairs 2] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONVERSATION = 'bench-long-conversation'
USER = 1
_TICK_S = 0.005
_CATCH_UP_PAGE = 500  # the route's replay page size (routes/conversation_sync_v3.py)
_EVENT_CONTRACT = 'tofu.conversation-sync.event/v1'


# --------------------------------------------------------------------------- fixture

def _turn_record(turn_id, *, ordinal, actor, status, projection, revision, attempt_id=None):
    return {
        'turnId': turn_id, 'presentationId': turn_id, 'conversationId': CONVERSATION,
        'laneId': 'main', 'parentTurnId': None, 'ordinal': ordinal, 'actor': actor,
        'kind': 'message' if actor == 'human' else 'reply', 'runId': 'bench-run',
        'status': status, 'currentAttemptId': attempt_id, 'projection': projection,
        'projectionRevision': revision, 'modelContextRevision': revision,
        'settlement': {}, 'createdAt': 1_700_000_000_000 + ordinal,
        'updatedAt': 1_700_000_000_000 + ordinal,
    }


def _lognormal_text(rng, text, median, ceiling):
    size = min(ceiling, int(rng.lognormvariate(0, 1.0) * median))
    return text(rng, max(256, size))


def build_fixture(turns: int, rounds: int, seed: int) -> list[bytes]:
    """The ordered change log of one long conversation, encoded per event."""
    import orjson
    sys.path.insert(0, str(ROOT / 'benchmarks'))
    import turn_projection_incremental as producers_module
    from lib.turn_lifecycle import _task_projection, capture_task_projection_snapshot
    from lib.turn_projection_patch import build_projection_patch

    rng = random.Random(seed)
    producers = producers_module._Producers()
    log: list[bytes] = []
    clock = 1_700_000_000_000

    def append(change_type, payload, turn_id, attempt_id=''):
        event = {'contract': _EVENT_CONTRACT, 'type': change_type,
                 'conversationId': CONVERSATION, 'syncSeq': len(log) + 1,
                 'occurredAt': clock, 'turnId': turn_id, 'payload': payload}
        if attempt_id:
            event['attemptId'] = attempt_id
        log.append(orjson.dumps(event))

    for index in range(turns):
        user_id, reply_id = f'turn-{index:04d}-user', f'turn-{index:04d}-reply'
        attempt_id = f'attempt-{index:04d}'
        prompt = producers_module._text(rng, rng.randint(80, 1200))
        attempt = {'attemptId': attempt_id, 'conversationId': CONVERSATION,
                   'turnId': reply_id, 'commandId': f'command-{index:04d}',
                   'taskId': f'task-{index:04d}', 'operation': 'create',
                   'status': 'running', 'baseProjectionRevision': 0,
                   'resumeAnchor': {'round': 0}, 'createdAt': clock,
                   'startedAt': clock, 'queueBinding': None, 'settledAt': None}
        append('turn.upsert', {
            'turns': [
                _turn_record(user_id, ordinal=2 * index, actor='human', status='completed',
                             projection={'content': prompt}, revision=1),
                _turn_record(reply_id, ordinal=2 * index + 1, actor='assistant',
                             status='pending', projection={}, revision=0,
                             attempt_id=attempt_id),
            ],
            'attempts': [attempt], 'conversationRevision': len(log) + 1,
        }, reply_id, attempt_id)
        task = {
            'id': f'task-{index:04d}', '_attemptId': attempt_id, 'status': 'running',
            '_turnActor': 'assistant', 'content': '', 'thinking': '',
            'content_lock': threading.Lock(), 'phase': None,
            'model': producers_module._MODEL, 'provider_id': producers_module._PROVIDER,
            'toolRounds': [], 'apiRounds': [], 'config': {}, 'usage': {},
            '_route_snapshot': {'model': producers_module._MODEL,
                                'providerId': producers_module._PROVIDER, 'routeId': 'r1'},
        }
        previous, revision = {}, 0
        for round_num in range(1, rounds + 1):
            row = producers_module._start_row(rng, round_num)
            for kind in ('tool_start', 'tool_result', 'round_end', 'phase'):
                clock += 250
                if kind == 'tool_start':
                    task['toolRounds'].append(row)
                    event = {'type': kind, 'roundNum': round_num,
                             'toolCallId': row['toolCallId'], 'toolName': row['toolName']}
                elif kind == 'tool_result':
                    producers_module._finish_row(rng, row)
                    content = _lognormal_text(rng, producers_module._text, 4096, 65536)
                    row['toolContent'] = content
                    row['results'][0]['output'] = content
                    producers_module._bill(producers, task, len(task['apiRounds']) + 1)
                    event = {'type': kind, 'roundNum': round_num, 'status': 'done',
                             'toolCallId': row['toolCallId'], 'toolName': row['toolName']}
                elif kind == 'round_end':
                    producers.close_round(task)
                    event = {'type': kind, 'roundNum': round_num, 'reason': 'tools'}
                else:
                    event = {'type': 'phase', 'phase': 'waiting_model', 'detail': 'wait'}
                    producers.observe(task, event, observed_at_ms=clock)
                event['emittedAt'] = clock
                event['seq'] = revision + 1
                source, _charge = capture_task_projection_snapshot(task, event)
                projection = _task_projection(source, previous, event)
                patch = build_projection_patch(previous, projection, base_revision=revision,
                                               target_revision=revision + 1)
                revision += 1
                append('attempt.event', {'event': {
                    'conversationId': CONVERSATION, 'turnId': reply_id,
                    'attemptId': attempt_id, 'seq': revision,
                    'projectionRevision': revision, 'type': 'projection_updated',
                    'payload': {'projectionPatch': patch, 'status': 'running',
                                'projectionBytes': len(orjson.dumps(projection))},
                }}, reply_id, attempt_id)
                previous = projection
    return log


def build_turn_rows(log: list[bytes]) -> list[bytes]:
    """Every Turn's final stored row, oldest first, as one encoded row each.

    Replies are settled with their final projection and an already completed
    translation verdict, so translation recovery must scan the whole history
    and recover nothing: the common shape of a long translated conversation.
    """
    import orjson
    from lib.turn_projection_patch import apply_projection_patch

    records: dict[str, dict] = {}
    projections: dict[str, dict] = {}
    revisions: dict[str, int] = {}
    for line in log:
        event = orjson.loads(line)
        if event['type'] == 'turn.upsert':
            for turn in event['payload']['turns']:
                records[turn['turnId']] = turn
        elif event['type'] == 'attempt.event':
            attempt = event['payload']['event']
            turn_id = attempt['turnId']
            projections[turn_id] = apply_projection_patch(
                projections.get(turn_id, {}), attempt['payload']['projectionPatch'])
            revisions[turn_id] = attempt['projectionRevision']
    rows = []
    for turn in sorted(records.values(), key=lambda row: row['ordinal']):
        if turn['turnId'] in projections:
            turn = {**turn, 'status': 'completed', 'currentAttemptId': None,
                    'projectionRevision': revisions[turn['turnId']],
                    'projection': {**projections[turn['turnId']],
                                   'translation': {'status': 'completed'}}}
        rows.append(orjson.dumps(turn))
    return rows


# --------------------------------------------------------------------------- worker

class _Repository:
    """Serves the encoded log like the storage wire: decoded per read."""

    def __init__(self, encoded: list[bytes]) -> None:
        import orjson
        self._loads = orjson.loads
        self.encoded = encoded
        self.decode_cpu_ns = 0

    def changes(self, conversation_id, user_id, *, after_sequence, limit):
        started = time.thread_time_ns()
        page = [self._loads(item) for item in self.encoded[after_sequence:after_sequence + limit]]
        self.decode_cpu_ns += time.thread_time_ns() - started
        head = len(self.encoded)
        return {'head': head, 'events': page,
                'hasMore': after_sequence + len(page) < head}


def _quantiles(values, scale=1.0):
    if not values:
        return {'n': 0}
    ordered = sorted(values)

    def pick(fraction):
        return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] * scale, 3)

    return {'n': len(ordered), 'p50': pick(0.50), 'p99': pick(0.99),
            'max': round(ordered[-1] * scale, 3), 'total': round(sum(ordered) * scale, 3)}


def _page_work(encoded: list[bytes], page_size: int) -> dict:
    import lib.conversation_sync.service as service_module
    import lib.turn_projection_segments as segments
    from lib.conversation_sync.service import ConversationSyncService

    def replay(repository):
        service = ConversationSyncService(repository)
        after, per_page = 0, []
        while after < len(encoded):
            decode_before = repository.decode_cpu_ns
            started = time.thread_time_ns()
            page = service.changes(CONVERSATION, USER, after_sequence=after,
                                   limit=page_size, diagnostic_payload='none')
            elapsed = time.thread_time_ns() - started
            per_page.append(elapsed - (repository.decode_cpu_ns - decode_before))
            after = int(page['events'][-1]['syncSeq'])
        return per_page

    collections_before = [stats['collections'] for stats in gc.get_stats()]
    projection_ns = replay(_Repository(encoded))
    collections = [after - before for before, after in zip(
        collections_before, (stats['collections'] for stats in gc.get_stats()))]
    # Counting pass: wrappers resolve through the module globals the walk and
    # Turn adaptation call, in every compared tree.
    counts = {'visits': 0, 'rebuilt': 0, 'derivations': 0}
    walk = segments.public_value_with_stable_segments
    derive = segments.projection_with_stable_segments

    def counted_walk(value):
        counts['visits'] += 1
        # The generic walk returns a new container for every list and
        # non-Turn mapping it visits.
        if isinstance(value, list) or (isinstance(value, dict) and not (
                'turnId' in value and 'projectionRevision' in value)):
            counts['rebuilt'] += 1
        return walk(value)

    def counted_derive(*args, **kwargs):
        counts['derivations'] += 1
        return derive(*args, **kwargs)

    patched = [(segments, 'public_value_with_stable_segments', counted_walk),
               (segments, 'projection_with_stable_segments', counted_derive)]
    if getattr(service_module, 'public_value_with_stable_segments', None) is walk:
        patched.append((service_module, 'public_value_with_stable_segments', counted_walk))
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in patched]
    for owner, name, replacement in patched:
        setattr(owner, name, replacement)
    try:
        pages = len(replay(_Repository(encoded)))
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)
    return {'page_size': page_size, 'pages': pages,
            'visits_per_page': round(counts['visits'] / pages, 1),
            'rebuilt_containers_per_page': round(counts['rebuilt'] / pages, 1),
            'visits_total': counts['visits'], 'derivations_total': counts['derivations'],
            'gc_collections_by_generation': collections,
            'projection_cpu_ms': _quantiles(projection_ns, 1e-6)}


def _spin(stop):
    value = 0
    while not stop.is_set():
        for number in range(10_000):
            value += number * number


def _loop_replay(encoded: list[bytes], streams: int, busy: int, replay_events: int) -> dict:
    import _thread
    import logging
    from lib.conversation_sync.service import ConversationSyncService
    from lib.observability import InstrumentedThreadPoolExecutor
    from lib.server.server_loop_runtime import _worker_count
    import routes.conversation_sync_v3 as sync_routes

    sync_routes._service = ConversationSyncService(_Repository(encoded))
    workers = _worker_count('TOFU_SYNC_WORKERS', os.environ, logging.getLogger(__name__))
    total = min(len(encoded), replay_events)
    after_sequence = len(encoded) - total
    observed = {'loop_ident': None, 'starts': 0, 'start_s': 0.0, 'raw': 0, 'active': False}
    thread_start = threading.Thread.start
    raw_start = _thread.start_new_thread

    def timed_start(thread):
        if observed['active'] and threading.get_ident() == observed['loop_ident']:
            started = time.perf_counter()
            try:
                return thread_start(thread)
            finally:
                observed['starts'] += 1
                observed['start_s'] += time.perf_counter() - started
        return thread_start(thread)

    def counted_raw(function, args, kwargs=None):
        if observed['active'] and threading.get_ident() == observed['loop_ident']:
            observed['raw'] += 1
        return raw_start(function, args, kwargs or {})

    collections = {'gen2_ms': [], 'all': 0}
    collection_started = {}

    def on_collection(phase, info):
        if phase == 'start':
            collection_started[info['generation']] = time.perf_counter()
            return
        collections['all'] += 1
        if info['generation'] == 2:
            collections['gen2_ms'].append(
                (time.perf_counter() - collection_started.get(2, time.perf_counter())) * 1e3)

    class Subscription:
        """The route's subscription seam: ends the replay once it has caught up."""

        def __init__(self):
            self.closed = False
            self.close_reason = ''

        async def wait(self, timeout):
            self.closed = True
            return False

    async def run_stream(index):
        digest, frames = hashlib.sha256(), 0
        frame = partial(sync_routes._multiplexed_sse_frame, index)
        started = time.perf_counter()
        async for encoded_frame in sync_routes._conversation_change_frames(
                CONVERSATION, USER, Subscription(), after_sequence=after_sequence,
                catch_up_budget=None, diagnostic_options={'diagnostic_payload': 'none'},
                frame=frame):
            frames += 1
            for line in encoded_frame.split('\n'):
                if line.startswith('data: ') and line != f'data: {index}':
                    digest.update(line.encode('utf-8'))
        return frames, digest.hexdigest(), time.perf_counter() - started

    async def main():
        loop = asyncio.get_running_loop()
        executor = InstrumentedThreadPoolExecutor(
            max_workers=workers, thread_name_prefix='tofu-sync', metric_pool='sync',
            idle_retain_threads=min(2, workers))
        loop.set_default_executor(executor)
        observed['loop_ident'] = threading.get_ident()
        lags, stop = [], asyncio.Event()

        async def tick():
            while not stop.is_set():
                started = time.perf_counter()
                await asyncio.sleep(_TICK_S)
                lags.append(time.perf_counter() - started - _TICK_S)

        ticker = asyncio.create_task(tick())
        observed['active'] = True
        started = time.perf_counter()
        try:
            results = await asyncio.gather(*(run_stream(index) for index in range(streams)))
        finally:
            observed['active'] = False
            stop.set()
            await ticker
        return results, lags, time.perf_counter() - started

    halt = threading.Event()
    spinners = [threading.Thread(target=_spin, args=(halt,), daemon=True) for _ in range(busy)]
    for spinner in spinners:
        spinner.start()
    threading.Thread.start = timed_start
    _thread.start_new_thread = counted_raw
    gc.callbacks.append(on_collection)
    try:
        results, lags, wall = asyncio.run(main())
    finally:
        gc.callbacks.remove(on_collection)
        threading.Thread.start = thread_start
        _thread.start_new_thread = raw_start
        halt.set()
        for spinner in spinners:
            spinner.join()
    digests = {digest for _frames, digest, _seconds in results}
    if len(digests) != 1 or any(frames != total for frames, _digest, _seconds in results):
        raise SystemExit(f'replay mismatch: frames={[r[0] for r in results]} total={total}')
    return {
        'streams': streams, 'busy': busy, 'sync_workers': workers,
        'frames_per_stream': total, 'frame_data_sha256': digests.pop(),
        'wall_s': round(wall, 3),
        'stream_s': _quantiles([seconds for _frames, _digest, seconds in results]),
        'lag_ms': _quantiles(lags, 1e3),
        'lags_over_100ms': sum(lag > 0.1 for lag in lags),
        'lags_over_1s': sum(lag > 1.0 for lag in lags),
        'stalled_s_over_100ms': round(sum(lag for lag in lags if lag > 0.1), 3),
        'loop_thread_starts': observed['starts'],
        'loop_thread_start_ms': round(observed['start_s'] * 1e3, 3),
        'loop_thread_raw_creations': observed['raw'],
        'gc_collections': collections['all'],
        'gc_gen2_ms': _quantiles(collections['gen2_ms']),
    }


def _sweep_work(rows: list[bytes], sweeps: int) -> dict:
    """Open-path translation recovery over the whole stored history."""
    import orjson
    import tracemalloc
    import lib.translate.terminal as terminal
    import lib.turn_lifecycle as lifecycle
    import lib.turn_projection_segments as segments

    served = {'pages': 0, 'rows': 0}

    class PagedClient:
        """``turn.sync.page`` over the encoded rows, decoded per page like the wire."""

        def query(self, operation, payload):
            if operation != 'turn.sync.page':
                raise AssertionError(f'unexpected storage operation {operation}')
            before = payload.get('before_ordinal')
            end = len(rows) if before is None else before
            start = max(0, end - int(payload['limit']))
            page = [orjson.loads(row) for row in rows[start:end]]
            served['pages'] += 1
            served['rows'] += len(page)
            return {'turns': page, 'attempts': [], 'hasMore': start > 0,
                    'syncSequence': 1, 'nextBeforeOrdinal': start if start > 0 else None}

    lifecycle._turn_client = lambda write=False: PagedClient()
    terminal._active_whole_turn_keys = lambda: set()
    spawned: list[str] = []
    terminal._spawn_whole_turn_translation = (
        lambda **kw: spawned.append(kw['turn_id']) is not None)
    config = {'autoTranslate': True, 'uiLang': 'zh'}

    def sweep():
        served.update(pages=0, rows=0)
        return terminal.sweep_conversation_translations(
            CONVERSATION, user_id=USER, config=config)

    derivations = 0
    derive = segments.projection_with_stable_segments

    def counted(*args, **kwargs):
        nonlocal derivations
        derivations += 1
        return derive(*args, **kwargs)

    segments.projection_with_stable_segments = counted
    try:
        respawned = sweep()
    finally:
        segments.projection_with_stable_segments = derive
    scanned = dict(served)
    cpu_ns, collections = [], []
    for _ in range(sweeps):
        before = [stats['collections'] for stats in gc.get_stats()]
        started = time.thread_time_ns()
        sweep()
        cpu_ns.append(time.thread_time_ns() - started)
        collections.append([after - prior for prior, after in zip(
            before, (stats['collections'] for stats in gc.get_stats()))])
    tracemalloc.start()
    sweep()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        'rows_per_sweep': scanned['rows'], 'pages_per_sweep': scanned['pages'],
        'respawned': respawned, 'spawned': spawned,
        'derivations_per_sweep': derivations,
        'cpu_ms': _quantiles(cpu_ns, 1e-6),
        'gc_collections_per_sweep': collections,
        'gen2_collections_total': sum(row[2] for row in collections),
        'peak_traced_mb': round(peak / 1e6, 1),
    }


def run_worker(fixture: Path, streams: int, busy: int, resident_objects: int,
               replay_events: int, *, sweeps: int = 0) -> dict:
    import lib
    encoded = fixture.read_bytes().splitlines()
    resident = [{'k': index, 'v': [index]} for index in range(resident_objects)]
    gc.collect()
    report = {
        'source': str(Path(lib.__file__).resolve().parents[1]),
        'pid': os.getpid(), 'python': sys.version.split()[0],
        'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'events': len(encoded), 'log_bytes': sum(len(item) for item in encoded),
        'resident_objects': len(resident),
    }
    if sweeps:
        report['sweep'] = _sweep_work(encoded, sweeps)
    else:
        report['pages'] = [_page_work(encoded, size) for size in (_CATCH_UP_PAGE, 1)]
        report['loop'] = _loop_replay(encoded, streams, busy, replay_events)
    return report


# --------------------------------------------------------------------------- driver

def _child_environment(work: Path, source_root: Path) -> dict[str, str]:
    keep = ('PATH', 'LANG', 'LC_ALL', 'TZ')
    env = {key: os.environ[key] for key in keep if key in os.environ}
    for name in ('home', 'tmp', 'data'):
        (work / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    env.update({'HOME': str(work / 'home'), 'TMPDIR': str(work / 'tmp'),
                'TOFU_DATA_DIR': str(work / 'data'), 'PYTHONPATH': str(source_root),
                'PYTHONNOUSERSITE': '1', 'PYTHONUNBUFFERED': '1'})
    return env


def _run_arm(name, source_root, fixture, args):
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', str(fixture),
               '--streams', str(args.streams), '--busy', str(args.busy),
               '--resident-objects', str(args.resident_objects),
               '--replay-events', str(args.replay_events), '--sweeps', str(args.sweeps)]
    completed = subprocess.run(command, env=_child_environment(args.work_dir / 'env', source_root),
                               cwd=str(args.work_dir), capture_output=True, text=True,
                               timeout=3600)
    if completed.returncode != 0:
        raise SystemExit(f'arm {name} failed:\n{completed.stderr[-4000:]}')
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    if Path(report['source']) != source_root:
        raise SystemExit(f'arm {name} imported {report["source"]}, not {source_root}')
    report['arm'] = name
    return report


def _sweep_summary(report):
    sweep = report['sweep']
    return {
        'arm': report['arm'], 'pid': report['pid'],
        'rows_per_sweep': sweep['rows_per_sweep'],
        'derivations_per_sweep': sweep['derivations_per_sweep'],
        'cpu_ms_p50': sweep['cpu_ms']['p50'], 'cpu_ms_max': sweep['cpu_ms']['max'],
        'gc_collections_per_sweep': sweep['gc_collections_per_sweep'],
        'gen2_collections_total': sweep['gen2_collections_total'],
        'peak_traced_mb': sweep['peak_traced_mb'],
    }


def _summary(report):
    if 'sweep' in report:
        return _sweep_summary(report)
    loop, pages = report['loop'], {row['page_size']: row for row in report['pages']}
    return {
        'arm': report['arm'], 'pid': report['pid'],
        'catch_up_visits_per_page': pages[_CATCH_UP_PAGE]['visits_per_page'],
        'catch_up_rebuilt_per_page': pages[_CATCH_UP_PAGE]['rebuilt_containers_per_page'],
        'catch_up_gc_by_generation': pages[_CATCH_UP_PAGE]['gc_collections_by_generation'],
        'catch_up_projection_ms_p50': pages[_CATCH_UP_PAGE]['projection_cpu_ms']['p50'],
        'catch_up_projection_ms_max': pages[_CATCH_UP_PAGE]['projection_cpu_ms']['max'],
        'steady_visits_per_event': pages[1]['visits_per_page'],
        'steady_projection_ms_p99': pages[1]['projection_cpu_ms']['p99'],
        'derivations': pages[_CATCH_UP_PAGE]['derivations_total'],
        'loop_lag_ms_p99': loop['lag_ms']['p99'], 'loop_lag_ms_max': loop['lag_ms']['max'],
        'lags_over_100ms': loop['lags_over_100ms'], 'stalled_s': loop['stalled_s_over_100ms'],
        'loop_thread_starts': loop['loop_thread_starts'],
        'loop_thread_start_ms': loop['loop_thread_start_ms'],
        'gc_gen2': loop['gc_gen2_ms']['n'], 'gc_gen2_ms_max': loop['gc_gen2_ms'].get('max'),
        'replay_wall_s': loop['wall_s'],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--arm', action='append', default=[], metavar='NAME=SOURCE_ROOT')
    parser.add_argument('--turns', type=int, default=300)
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--streams', type=int, default=4)
    parser.add_argument('--busy', type=int, default=2)
    parser.add_argument('--resident-objects', type=int, default=1_000_000)
    parser.add_argument('--replay-events', type=int, default=3_000)
    parser.add_argument('--sweeps', type=int, default=0,
                        help='measure N translation-recovery sweeps instead of replay')
    parser.add_argument('--pairs', type=int, default=2)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if not (1 <= args.streams <= 64 and 0 <= args.busy <= 64
            and 0 <= args.resident_objects <= 50_000_000
            and 1 <= args.replay_events <= 1_000_000 and 0 <= args.sweeps <= 100):
        parser.error('bounded --streams/--busy/--resident-objects/--replay-events/--sweeps required')
    if args.worker is not None:
        print(json.dumps(run_worker(args.worker, args.streams, args.busy,
                                    args.resident_objects, args.replay_events,
                                    sweeps=args.sweeps)))
        return 0
    if not (1 <= args.turns <= 5000 and 1 <= args.rounds <= 200 and 1 <= args.pairs <= 10):
        parser.error('bounded --turns/--rounds/--pairs required')
    if args.work_dir is None or not args.arm:
        parser.error('--work-dir and at least one --arm are required')
    arms = []
    for entry in args.arm:
        name, _, source = entry.partition('=')
        source_root = Path(source or '.').resolve()
        if not name or not (source_root / 'lib' / '__init__.py').is_file():
            parser.error(f'invalid --arm {entry!r}')
        arms.append((name, source_root))
    args.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fixture = args.work_dir / f'fixture-t{args.turns}-r{args.rounds}-s{args.seed}.jsonl'
    if not fixture.is_file():
        fixture.write_bytes(b'\n'.join(build_fixture(args.turns, args.rounds, args.seed)))
    if args.sweeps:
        turn_rows = fixture.with_name('turns-' + fixture.name)
        if not turn_rows.is_file():
            turn_rows.write_bytes(b'\n'.join(build_turn_rows(fixture.read_bytes().splitlines())))
        fixture = turn_rows
    reports = []
    for pair in range(args.pairs):
        order = arms if pair % 2 == 0 else list(reversed(arms))
        for name, source_root in order:
            report = _run_arm(name, source_root, fixture, args)
            report['pair'] = pair
            reports.append(report)
    if args.sweeps:
        outcomes = {(report['sweep']['rows_per_sweep'], tuple(report['sweep']['spawned']))
                    for report in reports}
        if len(outcomes) != 1:
            raise SystemExit(f'arms swept differently: {outcomes}; clocks are not comparable')
    elif len({report['loop']['frame_data_sha256'] for report in reports}) != 1:
        raise SystemExit('arms replayed different frame data; clocks are not comparable')
    output = {'fixture': str(fixture), 'fixture_sha256': reports[0]['fixture_sha256'],
              'events': reports[0]['events'], 'log_bytes': reports[0]['log_bytes'],
              'turns': args.turns, 'rounds': args.rounds, 'seed': args.seed,
              'summary': [_summary(report) for report in reports]}
    if args.json:
        output['reports'] = reports
    print(json.dumps(output, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
