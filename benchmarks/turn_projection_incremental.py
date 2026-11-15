"""Per-event Turn projection cost against task history size, idle and under GIL load.

Each arm runs in a fresh worker process with ``PYTHONPATH`` set to one source
tree (``--source-root`` for an exported pre-change tree, this repository by
default), so the compared trees share this driver, fixture and seed. A worker
builds a seeded synthetic task of N tool rounds and N/2 API rounds through that
tree's own producers (settle points are used only when the tree has them),
then measures ``capture_task_projection_snapshot`` + ``_task_projection`` +
``build_projection_patch`` for a fixed cycle of structural events (tool start,
tool result with a priced API round, round close, phase). ``--busy`` starts
CPU-bound Python threads beside the measured thread to emulate GIL pressure.

Each history round also carries one canonical system-intervention note, and
each measured tool round introduces another. This exercises cumulative note
identity/ordering work instead of benchmarking only note-free tool history.

Before any clock is reported, every arm of one size must produce the same
SHA-256 over all projection documents and patches: across trees and across
busy settings. Clocks are host observations of these components only; storage,
delivery queues, providers and the browser are excluded.

``--entry append`` instead drives the production producer entry
(``_events.append_event`` on a delivery lane of a registered task) through a
cycle of structural and progress frames and reports the producer thread's CPU
(``time.thread_time``) per appended frame and per structural capture. Its
digest covers every admitted retained-byte charge, projection charge,
progress-wire charge and captured projection structure, so trees must charge
exactly alike before clocks count. Persistence is stubbed at
``record_task_event(s)``; storage, push and pump threads are excluded.

Usage: python3 benchmarks/turn_projection_incremental.py
       [--source-root PRE_CHANGE_TREE] [--sizes 10,100,500] [--busy 0,8]
       [--events 6] [--seed 7] [--entry projection|append] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TOOLS = ('read_files', 'run_command', 'search_files', 'edit_file', 'write_file')
_WORDS = ('alpha', 'beta', 'gamma', 'delta', 'lambda', 'return', 'def', 'class',
          'import', 'self', 'value', 'result', 'task', 'round', '\n')
_ATTEMPT, _TASK, _MODEL, _PROVIDER = 'att-bench', 'task-bench', 'glm-5.3', 'zhipu'


def _text(rng, size):
    out, length = [], 0
    while length < size:
        word = rng.choice(_WORDS)
        out.append(word)
        length += len(word) + 1
    return ' '.join(out)[:size]


class _Producers:
    """The tree's own producer seams; settle points only where the tree has them."""

    def __init__(self):
        from lib.tasks_pkg.round_costs import stamp_round_costs
        from lib.tasks_pkg.segments import assemble_segments
        from lib.tasks_pkg.turn_trace import observe_task_trace_event
        self.stamp_round_costs = stamp_round_costs
        self.assemble_segments = assemble_segments
        self.observe = observe_task_trace_event
        try:
            from lib.tasks_pkg.orchestrator._round_checkpoint import (
                settle_finished_tool_rounds,
            )
            from lib.conversation_sync.settled_rows import settle_items
        except ImportError:
            settle_finished_tool_rounds = None
            settle_items = None
        self.settle_rounds = settle_finished_tool_rounds
        self.settle_items = settle_items

    def close_round(self, task):
        if self.settle_rounds is not None:
            self.settle_rounds(task)
        segments = self.assemble_segments(task)
        task['segments'] = (segments if self.settle_items is None else
                            self.settle_items(segments, previous=task.get('segments')))


def _start_row(rng, round_num):
    name = rng.choice(_TOOLS)
    row = {
        'roundNum': round_num, 'llmRound': round_num // 2 + 1,
        'toolCallId': f'call_{round_num:05d}', 'toolName': name,
        'toolArgs': json.dumps({'path': f'lib/mod_{round_num}.py', 'end_line': 200}),
        'query': f'{name} lib/mod_{round_num}.py', 'status': 'running',
        'source': 'native_direct', 'tStart': 1_700_000_000_000 + round_num * 1000,
        'results': None, 'attemptId': _ATTEMPT, 'taskId': _TASK,
    }
    if round_num % 3 == 0:
        row['assistantContent'] = _text(rng, 300)
        row['thinking'] = _text(rng, 500)
    return row


def _finish_row(rng, row):
    content = _text(rng, rng.randint(1500, 6000))
    row['results'] = [{
        'toolName': row['toolName'], 'title': f"{row['toolName']} ok",
        'snippet': content[:120], 'source': 'Project', 'fetched': True,
        'fetchedChars': len(content), 'output': content,
        'meta': {'lines': 200, 'truncated': False, 'tags': ['a', 'b']},
    }]
    row['toolContent'] = content
    row['status'] = 'done'
    row['tEnd'] = row['tStart'] + 900


