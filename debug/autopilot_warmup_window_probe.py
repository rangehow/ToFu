#!/usr/bin/env python3
"""Standalone reproduction of the autopilot warm-up silent window.

Proves — from REAL `task_events` — that the VU bubble sits on the bare
placeholder for seconds because NO phase is emitted between
`autopilot_vu_start` and the sub-task's first orchestrator phase
(`llm_thinking` / `waiting_model`).

For each recent autopilot run it prints:
  • the gap (seconds) from `autopilot_vu_start` to the first forwarded
    `autopilot_vu_event` carrying an inner `phase`, and
  • what that first phase was.

A large gap with the first phase being `llm_thinking` (NOT an autopilot
setup phase) is the bug: the pre-stream window is silent. After the fix
(lib/tasks_pkg/autopilot.py `_emit_vu_setup_phase`) the first forwarded
phase in that window is a `working` phase with an attributed Chinese detail
("整理对话上下文…" / "核对助手回答…").

Usage:  python3 debug/autopilot_warmup_window_probe.py
Skips cleanly (exit 0) when the chat DB is unavailable.
"""
from __future__ import annotations

import json
import sys


def _payload(row):
    p = row['payload'] if isinstance(row, dict) else row[3]
    if isinstance(p, (bytes, bytearray)):
        p = p.decode('utf-8', 'replace')
    try:
        return json.loads(p) if p else {}
    except Exception:
        return {}


def main() -> int:
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
    except Exception as e:  # pragma: no cover - env-dependent
        print(f'SKIP: chat DB unavailable: {e}')
        return 0

    tid_rows = db.execute(
        "SELECT DISTINCT task_id FROM task_events WHERE type='autopilot_vu_start' "
        "ORDER BY task_id DESC LIMIT 15"
    ).fetchall()
    if not tid_rows:
        print('SKIP: no autopilot_vu_start events found')
        return 0

    print(f'{"task":10} {"events":>7} {"silent_gap_s":>13}  first_phase')
    print('-' * 60)
    worst = 0.0
    for tr in tid_rows:
        tid = tr['task_id'] if isinstance(tr, dict) else tr[0]
        rows = db.execute(
            "SELECT event_id, ts_ms, type, payload FROM task_events "
            "WHERE task_id=%s ORDER BY event_id",
            (tid,),
        ).fetchall()
        start_ts = start_eid = None
        for r in rows:
            typ = r['type'] if isinstance(r, dict) else r[2]
            if typ == 'autopilot_vu_start':
                start_ts = r['ts_ms'] if isinstance(r, dict) else r[1]
                start_eid = r['event_id'] if isinstance(r, dict) else r[0]
                break
        if start_eid is None:
            continue
        first_phase = None
        for r in rows:
            eid = r['event_id'] if isinstance(r, dict) else r[0]
            typ = r['type'] if isinstance(r, dict) else r[2]
            if eid <= start_eid or typ != 'autopilot_vu_event':
                continue
            inner = _payload(r).get('inner', {})
            if inner.get('type') == 'phase':
                ts = r['ts_ms'] if isinstance(r, dict) else r[1]
                first_phase = ((ts - start_ts) / 1000.0,
                               inner.get('phase'),
                               (inner.get('detail') or '')[:32])
                break
        if first_phase:
            worst = max(worst, first_phase[0])
            print(f'{tid[:8]:10} {len(rows):>7} {first_phase[0]:>13.1f}  '
                  f'{first_phase[1]} :: {first_phase[2]}')
    print('-' * 60)
    print(f'worst silent gap: {worst:.1f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
