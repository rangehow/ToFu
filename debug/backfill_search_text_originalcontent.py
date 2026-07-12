"""One-shot idempotent backfill: re-index search_text to include originalContent.

Existing conversations already carry a non-empty ``search_text`` computed by an
older ``build_search_text`` that did NOT index ``originalContent`` (the user's
pre-auto-translation text). Those rows are skipped by the newly-added-column
migration (``WHERE search_text = ''``), so they stay unsearchable by the words
the user actually typed until their next save.

This script recomputes ``search_text`` (and, on PG, ``search_tsv``) for every
conversation from the authoritative ``messages`` JSON. It is non-destructive
(a derived index column) and idempotent (recomputing yields the same value a
future save would). Only rows whose recomputed text DIFFERS are written.

Run:
    python3 debug/backfill_search_text_originalcontent.py          # apply
    python3 debug/backfill_search_text_originalcontent.py --dry-run # report only
"""
import asyncio
import json
import sys

sys.path.insert(0, __file__.rsplit('/debug/', 1)[0])

from lib.conversations import build_search_text  # noqa: E402
from lib.database import DOMAIN_CHAT, async_fetchall, async_transaction  # noqa: E402
from lib.database import _BACKEND  # noqa: E402
from lib.log import get_logger  # noqa: E402

logger = get_logger(__name__)


async def main(dry_run: bool) -> None:
    rows = await async_fetchall(
        'SELECT id, messages, search_text FROM conversations WHERE msg_count > 0',
        (), domain=DOMAIN_CHAT)
    total = len(rows)
    changed = 0
    print(f'[backfill] backend={_BACKEND} scanning {total} conversations '
          f'(dry_run={dry_run})')

    for r in rows:
        cid = r['id']
        raw = r['messages']
        try:
            messages = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[backfill] skip conv %s: bad messages JSON: %s', cid, e)
            continue
        new_st = build_search_text(messages)
        if new_st == (r['search_text'] or ''):
            continue
        changed += 1
        if dry_run:
            continue
        async with async_transaction(domain=DOMAIN_CHAT) as conn:
            if _BACKEND == 'pg':
                await conn.execute(
                    "UPDATE conversations SET search_text = ?, "
                    "search_tsv = to_tsvector('simple', left(?, 50000)) WHERE id = ?",
                    (new_st, new_st, cid))
            else:
                await conn.execute(
                    'UPDATE conversations SET search_text = ? WHERE id = ?',
                    (new_st, cid))
        # Keep the SQLite FTS5 mirror in sync when applicable.
        if _BACKEND != 'pg':
            from lib.conversations import update_conversation_fts
            from lib.database import get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            update_conversation_fts(db, cid, new_st)

    print(f'[backfill] {"would update" if dry_run else "updated"} '
          f'{changed}/{total} conversations')


if __name__ == '__main__':
    asyncio.run(main('--dry-run' in sys.argv))
