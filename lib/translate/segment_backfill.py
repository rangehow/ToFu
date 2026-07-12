"""lib/translate/segment_backfill.py — narration-segment translation backfill.

THE GAP THIS CLOSES
-------------------
Auto-translate stamps per-round Chinese onto ``msg.segments[*].translatedText``
(read by the settled segment-timeline renderer) via two paths:

  • the LIVE incremental worker (``lib.translate.incremental``) — stamps each
    narration segment as its round closes, for forward-looking streaming turns;
  • the retro / whole-message path (``lib.translate.runtime._do_translate``) —
    builds ``{llmRound: 中文}`` and stamps too, but ONLY when it RUNS.

The retro path breaks early for a turn that already carries a non-stale
``translatedContent`` (its DELIVERABLE was translated), so a turn translated
before segments were stamped keeps its Chinese only in the bottom
``translatedContent`` blob — the interleaved narration falls back to English and
NEVER recovers on its own (no client/server path re-requests it, because both
``needsTranslation`` and the retro guard treat the turn as "done").

``_migrate_backfill_segment_translations`` fixes EXISTING rows once. This module
is the FORWARD fix: it is invoked on the conversation-GET path so that opening a
conversation with auto-translate on backfills any such ``_translateDone`` turn's
narration in the background (fire-and-forget), the exact "when I open a
conversation" behaviour.

SINGLE SOURCE OF TRUTH
----------------------
Reuses the EXACT translate core the live retro path uses
(``lib.translate.runtime._translate_segments_to_map`` — same eligibility,
notranslate handling, already-Chinese skip, enrich-only) and the EXACT stamp
helper (``lib.translate.commit._stamp_segment_translations``). It does NOT
re-implement "which segment is translatable" or "how the field is stamped". The
one-shot migration imports ``needs_segment_narration_translation`` from HERE so
the predicate is defined once.

REV-CAS NEUTRALITY
------------------
Stamping changes the ``messages`` column, which fires
``conversations_rev_bump_trg`` (rev → rev+1) and would make every open client
eat a spurious CAS 409. The write is guarded on the rev we read (skip on a
concurrent writer) and then resets rev back to that value in the SAME
transaction; ``updated_at`` is left untouched (no sidebar re-sort). Idempotent:
a re-run finds every narration already stamped → the core returns ``{}`` → no
write.

Thinking segments are intentionally never translated (backend contract — the
translator only stamps non-deliverable ``text`` narration segments).
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_TARGET = 'Chinese'
_SOURCE = ''


def needs_segment_narration_translation(msg) -> bool:
    """True iff ``msg`` is an already-translated assistant turn whose narration
    segments are missing ``translatedText`` (the backfill candidate condition).

    Pure read — mirrors the eligibility the live translate core enforces so a
    dry-run count matches what an apply would translate. Shared by the on-open
    trigger AND the one-shot migration (single source of truth).
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return False
    if not (msg.get('translatedContent') or '').strip():
        return False
    segs = msg.get('segments')
    if not isinstance(segs, list) or not segs:
        return False
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        if seg.get('type') != 'text' or seg.get('deliverable'):
            continue
        if seg.get('llmRound') is None:
            continue
        if not (seg.get('text') or '').strip():
            continue
        if not (seg.get('translatedText') or '').strip():
            return True
    return False


def conv_has_backfill_candidates(messages) -> bool:
    """Cheap pre-check: does this conversation carry ANY backfill candidate?

    Used by the GET path to decide whether to spawn the (LLM-calling) background
    task at all — the common case (no candidates) does zero extra work.
    """
    return any(needs_segment_narration_translation(m)
               for m in (messages or []) if isinstance(m, dict))


async def backfill_conv_narration_segments(conv_id: str, *, log_tag: str = '') -> dict:
    """Translate + stamp missing narration ``translatedText`` for one conversation.

    Enrich-only, rev-CAS-neutral, best-effort. Reuses the live translate core +
    stamp helper (single source of truth). Makes REAL LLM calls per missing
    narration segment, so callers gate on :func:`conv_has_backfill_candidates`
    and run this OFF the request path (background task) — it must never block a
    GET.

    Returns a summary ``{convId, messagesStamped, segmentsStamped, skipped,
    wrote}``. Never raises: per-row failures are logged and folded into the
    summary so a bad conversation cannot break the caller.
    """
    from lib.database import async_fetchone
    from lib.database.aio import async_transaction
    from lib.database._wrappers import json_dumps_pg
    from lib.translate.commit import _stamp_segment_translations
    from lib.translate.prompt import _build_translate_prompt
    from lib.translate.runtime import _translate_segments_to_map

    tag = log_tag or (conv_id or '?')[:8]
    summary = {'convId': conv_id, 'messagesStamped': 0, 'segmentsStamped': 0,
               'skipped': False, 'wrote': False}
    try:
        row = await async_fetchone(
            'SELECT messages, rev FROM conversations WHERE id=?', (conv_id,))
        if not row:
            return summary
        import json
        raw = row['messages']
        messages = raw if isinstance(raw, list) else json.loads(raw or '[]')
        eligible = [m for m in messages
                    if needs_segment_narration_translation(m)]
        if not eligible:
            return summary

        system_prompt = _build_translate_prompt(_TARGET, _SOURCE)
        msgs_stamped = segs_stamped = 0
        for m in eligible:
            seg_map = _translate_segments_to_map(
                m.get('segments'), system_prompt, _SOURCE, _TARGET, log_tag=tag)
            if not seg_map:
                continue
            before = sum(1 for s in m.get('segments', [])
                         if isinstance(s, dict) and (s.get('translatedText') or '').strip())
            _stamp_segment_translations(m, seg_map)
            after = sum(1 for s in m.get('segments', [])
                        if isinstance(s, dict) and (s.get('translatedText') or '').strip())
            gained = after - before
            if gained > 0:
                msgs_stamped += 1
                segs_stamped += gained
        if segs_stamped == 0:
            return summary

        try:
            expected_rev = int(row['rev'] or 0)
        except (TypeError, ValueError):
            expected_rev = 0

        messages_json = json_dumps_pg(messages)
        async with async_transaction() as conn:
            cur = await conn.execute(
                'UPDATE conversations SET messages=? WHERE id=? AND rev=?',
                (messages_json, conv_id, expected_rev))
            if getattr(cur, 'rowcount', 0) == 0:
                summary['skipped'] = True
                logger.info('[segment-xlate-onopen] conv=%s skipped — concurrent '
                            'writer moved rev; next open retries', tag)
                return summary
            # Reset rev so the messages UPDATE does not bump it (no client CAS 409).
            await conn.execute(
                'UPDATE conversations SET rev=? WHERE id=?', (expected_rev, conv_id))

        summary.update(messagesStamped=msgs_stamped, segmentsStamped=segs_stamped,
                       wrote=True)
        logger.info('[segment-xlate-onopen] conv=%s stamped %d segment(s) on '
                    '%d message(s) (rev held at %d)', tag, segs_stamped,
                    msgs_stamped, expected_rev)
        try:
            audit_log('conversation_segment_translation_backfill', conv_id=conv_id,
                      segments_stamped=segs_stamped, messages=msgs_stamped,
                      rev=expected_rev, trigger='on_open')
        except Exception as e:
            logger.debug('[segment-xlate-onopen] audit_log failed (non-fatal): %s', e)
        return summary
    except Exception as e:
        logger.error('[segment-xlate-onopen] conv=%s failed (%s): %s',
                     tag, type(e).__name__, e, exc_info=True)
        return summary
