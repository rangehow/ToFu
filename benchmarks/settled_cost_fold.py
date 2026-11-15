"""Per-frame settled-cost fold over a long API-round ledger, idle and under GIL load.

Each arm runs in a fresh worker process with ``PYTHONPATH`` set to one source
tree (``--source-root`` for an exported pre-change tree, this repository by
default), so the compared trees share this driver, fixture and seed. A worker
builds a synthetic running task whose ledger holds ``--rounds`` API rounds
(default 2,000) priced and settled by the tree's own ``stamp_round_costs``,
then measures the delivery-thread work of one Turn event frame:
``capture_task_projection_source`` + ``_task_projection`` +
``build_projection_patch`` (``frame``), the projection alone (``project``) and,
inside it, ``compute_api_rounds_cost`` (``fold``) and the projection's
``sanitize_api_rounds_for_persist`` (``sanitize``). Each cycle projects
``--unchanged`` frames over an unchanged ledger, then one frame after a new
round was billed and settled (``append``); the first cycle only warms memos.
``--busy`` starts CPU-bound Python threads beside the measured thread to
emulate interpreter-lock pressure.

Before any clock is reported, every arm of one ledger size must produce the
same SHA-256 over all projections and patches, across trees and busy levels.
Clocks are host observations of these components only; storage, delivery
queues, providers and the browser are excluded.

Usage: python3 benchmarks/settled_cost_fold.py [--source-root PRE_CHANGE_TREE]
       [--rounds 2000] [--cycles 5] [--unchanged 4] [--busy 0,4] [--seed 7] [--json]
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
_ATTEMPT, _TASK, _MODEL, _PROVIDER = 'att-cost-fold', 'task-cost-fold', 'glm-5.3', 'zhipu'
_TOOL_ROUNDS = 8
_KINDS = ('unchanged', 'append')
_PARTS = ('fold', 'sanitize', 'project', 'frame')


def _bill(task, index, rng, stamp_round_costs):
    usage = {
        'prompt_tokens': 20_000 + index * 300 + rng.randint(0, 999),
        'completion_tokens': 400 + rng.randint(0, 4_000),
        'prompt_tokens_details': {'cached_tokens': 18_000 + index * 290},
        '_dispatch': {'model': _MODEL, 'provider_id': _PROVIDER},
    }
    fresh = [{'round': index, 'model': _MODEL, 'tag': 'main',
              'provider_id': _PROVIDER, 'usage': usage}]
    # The tree's own settle point prices and settles the new round only.
    stamp_round_costs(task, fresh, model=_MODEL)
    task['apiRounds'].append(fresh[0])


def _build_task(rounds, rng):
    from lib.tasks_pkg.round_costs import stamp_round_costs

    task = {
        'id': _TASK, '_attemptId': _ATTEMPT, 'status': 'running',
        '_turnActor': 'assistant', 'content': 'answer so far', 'thinking': '',
        'content_lock': threading.Lock(), 'phase': None, 'model': _MODEL,
        'provider_id': _PROVIDER, 'toolRounds': [], 'apiRounds': [], 'config': {},
        'usage': {'prompt_tokens': 20_000 * rounds, 'completion_tokens': 400 * rounds},
    }
    for number in range(1, _TOOL_ROUNDS + 1):
        task['toolRounds'].append({
            'roundNum': number, 'llmRound': number, 'toolCallId': f'call_{number:05d}',
            'toolName': 'read_files', 'toolArgs': json.dumps({'path': f'lib/m{number}.py'}),
            'query': f'read_files lib/m{number}.py', 'status': 'done',
            'source': 'native_direct', 'tStart': 1_700_000_000_000 + number * 1000,
            'tEnd': 1_700_000_000_500 + number * 1000, 'attemptId': _ATTEMPT,
            'taskId': _TASK, 'toolContent': f'contents {number}',
            'results': [{'toolName': 'read_files', 'output': f'contents {number}'}],
        })
    for index in range(1, rounds + 1):
        _bill(task, index, rng, stamp_round_costs)
    return task, stamp_round_costs


def _spin(stop):
    value = 0
    while not stop.is_set():
        for number in range(10_000):
            value += number * number


def run_worker(rounds, cycles, unchanged, busy, seed):
    import lib.cost as cost_module
    import lib.turn_lifecycle as turn_lifecycle
    from lib.turn_projection_patch import build_projection_patch

    rng = random.Random(seed)
    task, stamp_round_costs = _build_task(rounds, rng)
    spent = {'fold': 0.0, 'sanitize': 0.0}
    projecting = [False]

    def timed(part, function):
        def measured(*args, **kwargs):
            if not projecting[0]:
                return function(*args, **kwargs)
            started = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                spent[part] += time.perf_counter() - started
        return measured

    # ``_task_projection`` imports the fold at call time and the sanitizer by
    # module name, so these seams time exactly the projection's own calls.
    seams = ((cost_module, 'compute_api_rounds_cost', 'fold'),
             (turn_lifecycle, 'sanitize_api_rounds_for_persist', 'sanitize'))
    originals = [(module, name, getattr(module, name)) for module, name, _ in seams]
    for module, name, part in seams:
        setattr(module, name, timed(part, getattr(module, name)))
    samples = {kind: {part: [] for part in _PARTS} for kind in _KINDS}
    digest = hashlib.sha256()
    previous, revision, clock = {}, 0, 1_700_000_100_000
    stop = threading.Event()
    spinners = [threading.Thread(target=_spin, args=(stop,), daemon=True)
                for _ in range(busy)]
    for spinner in spinners:
        spinner.start()
    try:
        for cycle in range(cycles + 1):
            for step in range(unchanged + 1):
                kind = 'append' if step == unchanged else 'unchanged'
                if kind == 'append':
                    _bill(task, len(task['apiRounds']) + 1, rng, stamp_round_costs)
                clock += 250
                event = {'type': 'phase', 'phase': 'waiting_model', 'detail': 'wait',
                         'emittedAt': clock, 'seq': revision + 1}
                spent.update(fold=0.0, sanitize=0.0)
                started = time.perf_counter()
                source = turn_lifecycle.capture_task_projection_source(task, event)
                captured = time.perf_counter()
                projecting[0] = True
                try:
                    projection = turn_lifecycle._task_projection(source, previous, event)
                finally:
                    projecting[0] = False
                projected = time.perf_counter()
                patch = build_projection_patch(previous, projection, base_revision=revision,
                                               target_revision=revision + 1)
                patched = time.perf_counter()
                digest.update(json.dumps([projection, patch], ensure_ascii=False,
                                         sort_keys=True, default=str).encode('utf-8'))
                previous, revision = projection, revision + 1
                if not cycle:
                    continue  # warm-up cycle: first fold of each memo
                values = samples[kind]
                values['fold'].append(spent['fold'] * 1e3)
                values['sanitize'].append(spent['sanitize'] * 1e3)
                values['project'].append((projected - captured) * 1e3)
                values['frame'].append((patched - started) * 1e3)
    finally:
        stop.set()
        for spinner in spinners:
            spinner.join()
        for module, name, value in originals:
            setattr(module, name, value)
    median = statistics.median
    return {
        'rounds': rounds, 'busy': busy, 'seed': seed, 'cycles': cycles,
        'unchanged': unchanged, 'digest': digest.hexdigest(),
        'frames': {kind: len(samples[kind]['frame']) for kind in _KINDS},
        'medianMs': {kind: {part: round(median(values), 4)
                            for part, values in samples[kind].items()}
                     for kind in _KINDS},
        'ledgerRounds': len(task['apiRounds']),
        'source': str(Path(turn_lifecycle.__file__).resolve().parents[1]),
        'python': sys.version.split()[0], 'pid': os.getpid(),
    }


def _run_arm(source_root, rounds, cycles, unchanged, busy, seed):
    env = dict(os.environ, PYTHONPATH=str(source_root))
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), '--worker',
         '--rounds', str(rounds), '--cycles', str(cycles), '--unchanged', str(unchanged),
         '--busy', str(busy), '--seed', str(seed)],
        cwd=str(source_root), env=env, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source-root', type=Path, default=None,
                        help='exported pre-change tree compared against this tree')
    parser.add_argument('--rounds', type=int, default=2000)
    parser.add_argument('--cycles', type=int, default=5)
    parser.add_argument('--unchanged', type=int, default=4)
    parser.add_argument('--busy', default='0,4')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(run_worker(args.rounds, args.cycles, args.unchanged,
                                    int(args.busy), args.seed)))
        return 0
    roots = [('current', ROOT)]
    if args.source_root is not None:
        roots.insert(0, ('reference', args.source_root.resolve()))
    rows = []
    for busy in [int(item) for item in args.busy.split(',') if item]:
        for label, source_root in roots:
            result = _run_arm(source_root, args.rounds, args.cycles, args.unchanged,
                              busy, args.seed)
            rows.append({'arm': label, **result})
    if len({row['digest'] for row in rows}) != 1:
        raise SystemExit('arms produced different projections; clocks are not comparable')
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'arm':9s} {'rounds':>6s} {'busy':>4s} {'frame kind':10s} {'fold':>8s} "
          f"{'sanitize':>8s} {'project':>8s} {'frame':>8s}  (median ms per frame)")
    for row in rows:
        for kind in _KINDS:
            median = row['medianMs'][kind]
            print(f"{row['arm']:9s} {row['rounds']:6d} {row['busy']:4d} {kind:10s} "
                  + ' '.join(f'{median[part]:8.3f}' for part in _PARTS))
    return 0


if __name__ == '__main__':
    sys.exit(main())
