#!/usr/bin/env python3
"""Measure — not assert — where time goes in a cold first-open of an old
conversation, and prove the reconcile UPDATE+commit (FUSE fsync) left the GET
read path.

Method
------
Seed a LARGE conversation (N settled turns) whose tail is a ghost empty
assistant turn (so reconcile has real work → the changed=True branch, the
worst case for the read path). Then time, on the SAME row, phase-by-phase:

  A. OLD read path  = _reconcile_conv_on_get_blocking
       (compute verdict + SYNCHRONOUS _persist_reconcile: UPDATE+commit+fsync)
  B. NEW read path  = _reconcile_conv_served_readonly
       (compute verdict ONLY; persist is deferred off-request)

and break the shared cost into: row SELECT+json blob deserialize, reconcile
compute, persist(write+commit+fsync), rehydrate. The delta (A-B) is exactly the
blocking write we moved off the request. Whatever remains in B is the residual
dominant cost of a cold open — reported honestly.

Run from the MAIN checkout (bootstrapped DB):
    python -B debug/bench_get_conv_readpath.py [num_turns]
"""

import json as _json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

N_TURNS = int(sys.argv[1]) if len(sys.argv) > 1 else 400


def _mk_big_conv(n_turns):
    """A realistic long history: n_turns exchanges, each assistant turn carrying
    thinking + a couple tool rounds (the heavy JSON), then a trailing GHOST
    empty assistant so reconcile has to act."""
    msgs = []
    big_tool_out = 'x' * 1200
    for i in range(n_turns):
        msgs.append({'role': 'user', 'content': f'question number {i} ' + 'q' * 40,
                     'timestamp': i * 2})
        msgs.append({
            'role': 'assistant',
            'content': f'answer {i} ' + 'a' * 300,
            'thinking': 'reasoning ' * 40,
            'finishReason': 'stop',
            'timestamp': i * 2 + 1,
            'toolRounds': [
                {'tool': 'read_files', 'args': {'p': f'/f/{i}'}, 'output': big_tool_out},
                {'tool': 'grep_search', 'args': {'q': str(i)}, 'output': big_tool_out},
            ],
        })
    # trailing ghost empty assistant (reconcile target)
    msgs.append({'role': 'user', 'content': 'last q', 'timestamp': n_turns * 2})
    msgs.append({'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
                 'timestamp': n_turns * 2 + 1})
    return msgs


