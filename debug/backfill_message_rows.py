#!/usr/bin/env python3
"""One-shot, idempotent backfill: mirror every conversation's JSONB ``messages``
array into ``conversation_messages`` rows (the Phase-5 windowed-read substrate).

Safe by construction:
  * writes ONLY the mirror table ``conversation_messages`` — never touches the
    authoritative ``conversations.messages`` blob;
  * idempotent (``backfill_conv`` is delete-then-insert under ``conv_id``), so
    re-running converges;
  * batched commits so a slow FUSE-mounted PG stays responsive and the job is
    resumable.

Usage::

    python debug/backfill_message_rows.py           # backfill all
    python debug/backfill_message_rows.py --verify   # backfill + per-conv DB parity

The read cutover flag (``TOFU_MESSAGES_ROWS_READ``) is NOT touched here — this
only populates rows. Flip reads separately, after this reports 0 parity failures.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.database import messages_rows as mr
from lib.log import get_logger

logger = get_logger(__name__)

BATCH = 100


def main(verify: bool = False) -> int:
    db = get_thread_db(DOMAIN_CHAT)
    t0 = time.time()
    rows = db.execute('SELECT id, messages FROM conversations ORDER BY id').fetchall()
    print(f'[backfill] fetched {len(rows)} conversations in {time.time()-t0:.1f}s')

    now_ms = int(time.time() * 1000)
    done = total_msgs = failures = parity_fail = 0
    for r in rows:
        cid = r['id']
        # Commit PER CONVERSATION: on PostgreSQL the first error aborts the whole
        # transaction, so a shared batch commit would roll back every conv in the
        # batch (the "current transaction is aborted" cascade). Per-conv commit +
        # rollback isolates a bad conv to itself.
        try:
            n = mr.backfill_conv(db, cid, r['messages'], now_ms=now_ms, commit=True)
            total_msgs += n
        except Exception as e:
            failures += 1
            try:
                db.rollback()
            except Exception as re:
                logger.debug('[backfill] rollback after conv=%s failed: %s',
                             (cid or '')[:12], re)
            logger.warning('[backfill] conv=%s failed: %s', (cid or '')[:12], e)
            continue
        done += 1
        if done % BATCH == 0:
            print(f'[backfill] {done}/{len(rows)} convs '
                  f'({total_msgs} rows) {time.time()-t0:.1f}s')
    print(f'[backfill] DONE: {done}/{len(rows)} convs, {total_msgs} rows, '
          f'{failures} failures, {time.time()-t0:.1f}s')

    if verify:
        tv = time.time()
        checked = 0
        vrows = db.execute('SELECT id FROM conversations ORDER BY id').fetchall()
        for r in vrows:
            v = mr.verify_conv_parity(db, r['id'])
            checked += 1
            if not v['ok']:
                parity_fail += 1
                print(f'[verify] PARITY FAIL conv={r["id"][:12]} {v}')
        print(f'[verify] checked={checked} parity_fail={parity_fail} '
              f'({time.time()-tv:.1f}s)')
    return parity_fail + failures


if __name__ == '__main__':
    sys.exit(main(verify='--verify' in sys.argv))