def _bill(producers, task, index):
    usage = {
        'prompt_tokens': 20_000 + index * 300, 'completion_tokens': 400 + index,
        'prompt_tokens_details': {'cached_tokens': 18_000 + index * 290},
        '_dispatch': {'model': _MODEL, 'provider_id': _PROVIDER},
    }
    task['apiRounds'].append({'round': index, 'model': _MODEL, 'tag': 'main',
                              'provider_id': _PROVIDER, 'usage': usage})
    producers.stamp_round_costs(task, task['apiRounds'], model=_MODEL)


def _build_task(producers, size, rng):
    task = {
        'id': _TASK, '_attemptId': _ATTEMPT, 'status': 'running',
        '_turnActor': 'assistant', 'content': '', 'thinking': '',
        'content_lock': threading.Lock(), 'phase': None, 'model': _MODEL,
        'provider_id': _PROVIDER, 'toolRounds': [], 'apiRounds': [], 'config': {},
        'usage': {'prompt_tokens': 20_000 * max(1, size),
                  'completion_tokens': 400 * max(1, size)},
        '_route_snapshot': {'model': _MODEL, 'providerId': _PROVIDER, 'routeId': 'r1'},
        'modifiedFileList': [{'path': f'lib/mod_{i}.py', 'kind': 'edit'} for i in range(8)],
        'modifiedFiles': 8,
    }
    clock = 1_700_000_000_000
    for index in range(size):
        row = _start_row(rng, index + 1)
        _finish_row(rng, row)
        task['toolRounds'].append(row)
        if index % 2 == 0:
            _bill(producers, task, index // 2 + 1)
            producers.close_round(task)
        for phase in ('waiting_model', 'tool_exec'):
            clock += 500
            producers.observe(task, {'type': 'phase', 'phase': phase,
                                     'detail': f'{phase} {index}', 'roundNum': index},
                              observed_at_ms=clock)
    from lib.tasks_pkg.segments import record_injected_note
    for index in range(size):
        record_injected_note(task, llm_round=index // 2 + 1,
                             kind='system-intervention', text=f'continuation {index}')
    producers.close_round(task)
    return task, clock


def _spin(stop):
    value = 0
    while not stop.is_set():
        for number in range(10_000):
            value += number * number


def run_worker(size, events, busy, seed):
    from lib.turn_lifecycle import _task_projection, capture_task_projection_snapshot
    from lib.turn_projection_patch import build_projection_patch
    import lib.turn_lifecycle as turn_lifecycle

    rng = random.Random(seed)
    producers = _Producers()
    task, clock = _build_task(producers, size, rng)
    digest = hashlib.sha256()
    previous, revision = {}, 0
    samples = {'capture': [], 'project': [], 'patch': [], 'total': []}
    by_kind = {}
    stop = threading.Event()
    spinners = [threading.Thread(target=_spin, args=(stop,), daemon=True)
                for _ in range(busy)]
    for spinner in spinners:
        spinner.start()
    round_num = size
    try:
        for step in range(events + 1):
            round_num += 1
            row = _start_row(rng, round_num)
            cycle = []
            for kind in ('tool_start', 'tool_result', 'round_end', 'phase'):
                clock += 250
                if kind == 'tool_start':
                    from lib.tasks_pkg.segments import record_injected_note
                    record_injected_note(task, llm_round=round_num // 2 + 1,
                                         kind='system-intervention', text=f'continuation {round_num}')
                    task['toolRounds'].append(row)
                    event = {'type': kind, 'roundNum': round_num,
                             'toolCallId': row['toolCallId'], 'toolName': row['toolName']}
                elif kind == 'tool_result':
                    _finish_row(rng, row)
                    _bill(producers, task, len(task['apiRounds']) + 1)
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
                started = time.perf_counter()
                source, _charge = capture_task_projection_snapshot(task, event)
                captured = time.perf_counter()
                projection = _task_projection(source, previous, event)
                projected = time.perf_counter()
                patch = build_projection_patch(
                    previous, projection, base_revision=revision,
                    target_revision=revision + 1)
                patched = time.perf_counter()
                digest.update(json.dumps([projection, patch], ensure_ascii=False,
                                         sort_keys=True, default=str).encode('utf-8'))
                previous, revision = projection, revision + 1
                cycle.append((kind, captured - started, projected - captured,
                              patched - projected))
            if step == 0:
                continue  # warm-up cycle
            for kind, capture_s, project_s, patch_s in cycle:
                samples['capture'].append(capture_s * 1e3)
                samples['project'].append(project_s * 1e3)
                samples['patch'].append(patch_s * 1e3)
                total = (capture_s + project_s + patch_s) * 1e3
                samples['total'].append(total)
                by_kind.setdefault(kind, []).append(total)
    finally:
        stop.set()
        for spinner in spinners:
            spinner.join()
    median = statistics.median
    return {
        'size': size, 'busy': busy, 'events': len(samples['total']), 'seed': seed,
        'digest': digest.hexdigest(),
        'medianMs': {key: round(median(values), 3) for key, values in samples.items()},
        'meanMs': {key: round(statistics.fmean(values), 3) for key, values in samples.items()},
        'kindMedianMs': {key: round(median(values), 3) for key, values in by_kind.items()},
        'settlePoints': producers.settle_rounds is not None,
        'source': str(Path(turn_lifecycle.__file__).resolve().parents[1]),
        'python': sys.version.split()[0], 'pid': os.getpid(),
    }


_APPEND_CYCLE = ('tool_start', 'tool_progress', 'tool_progress', 'tool_result',
                 'delta', 'delta', 'round_end', 'phase')
_PROGRESS_KINDS = frozenset({'tool_progress', 'delta'})


def _append_frame(producers, task, kind, row, rng):
    """Advance the task as its executor would and return the frame it emits."""
    identity = {'roundNum': row['roundNum'], 'toolCallId': row['toolCallId'],
                'toolName': row['toolName']}
    if kind == 'tool_start':
        task['toolRounds'].append(row)
        return {'type': kind, **identity, 'query': row['query']}
    if kind == 'tool_progress':
        row['_partialOutput'] = row.get('_partialOutput', '') + _text(rng, 200)
        row['_partialOutputTotalChars'] = len(row['_partialOutput'])
        return {'type': kind, **identity}
    if kind == 'tool_result':
        _finish_row(rng, row)
        _bill(producers, task, len(task['apiRounds']) + 1)
        return {'type': kind, **identity, 'status': 'done', 'query': row['query'],
                'results': row['results']}
    if kind == 'delta':
        piece = _text(rng, 80)
        with task['content_lock']:
            task['content'] += piece
        return {'type': kind, 'content': piece}
    if kind == 'round_end':
        producers.close_round(task)
        return {'type': kind, 'roundNum': row['roundNum'], 'reason': 'tools'}
    return {'type': 'phase', 'phase': 'waiting_model', 'detail': 'wait'}


def _charged_structure(source):
    # Wall-clock values (phase stamps, status intervals) vary between runs;
    # their charges do not, so they stay in the charges but not in the JSON.
    return json.dumps({key: value for key, value in source.items()
                       if key not in {'content', 'thinking', 'phase', '_timingStatusHistory'}},
                      ensure_ascii=False, sort_keys=True, default=str)


def run_append_worker(size, events, busy, seed):
    """Producer CPU of ``append_event``: frame copy, capture, charge, admission."""
    from lib import turn_lifecycle
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    import lib.translate.terminal as terminal_translations

    rng = random.Random(seed)
    producers = _Producers()
    built, _clock = _build_task(producers, size, rng)
    owner = event_delivery.EventDeliveryCoordinator(workers=1, retry_delay=0, idle_timeout=30)
    admitted = []
    submit = owner.submit

    def recording_submit(lane, sequence, retained_bytes, persist, publish, **options):
        fresh = options.get('projection_identity') is None
        admitted.append((sequence, retained_bytes, options.get('projection_bytes', 0),
                         options.get('progress_wire_bytes', 0),
                         options.get('projection_source') if fresh else None))
        return submit(lane, sequence, retained_bytes, persist, publish, **options)

    owner.submit = recording_submit
    capture = turn_lifecycle.capture_task_projection_snapshot
    capture_cpu = []

    def timed_capture(*args, **kwargs):
        started = time.thread_time()
        try:
            return capture(*args, **kwargs)
        finally:
            capture_cpu.append(time.thread_time() - started)

    seams = (
        (event_delivery, '_COORDINATOR', owner),
        (turn_lifecycle, 'record_task_event', lambda task, wire, **kwargs: 'carried'),
        (turn_lifecycle, 'record_task_events',
         lambda task, entries, **kwargs: ['carried'] * len(entries)),
        (terminal_translations, 'schedule_terminal_turn_translations', lambda task: None),
        (turn_lifecycle, 'capture_task_projection_snapshot', timed_capture),
    )
    originals = [(module, name, getattr(module, name)) for module, name, _ in seams]
    for module, name, value in seams:
        setattr(module, name, value)
    task = chat_task_runtime.create(user_id=1, task_id=_TASK + '-append')
    fields = dict(built)
    fields.pop('id')
    task.update(fields, convId='conv-bench', _turnId='turn-bench', _workerStartedAt=time.time())
    samples = {kind: [] for kind in _APPEND_CYCLE}
    stop = threading.Event()
    spinners = [threading.Thread(target=_spin, args=(stop,), daemon=True)
                for _ in range(busy)]
    for spinner in spinners:
        spinner.start()
    round_num = size
    try:
        for step in range(events + 1):
            round_num += 1
            row = _start_row(rng, round_num)
            for kind in _APPEND_CYCLE:
                frame = _append_frame(producers, task, kind, row, rng)
                capture_cpu.clear()
                started = time.thread_time()
                _events.append_event(task, frame)
                spent = time.thread_time() - started
                if step:  # the first cycle warms caches and memos
                    samples[kind].append((spent * 1e3, sum(capture_cpu) * 1e3))
    finally:
        stop.set()
        for spinner in spinners:
            spinner.join()
        owner.close(10)
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])
        for module, name, value in originals:
            setattr(module, name, value)
    digest = hashlib.sha256()
    for sequence, retained, projection, progress_wire, source in admitted:
        digest.update(json.dumps([sequence, retained, projection, progress_wire]).encode())
        if source is not None:
            digest.update(_charged_structure(source).encode('utf-8'))
    median = statistics.median
    structural = [kind for kind in _APPEND_CYCLE if kind not in _PROGRESS_KINDS]
    return {
        'size': size, 'busy': busy, 'events': sum(map(len, samples.values())), 'seed': seed,
        'digest': digest.hexdigest(), 'admitted': len(admitted),
        'medianCpuMs': {
            'append': round(median([s for v in samples.values() for s, _ in v]), 3),
            'structural': round(median([s for k in structural for s, _ in samples[k]]), 3),
            'progress': round(median([s for k in _PROGRESS_KINDS for s, _ in samples[k]]), 3),
            'capture': round(median([c for k in structural for _, c in samples[k]]), 3),
        },
        'kindMedianCpuMs': {kind: [round(median([s for s, _ in values]), 3),
                                   round(median([c for _, c in values]), 3)]
                            for kind, values in samples.items()},
        'source': str(Path(turn_lifecycle.__file__).resolve().parents[1]),
        'python': sys.version.split()[0], 'pid': os.getpid(),
    }


