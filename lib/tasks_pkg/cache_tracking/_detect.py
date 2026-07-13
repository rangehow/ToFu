"""Two-phase cache-break detection.

Owns the break-classification thresholds and the single-source
``EDITABLE_TAIL_COUNT`` immutable-prefix bound, the pure classifiers
(``_classify_break`` / ``_resolve_break_cause``), and the round-driving
``detect_cache_break`` which mutates the shared ``CacheState`` singleton.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.wire_fingerprint import diff_canonical
from lib.tasks_pkg.cache_tracking._state import (
    CacheState,
    _cache_lock,
    _cache_states,
    _state_key,
)
from lib.tasks_pkg.cache_tracking._hashing import (
    _diff_prefix_fields,
    _diff_tool_hashes,
    _hash_prefix_content,
    _hash_prefix_fields,
    _hash_system_prompt,
    _hash_tools,
    _hash_tools_per_tool,
)
from lib.tasks_pkg.cache_tracking._roi import _emit_l2_roi

logger = get_logger(__name__)


# Minimum absolute token drop required to trigger a cache break warning.
# Small drops (e.g., a few thousand tokens) can happen due to normal
# variation and aren't worth alerting on.
_MIN_CACHE_MISS_TOKENS = 2000

# ── Single-source editable-tail bound ──
# The number of trailing messages a backend writer (turn-end tail rewrite,
# reconcile) may mutate WITHOUT busting the cache. Equivalently: the cache
# prefix treated as immutable is messages[0 : len - EDITABLE_TAIL_COUNT].
# ``get_cache_prefix_count`` (what the cache assumes stable) and the
# reconcile prefix guard (what a writer is allowed to touch) BOTH read this
# ONE number, so "immutable prefix" and "mutable suffix" can never silently
# diverge. The value is 2 because the Anthropic tail breakpoint + the moving
# str↔block wrap keep the last user+assistant pair in flux each round.
EDITABLE_TAIL_COUNT = 2

# Minimum fresh cache_write (with zero cache_read) required to flag a
# "cache written but never read back" miss. Set well above the tiny-prompt
# alternating-WRITE/HIT noise floor (system+tools < 4096 tokens) so we only
# alert when re-writing the prefix actually cost real money. The motivating
# case: 2 rounds, both ~279k cache_write, zero cache_read.
_MIN_NO_REUSE_TOKENS = 20000


def _classify_break(
    *, call_count: int, was_compaction: bool,
    prev_cache_read: int, cache_read: int,
    prev_cache_write: int, cache_write: int,
    prev_prefix_tokens: int,
) -> tuple[bool, bool, bool]:
    """Phase-2 break classification (pure arithmetic on API cache tokens).

    Returns ``(api_break, no_reuse, partial_no_reuse)`` — the three
    mutually-narrowing break predicates.  Extracted verbatim from
    ``detect_cache_break``'s Phase 2 so the (subtle, much-commented)
    thresholds live in one testable place; the caller still owns the lock,
    the state mutation, and the cause/logging/return shaping.

      * api_break       — cache_read DROPPED >5% from a prior high read, with
                          the absolute drop over the miss threshold.
      * no_reuse        — a large fresh write with ZERO read despite an
                          established prefix last round.
      * partial_no_reuse— a large write repeated round-over-round while the
                          read stayed pinned (body re-billed uncached).
    """
    api_break = (
        call_count > 0
        and prev_cache_read > _MIN_CACHE_MISS_TOKENS
        and cache_read < prev_cache_read * 0.95
        and (prev_cache_read - cache_read) >= _MIN_CACHE_MISS_TOKENS
        and not was_compaction
    )

    no_reuse = (
        call_count > 0
        and cache_read == 0
        and cache_write >= _MIN_NO_REUSE_TOKENS
        and prev_prefix_tokens >= _MIN_NO_REUSE_TOKENS
        and not was_compaction
    )

    partial_no_reuse = (
        call_count > 0
        and not was_compaction
        and not api_break
        and not no_reuse
        and cache_write >= _MIN_NO_REUSE_TOKENS
        and prev_cache_write >= _MIN_NO_REUSE_TOKENS
        and cache_read <= prev_cache_read * 1.05
    )

    return bool(api_break), bool(no_reuse), bool(partial_no_reuse)


def _resolve_break_cause(
    *, client_changes: dict, prefix_mutation_break: bool,
    elapsed: float, cache_read: int, prefix_mutated: bool,
    prefix_culprits: list | None = None,
    wire_proven_identical: bool = False,
    history_rewrite: bool = False,
) -> str:
    """Build the single most-specific human cause string for a confirmed break.

    Precedence: explicit client changes → prefix-byte mutation (with the EXACT
    changed ``key.field`` list when known) → TTL expiry → server-side miss.

    The server-side verdict is now GATED on evidence, not reached by
    elimination. ``wire_proven_identical`` is True only when the authoritative
    post-translation wire fingerprint (see ``wire_fingerprint.py``) showed the
    actual sent bytes were byte-for-byte identical to the previous round. Only
    then may we state the miss is server-side as a PROVEN fact. When no wire
    fingerprint was captured (non-Claude / capture failure) we fall back to the
    legacy, honestly-hedged "stochastic server OR silent byte change" wording —
    the elimination guess, explicitly marked unproven.
    """
    _culprits = ', '.join(prefix_culprits) if prefix_culprits else ''
    if client_changes:
        return ', '.join(f'{k}={v}' for k, v in client_changes.items())
    if prefix_mutation_break:
        _base = ('cached prefix bytes changed between turns '
                 '(non-idempotent history edit) — the whole body '
                 'was re-billed uncached')
        return f'{_base} [changed: {_culprits}]' if _culprits else _base
    # ── Backend history rewrite ──
    # A reconcile / committed-dict projection edited or deleted a committed
    # message. This is a KNOWN backend cause, not an L2 compaction and not a
    # server-side miss — name it so it is not laundered into either.
    if history_rewrite:
        _base = ('backend history rewrite (reconcile / committed-dict '
                 'projection) edited or deleted a cached message — the '
                 'prefix was re-billed uncached')
        return f'{_base} [changed: {_culprits}]' if _culprits else _base
    if elapsed > 300:
        return 'TTL expiry (>5min gap, prompt unchanged)'
    # ── PROVEN server-side miss ──
    # The wire fingerprint confirmed our sent bytes were IDENTICAL to last
    # round, so the miss cannot be client-caused. This is the ONLY path allowed
    # to assert "server-side" as fact.
    if wire_proven_identical:
        if cache_read > _MIN_CACHE_MISS_TOKENS:
            return ('server-side cache miss — PROVEN: the wire bytes were '
                    'byte-identical to the previous round (only the body past '
                    'the static prefix was not read back)')
        return ('server-side cache miss — PROVEN: the wire bytes were '
                'byte-identical to the previous round (whole prefix not reused)')
    # ── Wire fingerprint UNAVAILABLE → legacy elimination guess (unproven) ──
    if cache_read > _MIN_CACHE_MISS_TOKENS:
        return ('likely server-side cache miss (UNPROVEN — no wire '
                'fingerprint; body re-billed, static prefix still cached)')
    if not prefix_mutated:
        return ('prefix not reused — likely server-side miss or TTL expiry '
                '(UNPROVEN — no wire fingerprint)')
    # Prefix bytes DID change but the write was below the surfacing floor.
    # We still know exactly which field moved — name it instead of guessing.
    if _culprits:
        return ('prefix bytes changed between turns '
                f'[changed: {_culprits}] — likely cause of the miss')
    return ('prefix not reused — likely server-side miss, TTL expiry, '
            'or a silent prefix byte change (UNPROVEN — no wire fingerprint)')


def detect_cache_break(
    conv_id: str,
    messages: list,
    tools: list | None,
    model: str,
    usage: dict | None = None,
) -> dict[str, Any] | None:
    """Two-phase cache break detection (inspired by Claude Code).

    Phase 1: Compare system/tools/model hashes to detect WHAT changed.
    Phase 2: Check API-reported cache_read_tokens to confirm whether
             a break actually occurred.

    Returns a dict describing what changed, or None if no break detected.
    Logs warnings on significant cache breaks for cost diagnostics.

    Key change from previous implementation:
      - Does NOT hash message content (avoids false positives from
        micro-compact mutations)
      - Only tracks system prompt, tools, model, and message count
      - Uses API-reported cache tokens as the source of truth
      - Accounts for compaction events (expected token drops)
    """
    if not conv_id:
        return None

    now = time.time()

    _key = _state_key(conv_id)
    with _cache_lock:
        prev = _cache_states.get(_key)
        if prev is None:
            prev = CacheState()
            _cache_states[_key] = prev

        # ── Phase 1: Detect WHAT changed (client-side hashes) ──
        sys_hash = _hash_system_prompt(messages)
        tools_hash = _hash_tools(tools)
        msg_count = len(messages)

        # Per-tool hash diffing for detailed diagnostics
        per_tool_hashes = _hash_tools_per_tool(tools)

        # Prefix content mutation detection (diagnostic only)
        # ★ FIX: Use the PREVIOUS call's prefix count for mutation comparison,
        #   then compute a NEW prefix hash for saving.
        #   Bug was: _prefix_count grew each round (prev.message_count - 2),
        #   so the hash covered MORE messages than prev.prefix_content_hash,
        #   causing false positives every round (942 in one log window!).
        #   Fix: compare hash(messages[0:prev_prefix]) against saved hash,
        #   then save hash(messages[0:new_prefix]) for next round.
        _prev_prefix_count = prev.prefix_content_count if prev.call_count > 0 else 0
        _new_prefix_count = max(0, msg_count - EDITABLE_TAIL_COUNT)
        _prev_prefix_hash = _hash_prefix_content(messages, _prev_prefix_count)
        prefix_hash = _hash_prefix_content(messages, _new_prefix_count)
        # Per-field hashes of the SAME (prev) range — lets us name the exact
        # message+field that changed, not just THAT the prefix changed.
        _cur_field_hashes_prevrange = _hash_prefix_fields(
            messages, _prev_prefix_count)
        prefix_field_hashes = _hash_prefix_fields(messages, _new_prefix_count)

        # Capture the history-rewrite signal (set by notify_history_rewrite
        # when the backend reconcile / committed-dict projection edited
        # committed messages). Read here for LABELING only; unlike compaction
        # it feeds NO break gate and does NOT skip the authoritative wire diff
        # below, so it can NEVER flip _wire_proven_identical to True. Cleared
        # in Phase 2 alongside compaction_pending.
        _was_history_rewrite = prev.history_rewrite_pending

        client_changes = {}
        _prefix_mutated = False
        _prefix_culprits: list = []
        if prev.call_count > 0:
            if sys_hash != prev.system_hash:
                client_changes['system_prompt'] = 'changed'
            if tools_hash != prev.tools_hash:
                # Identify exactly which tools changed
                tool_diffs = _diff_tool_hashes(
                    prev.per_tool_hashes, per_tool_hashes)
                if tool_diffs:
                    client_changes['tools'] = (
                        f'changed: [{", ".join(tool_diffs)}]')
                else:
                    client_changes['tools'] = 'changed (ordering or meta)'
            if model != prev.model:
                client_changes['model'] = f'{prev.model} → {model}'
            # Message count going DOWN indicates compaction/truncation OR a
            # backend history rewrite (reconcile / committed-dict projection).
            # Distinguish them so a reconcile deletion is not mislabeled as an
            # L2 compaction it never was.
            if msg_count < prev.message_count:
                _lbl = 'history rewrite' if _was_history_rewrite else 'compacted'
                client_changes['message_count'] = (
                    f'{prev.message_count} → {msg_count} ({_lbl})')

            # ★ Diagnostic: prefix content mutation detection
            # Compare hash of the SAME range (prev prefix count) to detect
            # if existing messages were silently mutated in-place.
            if (_prev_prefix_hash
                    and prev.prefix_content_hash
                    and _prev_prefix_hash != prev.prefix_content_hash
                    and not prev.compaction_pending):
                _prefix_mutated = True
                _prefix_culprits = _diff_prefix_fields(
                    prev.prefix_field_hashes, _cur_field_hashes_prevrange)
                # The ANONYMOUS leading-indicator warning ("...without
                # compaction") is only useful when the mutation cause is
                # UNKNOWN. A history-rewrite signal (e.g. the per-turn
                # user-profile / detail splice via notify_history_rewrite)
                # NAMES the cause, so suppress the anonymous alarm here — but
                # keep _prefix_mutated=True so the CONFIRMED, NAMED break still
                # fires below and the re-bill is attributed (unlike
                # compaction_pending, which blanket-suppresses detection).
                if not _was_history_rewrite:
                    logger.warning(
                        '[CacheTrack] conv=%s call=%d ⚠ PREFIX MUTATION DETECTED: '
                        'messages[0:%d] content hash changed without compaction. '
                        'This will cause a cache miss. changed=[%s] '
                        'prev_hash=%s new_hash=%s',
                        conv_id[:8], prev.call_count + 1, _prev_prefix_count,
                        ', '.join(_prefix_culprits) or '?',
                        prev.prefix_content_hash[:8], _prev_prefix_hash[:8])

        # ── Phase 2: Check API-reported cache stats ──
        cache_read = 0
        cache_write = 0
        if usage:
            cache_read = (usage.get('cache_read_tokens')
                          or usage.get('cache_read_input_tokens')
                          or 0)
            cache_write = (usage.get('cache_write_tokens')
                           or usage.get('cache_creation_input_tokens')
                           or 0)

        prev_cache_read = prev.last_cache_read_tokens

        # ★ FIX: compute elapsed BEFORE updating state so TTL detection works.
        # Previously, elapsed was computed AFTER setting last_update_time = now,
        # which meant it was always 0, making the >5min TTL check dead code.
        elapsed = now - prev.last_update_time if prev.last_update_time else 0

        # Handle compaction: if compaction happened, a drop in cache_read
        # is expected — don't flag it as a break. Capture the flag BEFORE
        # resetting it; the break/no-reuse guards below must see the value
        # this round had, not the already-cleared False.
        _was_compaction = prev.compaction_pending
        if prev.compaction_pending:
            prev.compaction_pending = False
            if cache_read < prev_cache_read:
                logger.debug(
                    '[CacheTrack] conv=%s Expected cache drop after compaction: '
                    '%d → %d tokens',
                    conv_id[:8], prev_cache_read, cache_read)
        # Clear the history-rewrite signal (captured above). Deliberately does
        # NOT gate the wire diff or any break classifier — a backend edit that
        # changed prefix bytes MUST still be caught and named, not silenced.
        if prev.history_rewrite_pending:
            prev.history_rewrite_pending = False

        # ── Phase-C: complete a pending L2 ROI record ──
        # An L2 force-summary event stashed its 'saved' half via
        # record_l2_compaction. THIS round is the one whose prompt was rebuilt
        # from the summarized prefix, so its cache_write is the tokens re-billed
        # BECAUSE the summary busted the prefix. Pair the two halves and emit
        # ONE structured metric with both sides populated, then clear it.
        if prev.pending_l2_roi is not None:
            _roi = prev.pending_l2_roi
            prev.pending_l2_roi = None
            # THIS round's prompt was rebuilt from the summarized prefix, so its
            # cache_write is the OBSERVED re-billed half. outcome='paired'.
            _emit_l2_roi(conv_id, _roi, cache_write_rebilled=int(cache_write),
                         cache_read_next=int(cache_read), now=now)

        # ── Authoritative wire-fingerprint prefix diff ──
        # `usage['_wire_fp']` is the post-translation, envelope-agnostic
        # fingerprint of the ACTUAL bytes sent this round (captured in
        # prepare_request — the only point after add_cache_breakpoints AND
        # openai_body_to_anthropic). Diffing it against the previous round's
        # stored fingerprint is GROUND TRUTH for prefix mutation, unlike the
        # `_hash_prefix_content` reconstruction above which is blind to the
        # build_body / breakpoint / anthropic-translation transforms. When
        # present it OVERRIDES the reconstruction's verdict:
        #   * identical  → our bytes did NOT change → any miss is PROVABLY
        #                  server-side (the "stochastic" label is now earned,
        #                  not reached by elimination).
        #   * differ     → names the exact msg.field WE mutated → client-caused.
        _cur_wire_fp = None
        _wire_static = ''
        if usage:
            _cur_wire_fp = usage.get('_wire_fp')
            _wire_static = usage.get('_wire_static') or ''
        _wire_available = _cur_wire_fp is not None
        _wire_prefix_changed = False
        _wire_culprits: list = []
        if (_wire_available and prev.call_count > 0
                and prev.wire_fp is not None and not _was_compaction):
            # Compare only the region that existed last round (its full length);
            # this round appends new tail messages we don't diff against.
            _shared = len(prev.wire_fp)
            _wire_culprits = diff_canonical(
                prev.wire_fp[:_shared], (_cur_wire_fp or [])[:_shared])
            _wire_prefix_changed = bool(_wire_culprits)
            if _wire_prefix_changed:
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ WIRE PREFIX CHANGED: the '
                    'ACTUAL sent bytes differ from last round — client-caused '
                    'miss. changed=[%s]',
                    conv_id[:8], prev.call_count + 1,
                    ', '.join(_wire_culprits[:8]) or '?')

        # ── Phase-2 break classification (pure; see _classify_break) ──
        #   api_break        — cache_read dropped from a prior high read.
        #   no_reuse         — large fresh write, zero read, established prefix.
        #   partial_no_reuse — large write repeated while read stayed pinned
        #                      (conversation body re-billed uncached).
        _prev_prefix_tokens = (prev.last_cache_read_tokens
                               + prev.last_cache_write_tokens)
        prev_cache_write = prev.last_cache_write_tokens
        api_break, no_reuse, partial_no_reuse = _classify_break(
            call_count=prev.call_count, was_compaction=_was_compaction,
            prev_cache_read=prev_cache_read, cache_read=cache_read,
            prev_cache_write=prev_cache_write, cache_write=cache_write,
            prev_prefix_tokens=_prev_prefix_tokens,
        )

        # ── Update state (AFTER elapsed computation) ──
        prev.system_hash = sys_hash
        prev.tools_hash = tools_hash
        prev.per_tool_hashes = per_tool_hashes
        prev.prefix_content_hash = prefix_hash
        prev.prefix_content_count = _new_prefix_count
        prev.prefix_field_hashes = prefix_field_hashes
        if _wire_available:
            prev.wire_fp = _cur_wire_fp
            prev.wire_static = _wire_static
        prev.model = model
        prev.message_count = msg_count
        prev.last_cache_read_tokens = cache_read
        prev.last_cache_write_tokens = cache_write
        prev.last_update_time = now
        prev.call_count += 1
        if not prev.first_call_time:
            prev.first_call_time = now
        # Accumulate session-level stats
        prev.total_cache_read += cache_read
        prev.total_cache_write += cache_write
        prompt_tokens = 0
        if usage:
            prompt_tokens = (usage.get('prompt_tokens')
                             or usage.get('input_tokens') or 0)
        prev.total_input_tokens += prompt_tokens + cache_write + cache_read

        # ★ When the authoritative wire fingerprint is available it REPLACES
        #   the client-side reconstruction as the prefix-mutation signal — it
        #   reflects the real sent bytes, so it neither misses a transform the
        #   reconstruction is blind to (downscale re-encode, anthropic re-dump)
        #   nor cries wolf on a benign wrapping the reconstruction over-counts.
        #   The reconstruction (_prefix_mutated) is used ONLY as a fallback when
        #   no wire fingerprint was captured (non-Claude / capture failure).
        if _wire_available:
            _prefix_mutated = _wire_prefix_changed
            _prefix_culprits = _wire_culprits
        _wire_proven_identical = _wire_available and not _wire_prefix_changed

        # ★ A silent prefix-byte mutation only counts as a CONFIRMED, surfaced
        #   break when it actually cost money this round (a real cache_write).
        #   On its own the hash change is a leading indicator; pairing it with
        #   a non-trivial write avoids crying wolf on rounds where the mutated
        #   prefix happened to still read back.
        prefix_mutation_break = (
            _prefix_mutated
            and not _was_compaction
            and cache_write >= _MIN_CACHE_MISS_TOKENS
        )

        if api_break or no_reuse or partial_no_reuse or prefix_mutation_break:
            prev.total_breaks += 1

        # ── Report ──
        # A cache break is "confirmed" when the API shows: a DROP from a prior
        # high read (api_break), a large fresh write with zero read despite an
        # established prefix (no_reuse), OR a large write repeated round-over-
        # round while the read stays pinned (partial_no_reuse). All three mean
        # we paid to rebuild (part of) the cache instead of reading it back.
        if api_break or no_reuse or partial_no_reuse or prefix_mutation_break:
            # Build the most specific cause we can (pure; see _resolve_break_cause).
            #
            # NOTE: "cache contention" between different conversations is NOT a
            # real phenomenon. A/B tested 2026-04-10: per-round cache_read is
            # identical between solo and interleaved modes (±0.0%). Anthropic
            # cache is keyed on exact prefix bytes — different conversations
            # have different keys and CANNOT evict each other.
            cause_str = _resolve_break_cause(
                client_changes=client_changes,
                prefix_mutation_break=prefix_mutation_break,
                elapsed=elapsed, cache_read=cache_read,
                prefix_mutated=_prefix_mutated,
                prefix_culprits=_prefix_culprits,
                wire_proven_identical=_wire_proven_identical,
                history_rewrite=_was_history_rewrite,
            )

            # ★ Prefix mutation is the most ACTIONABLE and most CERTAIN cause —
            #   it means our own code rewrote bytes inside the cached prefix,
            #   which GUARANTEES a miss regardless of any concurrent read drop.
            #   So it must win over `api_break` too: a round that both mutated
            #   the prefix AND shows a cache_read drop was previously falling
            #   through to the generic `server_side` "breakpoint advancement"
            #   label, hiding the real, fixable culprit (that exact mislabel is
            #   what the cost popover showed on memory-CRUD turns — the system
            #   prefix changed yet it read "服务端缓存失效（缓存断点前移…）").
            #   We still defer ONLY to `client_changes` (system/tools/model),
            #   which is a concrete, differently-named cause the popover labels
            #   on its own. Surfaced under the existing `prefix_mutation` key.
            if prefix_mutation_break and not client_changes:
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ PREFIX MUTATION BREAK: '
                    'cache_write=%d cache_read=%d (prev read=%d, gap=%.1fs) — '
                    'cached prefix bytes changed between turns. Cause: %s',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    prev_cache_read, elapsed, cause_str,
                )
                return {'prefix_mutation': cause_str}

            if no_reuse and not api_break:
                # The expensive, previously-undetected pattern: wrote a fresh
                # large prefix and read nothing back. WARN so each occurrence
                # is greppable in error.log for debugging.
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ NO CACHE REUSE: '
                    'cache_write=%d cache_read=0 (prev prefix=%d tokens, '
                    'gap=%.1fs). Cause: %s',
                    conv_id[:8], prev.call_count, cache_write,
                    _prev_prefix_tokens, elapsed, cause_str,
                )
                if client_changes:
                    return client_changes
                return {'no_cache_reuse': cause_str}

            if partial_no_reuse and not api_break and not no_reuse:
                # Big write repeated while cache_read stayed pinned at the
                # static prefix — the conversation body is being re-billed
                # uncached every round. WARN so each occurrence is greppable.
                logger.warning(
                    '[CacheTrack] conv=%s call=%d ⚠ PREFIX RE-WRITTEN: '
                    'cache_write=%d cache_read=%d (prev write=%d read=%d, '
                    'gap=%.1fs) — read pinned, body re-billed uncached. '
                    'Cause: %s',
                    conv_id[:8], prev.call_count, cache_write, cache_read,
                    prev_cache_write, prev_cache_read, elapsed, cause_str,
                )
                if client_changes:
                    return client_changes
                return {'no_cache_reuse': cause_str}

            # api_break path: cache_read dropped from a prior high value.
            if client_changes:
                logger.info(
                    '[CacheBreak] conv=%s call=%d CONFIRMED cache break: %s. '
                    'cache_read: %d → %d tokens (gap=%.1fs)',
                    conv_id[:8], prev.call_count, cause_str,
                    prev_cache_read, cache_read, elapsed,
                )
                return client_changes
            logger.info(
                '[CacheTrack] conv=%s call=%d cache_read dropped: %d → %d '
                '(gap=%.1fs, %s)',
                conv_id[:8], prev.call_count,
                prev_cache_read, cache_read, elapsed, cause_str,
            )
            return {'server_side': cause_str}
        elif client_changes:
            # Client-side changes detected but cache wasn't broken (or no
            # cache stats available) — log at debug level only.
            logger.debug(
                '[CacheTrack] conv=%s call=%d client changes: %s '
                '(cache_read: %d → %d, no confirmed break)',
                conv_id[:8], prev.call_count,
                ', '.join(f'{k}={v}' for k, v in client_changes.items()),
                prev_cache_read, cache_read,
            )

    return None