def _load_rc():
    """Import routes.conversations WITHOUT triggering routes/__init__.py (which
    pulls routes/push.py's @websocket decorator, unavailable under bare quart)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'routes', 'conversations.py')
    # Register a stub 'routes' package so 'from . import ...' relative imports resolve
    import types
    if 'routes' not in sys.modules:
        pkg = types.ModuleType('routes')
        pkg.__path__ = [os.path.dirname(path)]
        sys.modules['routes'] = pkg
    spec = importlib.util.spec_from_file_location('routes.conversations', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['routes.conversations'] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    rc = _load_rc()
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert

    conv_id = f'bench-getconv-{uuid.uuid4().hex[:8]}'
    db = get_thread_db(DOMAIN_CHAT)
    msgs = _mk_big_conv(N_TURNS)
    blob = json_dumps_pg(msgs)
    now = int(time.time() * 1000)
    print(f'[seed] conv={conv_id} turns={N_TURNS} messages={len(msgs)} '
          f'blob={len(blob)/1024:.0f} KiB')
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'bench', 'messages': blob,
        'msg_count': len(msgs), 'created_at': now, 'updated_at': now,
        'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()

    def _fetch_row():
        return db.execute(
            'SELECT id, title, messages, created_at, updated_at, settings, rev '
            'FROM conversations WHERE id=? AND user_id=1', (conv_id,)).fetchone()

    try:
        # ── phase: SELECT + blob deserialize (shared by both paths) ──
        t0 = time.perf_counter()
        r = _fetch_row()
        raw = r['messages']
        t_select = time.perf_counter() - t0
        t0 = time.perf_counter()
        _deser = _json.loads(raw) if isinstance(raw, str) else raw
        t_deser = time.perf_counter() - t0

        # ── phase: reconcile compute (read-only verdict) ──
        t0 = time.perf_counter()
        cleaned, changed, sd = rc._compute_reconcile(conv_id, r)
        t_compute = time.perf_counter() - t0
        assert changed, 'expected reconcile to act on the ghost tail'

        # ── phase: persist (UPDATE + commit + FUSE fsync) — the moved write ──
        # measure it in isolation, repeated, to get a stable fsync cost.
        persist_samples = []
        for _ in range(5):
            r2 = _fetch_row()
            c2, ch2, s2 = rc._compute_reconcile(conv_id, r2)
            # re-seed the ghost so each persist has identical work
            if not ch2:
                db.execute('UPDATE conversations SET messages=?, settings=? '
                           'WHERE id=? AND user_id=1', (blob, '{}', conv_id))
                db.commit()
                r2 = _fetch_row(); c2, ch2, s2 = rc._compute_reconcile(conv_id, r2)
            t0 = time.perf_counter()
            rc._persist_reconcile(db, conv_id, c2, s2)
            persist_samples.append(time.perf_counter() - t0)
        t_persist = sorted(persist_samples)[len(persist_samples)//2]  # median

        # ── phase: rehydrate (shared) ──
        d = {'messages': list(cleaned)}
        t0 = time.perf_counter()
        try:
            rc._rehydrate_segments_from_task_results(db, conv_id, d['messages'])
        except Exception as e:
            print('  (rehydrate skipped:', e, ')')
        t_rehydrate = time.perf_counter() - t0

        # ── end-to-end: OLD (sync persist) vs NEW (read-only) ──
        # reset ghost first
        db.execute('UPDATE conversations SET messages=?, settings=? WHERE id=? AND user_id=1',
                   (blob, '{}', conv_id)); db.commit()
        r3 = _fetch_row()
        t0 = time.perf_counter()
        rc._reconcile_conv_on_get_blocking(db, conv_id, r3)   # OLD: compute+persist inline
        t_old = time.perf_counter() - t0

        db.execute('UPDATE conversations SET messages=?, settings=? WHERE id=? AND user_id=1',
                   (blob, '{}', conv_id)); db.commit()
        r4 = _fetch_row()
        t0 = time.perf_counter()
        rc._reconcile_conv_served_readonly(db, conv_id, r4)   # NEW: read-only
        t_new = time.perf_counter() - t0

        ms = lambda s: f'{s*1000:.2f} ms'
        print('\n── PER-PHASE (shared read cost) ──')
        print(f'  SELECT row (blob fetch)      {ms(t_select)}')
        print(f'  JSON deserialize blob        {ms(t_deser)}')
        print(f'  reconcile compute (verdict)  {ms(t_compute)}')
        print(f'  rehydrate segments           {ms(t_rehydrate)}')
        print(f'  persist UPDATE+commit+fsync  {ms(t_persist)}   <-- MOVED OFF REQUEST')
        print('\n── END-TO-END read path ──')
        print(f'  OLD (compute + SYNC persist) {ms(t_old)}')
        print(f'  NEW (read-only, no write)    {ms(t_new)}')
        saved = t_old - t_new
        print(f'  delta removed from request   {ms(saved)}'
              f'  ({saved/t_old*100:.0f}% of old read path)' if t_old > 0 else '')
        print('\n── residual dominant cost of the NEW cold-open read ──')
        parts = {'SELECT': t_select, 'deserialize': t_deser,
                 'reconcile': t_compute, 'rehydrate': t_rehydrate}
        dom = max(parts, key=parts.get)
        print(f'  largest remaining phase: {dom} ({ms(parts[dom])})')
        print(f'  → the blocking write is gone; the residual is dominated by '
              f'{dom} of a {len(blob)/1024:.0f} KiB single-blob full-history load.')

        # ── NORMALIZED + WINDOWED read (the root-cause fix) ──────────────
        # Backfill the row store, then time a tail-window read. This is what
        # the read cutover serves; its cost should be CONSTANT in the window,
        # not linear in history — measured here against the blob path above.
        try:
            from lib.database.messages_rows import backfill_conv, load_message_window
            backfill_conv(db, conv_id, blob, now_ms=now, commit=True)
            WIN = 60
            win_samples = []
            for _ in range(5):
                t0 = time.perf_counter()
                w = load_message_window(db, conv_id, limit=WIN)
                win_samples.append(time.perf_counter() - t0)
            t_win = sorted(win_samples)[len(win_samples)//2]
            print('\n── NORMALIZED + WINDOWED read (tail %d of %d msgs) ──' % (WIN, len(msgs)))
            print(f'  load_message_window(tail={WIN})  {ms(t_win)}'
                  f'   (returned {len(w["messages"])}, total {w["totalCount"]}, hasMore={w["hasMore"]})')
            print(f'  vs single-blob NEW read-only     {ms(t_new)}')
            if t_new > 0:
                print(f'  windowed is {t_new/t_win:.1f}x cheaper than full-blob read on this size;')
            print(f'  → windowed cost is O(window)={WIN} rows, INDEPENDENT of the '
                  f'{len(msgs)}-msg / {len(blob)/1024:.0f} KiB history length.')
        except Exception as _we:
            print('\n(normalized+windowed branch skipped:', _we, ')')
    finally:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
        db.commit()


if __name__ == '__main__':
    main()