def _run_arm(source_root, size, events, busy, seed, entry='projection'):
    env = dict(os.environ, PYTHONPATH=str(source_root))
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--worker',
         '--size', str(size), '--events', str(events), '--busy', str(busy),
         '--seed', str(seed), '--entry', entry],
        cwd=str(source_root), env=env, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source-root', type=Path, default=None,
                        help='exported pre-change tree compared against this tree')
    parser.add_argument('--sizes', default='10,100,500')
    parser.add_argument('--busy', default='0,8')
    parser.add_argument('--events', type=int, default=6)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--entry', choices=('projection', 'append'), default='projection')
    parser.add_argument('--size', type=int)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.worker:
        worker = run_append_worker if args.entry == 'append' else run_worker
        print(json.dumps(worker(args.size, args.events, int(args.busy), args.seed)))
        return 0
    roots = [('current', ROOT)]
    if args.source_root is not None:
        roots.insert(0, ('reference', args.source_root.resolve()))
    sizes = [int(item) for item in args.sizes.split(',') if item]
    busy_levels = [int(item) for item in args.busy.split(',') if item]
    rows = []
    for size in sizes:
        for busy in busy_levels:
            for label, source_root in roots:
                result = _run_arm(source_root, size, args.events, busy, args.seed,
                                  entry=args.entry)
                rows.append({'arm': label, **result})
        digests = {row['digest'] for row in rows if row['size'] == size}
        if len(digests) != 1:
            raise SystemExit(f'size {size}: arms produced different projections; '
                             'clocks are not comparable')
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if args.entry == 'append':
        print(f"{'arm':9s} {'size':>5s} {'busy':>4s} {'append':>8s} {'struct':>8s} "
              f"{'progress':>8s} {'capture':>8s}  (median producer CPU ms per frame)")
        for row in rows:
            median = row['medianCpuMs']
            print(f"{row['arm']:9s} {row['size']:5d} {row['busy']:4d} {median['append']:8.3f} "
                  f"{median['structural']:8.3f} {median['progress']:8.3f} {median['capture']:8.3f}")
        return 0
    print(f"{'arm':9s} {'size':>5s} {'busy':>4s} {'capture':>9s} {'project':>9s} "
          f"{'patch':>8s} {'total':>9s}  (median ms per structural event)")
    for row in rows:
        median = row['medianMs']
        print(f"{row['arm']:9s} {row['size']:5d} {row['busy']:4d} {median['capture']:9.3f} "
              f"{median['project']:9.3f} {median['patch']:8.3f} {median['total']:9.3f}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
