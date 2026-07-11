#!/usr/bin/env python3
"""Benchmark the event-log fold vs the 5s task_results checkpoint.

Answers the empirical question the owner (rightly) demanded numbers for:
"is closing the cold-replay window by folding the per-delta task_events log
(instead of reading the lossy 5s task_results checkpoint) affordable?"

Two costs are measured against a REAL DB:

  A. STEADY-STATE per-delta persist cost — ``append_persistent_event`` is
     ALREADY called on every delta today (event_log.py docstring: "No
     in-memory buffering, no coalescing"). We measure it to show the write
     amplification I previously dismissed is the CURRENT cost, not a new one.

  B. COLD-REPLAY fold-read cost — reading N delta rows and concatenating
     their ``content`` (respecting ``delta_reset``) to reconstruct the exact
     text the client saw. Compared against the single-row task_results read
     the current cold path does. This is the ONLY new work the fix adds, and
     it happens once per cold reconnect (a rare event), not per delta.

Run:  TOFU_DB_PATH=/tmp/bench_evlog.db python debug/bench_event_log_fold.py
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _pctl(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def main():
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('bench_event_log_fold')

    from lib.tasks_pkg.event_log import append_persistent_event, read_events
    from lib.database import DOMAIN_CHAT, get_thread_db

    # Realistic turn sizes: a delta is ~1-8 tokens ≈ a few chars to ~40 chars.
    # A long turn = a few thousand deltas. Test a spread.
    for n_deltas in (200, 1000, 3000):
        task_id = f'bench-{uuid.uuid4().hex[:10]}'
        # ── A. per-delta persist cost ──
        persist_ts = []
        acc = 0
        for i in range(n_deltas):
            chunk = 'lorem ipsum dolor ' * (1 + (i % 3))  # 18-54 chars
            acc += len(chunk)
            ev = {'type': 'delta', 'content': chunk}
            t0 = time.perf_counter()
            append_persistent_event(task_id, i, ev)
            persist_ts.append((time.perf_counter() - t0) * 1000.0)
        # a terminal done row
        append_persistent_event(task_id, n_deltas, {'type': 'done', 'finishReason': 'stop'})

        # ── B. cold-replay fold-read cost ──
        fold_ts = []
        folded_len = 0
        for _ in range(20):
            t0 = time.perf_counter()
            evs = read_events(task_id, since_event_id=None)
            content = []
            for e in evs:
                p = e['payload']
                t = p.get('type')
                if t == 'delta':
                    if p.get('content'):
                        content.append(p['content'])
                elif t == 'delta_reset':
                    content.clear()
            folded = ''.join(content)
            fold_ts.append((time.perf_counter() - t0) * 1000.0)
            folded_len = len(folded)

        # ── Compare: single-row task_results read (what cold path does now) ──
        db = get_thread_db(DOMAIN_CHAT)
        # seed a task_results row so the comparison read hits a real row
        try:
            from lib.database._core_schema import TASK_RESULTS, upsert
            upsert(db, TASK_RESULTS,
                   {'task_id': task_id, 'conv_id': 'c', 'content': 'x' * acc,
                    'thinking': '', 'status': 'running'},
                   conflict_cols=['task_id'],
                   insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'status'],
                   update_cols=['content'], commit=True, retry=False)
        except Exception as e:
            print('  (task_results seed skipped:', e, ')')
        ckpt_ts = []
        for _ in range(20):
            t0 = time.perf_counter()
            db.execute('SELECT content,thinking,status FROM task_results WHERE task_id=?',
                       (task_id,)).fetchone()
            ckpt_ts.append((time.perf_counter() - t0) * 1000.0)

        print(f'\n=== {n_deltas} deltas (reconstructed {folded_len} chars) ===')
        print(f'  A. per-delta persist : mean={sum(persist_ts)/len(persist_ts):.3f}ms  '
              f'p50={_pctl(persist_ts,0.5):.3f}  p99={_pctl(persist_ts,0.99):.3f}  '
              f'total={sum(persist_ts):.1f}ms  (ALREADY PAID today)')
        print(f'  B. cold fold-read    : mean={sum(fold_ts)/len(fold_ts):.3f}ms  '
              f'p50={_pctl(fold_ts,0.5):.3f}  p99={_pctl(fold_ts,0.99):.3f}  '
              f'(NEW; once per cold reconnect)')
        print(f'  C. 5s-ckpt read (now): mean={sum(ckpt_ts)/len(ckpt_ts):.3f}ms  '
              f'p50={_pctl(ckpt_ts,0.5):.3f}  (current cold path, LOSSY)')
        print(f'  Δ fold vs ckpt       : +{(sum(fold_ts)/len(fold_ts)) - (sum(ckpt_ts)/len(ckpt_ts)):.3f}ms '
              f'per cold reconnect')

        # cleanup
        try:
            from lib.database import db_execute_with_retry
            db_execute_with_retry(db, 'DELETE FROM task_events WHERE task_id=?', (task_id,))
            db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))
            db.commit()
        except Exception as e:
            print('  (cleanup skipped:', e, ')')

    print('\nInterpretation:')
    print('  A is the write cost ALREADY incurred per delta today (not new).')
    print('  B is the ONLY new work, incurred once per COLD reconnect (rare).')
    print('  If B is within a few ms and comparable to C, folding the log closes')
    print('  the cold-replay window at negligible cost → the 5 state-site')
    print('  keep-longer belts become provably unnecessary for cold replay.')


if __name__ == '__main__':
    main()
