"""Per-event persist CPU of a long task's turn events, idle and under GIL load.

Each arm runs in a fresh worker process with ``PYTHONPATH`` set to one source
tree (``--source-root`` for an exported pre-change tree, this repository by
default) and the same seeded fixture: a conversation task whose history grows
by one tool round per round (default 300 rounds), with a constant 40-tool
catalog. Every round persists, through that tree's own production code, a
request ``messages_snapshot`` (``round_evidence.build_round_evidence`` over a
deep copy of the history, the managed lane's detach,
``event_log.project_persistent_event``), a phase, three text progress frames,
tool start/result, round end and the post-tool state snapshot, each through
``turn_lifecycle.record_task_event`` with its carried row. The storage stub
encodes every request exactly as the native client does
(``v2_semantics.request_payload`` + ``tofu_db.wire._payload_bytes``) and
acknowledges it. A non-conversation task's ``checkpoint_task_partial`` runs
twice per round (provider boundary, round close) over its settled tool rounds.
``--busy`` starts CPU-bound Python threads beside the measured thread.

Before any clock counts, every arm must produce the same SHA-256 over all
request bytes (the request clock is a counter), carried task-event rows and
checkpoint texts. Clocks are calling-thread CPU (``thread_time``) and wall time
of these components only; the authority, delivery coordinator, providers and
push are excluded.

Usage: python3 benchmarks/turn_event_persist_encode.py
       [--source-root PRE_CHANGE_TREE] [--rounds 300] [--busy 0,8]
       [--seed 7] [--json]
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
_WORDS = ('alpha', 'beta', 'gamma', 'return', 'def', 'class', 'import', 'self',
          'value', 'result', 'task', 'round', '"quoted"', '\n', '\t', 'path/to/file.py')


def _text(rng, size):
    out, length = [], 0
    while length < size:
        word = rng.choice(_WORDS)
        out.append(word)
        length += len(word) + 1
    return ' '.join(out)[:size]


def _tools(rng):
    return [{'type': 'function', 'function': {
        'name': f'tool_{index}', 'description': _text(rng, 900),
        'parameters': {'type': 'object', 'properties': {
            f'arg_{arg}': {'type': 'string', 'description': _text(rng, 80)}
            for arg in range(5)}, 'required': ['arg_0']}}} for index in range(40)]


def _spin(stop):
    while not stop.is_set():
        sum(range(2000))


def run_worker(rounds, busy, seed):
    os.environ['TOFU_TURN_DELTA_RECORD_MS'] = '0'
    os.environ['TOFU_TURN_STRUCTURAL_RECORD_MS'] = str(10 ** 12)

    from lib import turn_lifecycle
    from lib.conversation_sync import settled_rows
    from lib.storage.v2_semantics import request_payload
    from lib.tasks_pkg.event_log import project_persistent_event
    from lib.tasks_pkg.manager import _sync
    from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence
    from lib.utils import fast_deepcopy
    from tofu_db.wire import _payload_bytes

    lane_detach = getattr(settled_rows, 'detach_frame_sharing_settled', None)
    shares_settled = lane_detach is not None
    # Each run starts with no recorded cadence for its attempt.
    turn_lifecycle._structural_last_fold.pop('bench-attempt', None)
    turn_lifecycle._delta_last_recorded.pop('bench-attempt', None)

    def detach(frame):
        """The managed lane's detach of an accepted frame, as this tree does it."""
        return lane_detach(frame)[0] if shares_settled else fast_deepcopy(frame)

    import uuid
    from lib import turn_activity_timeline
    from lib.tasks_pkg import round_evidence
    restore = []

    def seam(module, name, value):
        restore.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    clock = iter(range(1_700_000_000_000, 1_800_000_000_000))
    seam(turn_lifecycle, '_now_ms', lambda: next(clock))
    seam(turn_activity_timeline, '_now_ms', turn_lifecycle._now_ms)
    identities = iter(range(1, 1 << 62))
    seam(round_evidence, 'uuid4', lambda: uuid.UUID(int=next(identities)))
    wire_digest, rows_digest, checkpoint_digest = (hashlib.sha256() for _ in range(3))
    wire_bytes = [0]

    class Authority:
        revision = 0

        def command(self, operation, payload, command_id, priority='user', deadline=None):
            wire = _payload_bytes(request_payload(operation, payload, owner_id=1))
            wire_digest.update(wire)
            wire_bytes[0] += len(wire)
            carried = payload.get('task_event') or {}
            rows_digest.update(json.dumps(carried.get('event'), sort_keys=True,
                                          ensure_ascii=False).encode())
            self.revision = payload['projection_patch']['targetRevision']
            return {'applied': True, 'projection_revision': self.revision,
                    'task_event': {'sequence': carried.get('sequence')},
                    'task_event_count': 1 + len(carried.get('preceding_events') or ())}

    authority = Authority()
    seam(turn_lifecycle, '_turn_client', lambda write=False: authority)
    seam(_sync, '_upsert_task_row', lambda task, conv_id, **payload: checkpoint_digest.update(
        ((payload.get('tr_json') or '') + (payload.get('segments_json') or '')).encode()) or True)

    rng = random.Random(seed)
    tools = _tools(rng)
    task = {'id': 'bench-task', '_attemptId': 'bench-attempt', '_userId': 1,
            'convId': 'bench-conv', '_turnId': 'bench-turn', 'status': 'running',
            'content': '', 'thinking': '', 'toolRounds': [], 'model': 'glm-5.3',
            'config': {'model': 'glm-5.3'}}
    turn_lifecycle._remember_task_projection_state(
        task, attempt_id='bench-attempt', user_id=1,
        turn={'conversationId': 'bench-conv', 'turnId': 'bench-turn', 'actor': 'assistant'},
        projection={}, projection_revision=0)
    child = {'id': 'bench-child', 'status': 'running', 'content': 'working',
             'thinking': '', 'toolRounds': [], 'config': {}}
    history = [{'role': 'system', 'content': _text(rng, 16_000)},
               {'role': 'user', 'content': _text(rng, 600)}]
    samples = {'snapshot': [], 'structural': [], 'progress': [], 'checkpoint': []}
    sequence = [0]

    def persist(kind, frame, build=None):
        cpu, wall = time.thread_time_ns(), time.perf_counter_ns()
        if build is not None:
            frame = build()
        frame = detach(frame)
        durable = project_persistent_event(task['id'], frame)
        carrier = {'task_id': task['id'], 'sequence': sequence[0], 'event': durable}
        outcome = turn_lifecycle.record_task_event(task, frame, task_event=carrier)
        samples[kind].append((time.thread_time_ns() - cpu, time.perf_counter_ns() - wall))
        assert outcome == 'carried', outcome
        sequence[0] += 1

    def checkpoint():
        cpu, wall = time.thread_time_ns(), time.perf_counter_ns()
        _sync.checkpoint_task_partial(child, force=True)
        samples['checkpoint'].append((time.thread_time_ns() - cpu, time.perf_counter_ns() - wall))

    stop = threading.Event()
    spinners = [threading.Thread(target=_spin, args=(stop,), daemon=True) for _ in range(busy)]
    for spinner in spinners:
        spinner.start()
    try:
        for number in range(1, rounds + 1):
            ref = begin_round_evidence(task, task['id'], number)
            prepared = fast_deepcopy(history)
            persist('snapshot', None, lambda: build_round_evidence(
                task, history, ref=ref, kind='request', model='glm-5.3',
                prepared_messages=prepared, tools=tools))
            persist('structural', {'type': 'phase', 'phase': 'calling', 'roundNum': number})
            for _ in range(3):
                task['content'] += _text(rng, 80)
                persist('progress', {'type': 'delta', 'content': task['content'][-80:]})
            call_id = f'call_{number}'
            arguments = json.dumps({'path': f'lib/mod_{number}.py', 'end_line': rng.randint(50, 400)})
            row = {'roundNum': number, 'llmRound': number, 'toolCallId': call_id,
                   'toolName': 'read_file', 'toolArgs': arguments, 'status': 'running'}
            task['toolRounds'] = [*task['toolRounds'], row]
            child['toolRounds'] = [*child['toolRounds'], dict(row)]
            checkpoint()
            persist('structural', {'type': 'tool_start', 'toolName': 'read_file',
                                   'roundNum': number, 'toolCallId': call_id})
            output = _text(rng, rng.randint(1_000, 6_000))
            done = {**row, 'status': 'done', 'toolContent': output}
            # Round close settles finished rounds (settle_finished_tool_rounds).
            task['toolRounds'] = [*task['toolRounds'][:-1], settled_rows.settle(done)]
            child['toolRounds'] = [*child['toolRounds'][:-1], settled_rows.settle(done)]
            persist('structural', {'type': 'tool_result', 'toolName': 'read_file',
                                   'roundNum': number, 'toolCallId': call_id})
            persist('structural', {'type': 'round_end', 'roundNum': number,
                                   'reason': 'tool_calls'})
            history = [*history,
                       {'role': 'assistant', 'content': _text(rng, rng.randint(100, 600)),
                        'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                            'name': 'read_file', 'arguments': arguments}}]},
                       {'role': 'tool', 'tool_call_id': call_id, 'content': output}]
            persist('snapshot', None, lambda: build_round_evidence(
                task, history, ref=ref, kind='state', model='glm-5.3', tools=tools))
            checkpoint()
    finally:
        stop.set()
        for spinner in spinners:
            spinner.join()
        for module, name, value in reversed(restore):
            setattr(module, name, value)
        from lib.tasks_pkg.snapshot_delta import forget_projector_task
        forget_projector_task(task['id'])

    def summary(values):
        cpu = [value[0] / 1e6 for value in values]
        wall = [value[1] / 1e6 for value in values]
        return {'events': len(values), 'cpu_ms_mean': statistics.fmean(cpu),
                'cpu_ms_p50': statistics.median(cpu),
                'cpu_ms_p95': sorted(cpu)[int(len(cpu) * 0.95) - 1],
                'cpu_ms_total': sum(cpu), 'wall_ms_mean': statistics.fmean(wall)}

    late = max(1, rounds // 6)
    per_round = {'snapshot': 2, 'structural': 4, 'progress': 3, 'checkpoint': 2}
    result = {'rounds': rounds, 'busy': busy, 'seed': seed, 'sharesSettled': shares_settled,
              'wireSha256': wire_digest.hexdigest(), 'rowsSha256': rows_digest.hexdigest(),
              'checkpointSha256': checkpoint_digest.hexdigest(), 'wireBytes': wire_bytes[0]}
    for kind, values in samples.items():
        result[kind] = summary(values)
        result[kind + '_late'] = summary(values[-late * per_round[kind]:])
    events = samples['snapshot'] + samples['structural'] + samples['progress']
    result['event'] = summary(events)
    return result


def _run_arm(source_root, rounds, busy, seed):
    env = dict(os.environ, PYTHONPATH=str(source_root))
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--worker',
         '--rounds', str(rounds), '--busy', str(busy), '--seed', str(seed)],
        cwd=str(source_root), env=env, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source-root', type=Path, default=None,
                        help='exported pre-change tree compared with this one')
    parser.add_argument('--rounds', type=int, default=300)
    parser.add_argument('--busy', default='0,8')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(run_worker(args.rounds, int(args.busy), args.seed)))
        return 0
    roots = [('current', ROOT)]
    if args.source_root is not None:
        roots.insert(0, ('reference', args.source_root.resolve()))
    rows = []
    for busy in [int(item) for item in args.busy.split(',') if item]:
        for label, source_root in roots:
            rows.append({'arm': label, **_run_arm(source_root, args.rounds, busy, args.seed)})
    digests = {(row['wireSha256'], row['rowsSha256'], row['checkpointSha256']) for row in rows}
    if len(digests) != 1:
        print(json.dumps(rows, indent=2))
        raise SystemExit('arms persisted different bytes; clocks are not comparable')
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    columns = [('event', 'event'), ('snapshot', 'snapshot'), ('snapshot_late', 'snap/late'),
               ('structural', 'struct'), ('structural_late', 'struct/late'),
               ('progress', 'progress'), ('checkpoint', 'ckpt'), ('checkpoint_late', 'ckpt/late')]
    print('calling-thread CPU per event, mean ms (late = last sixth of the rounds)')
    print(f"{'arm':9s} {'busy':>4s} " + ' '.join(f'{title:>11s}' for _, title in columns))
    for row in rows:
        print(f"{row['arm']:9s} {row['busy']:4d} " + ' '.join(
            f"{row[key]['cpu_ms_mean']:11.3f}" for key, _ in columns))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
