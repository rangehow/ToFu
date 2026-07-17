"""lib/llm_dispatch/cache_settle.py — cache write-visibility settle gate.

Why this exists
===============
Anthropic's prompt cache has a documented write-visibility race: a cache entry
"only becomes available after the first response begins" (official prompt-cache
docs), and Anthropic's own SDK reproducer (``anthropics/anthropic-sdk-python``
issue #1451) shows that two back-to-back requests with an IDENTICAL cached
prefix miss on the second ~40% of the time — the second request fires before
the first request's cache WRITE has become visible upstream, so it re-writes
the same prefix (a full ``cache_creation`` bill + 0% read) instead of reading
it back. The reproducer's fix is a single mitigation: ``sleep 2s`` between the
two calls drops the miss rate to 0/20.

Our live floor-miss signature matches this race exactly: byte-identical prefix,
read collapses to the static floor, next round rebounds — concentrated in
FAST tool-loop / autopilot conversations that fire round N+1 within a second or
two of round N's stream ending, i.e. before the write settled. This is the
dominant residual after the client-side byte-freeze fixes (which zeroed the
prefix-mutation miss class) and is NOT addressed by the big-prefix admission
gate (that bounds cross-conversation working-set pressure, a different axis).

What this does
==============
Per CONVERSATION, remember when its last (big) request's stream ENDED. Before
the next big request on the SAME conversation, if less than a settle window has
elapsed since that stream end, wait out the remainder so the prior round's
cache write is visible before this round tries to read it back.

Design invariants (each one matters — see the tests):
  * Same-conversation only. Different conversations have different prefixes and
    cannot read each other's cache, so cross-conv timing is irrelevant here.
  * Big prefixes only. A miss on a sub-threshold prefix costs almost nothing,
    and small/cheap turns must never eat added latency. Reuses the big-prefix
    size threshold so the two gates classify "big" identically.
  * Tool-loop-internal latency only. The wait sits between the PRIOR round's
    stream end and THIS round's send — inside the agent's own tool loop, where
    the user is already waiting on tool execution. It never delays the FIRST
    request of a turn (no prior stream end recorded → zero wait), so the human's
    perceived time-to-first-token is unaffected.
  * Adaptive, not fixed. We wait only the REMAINDER of the settle window since
    the prior stream end. A round that already took >window (a long tool exec,
    a slow model turn) waits zero — the write has already settled.
  * Abort-aware. The wait uses ``abortable_sleep`` so a cancelled task breaks
    out immediately instead of blocking for the full window.
  * Env-gated + reversible. ``TOFU_CACHE_SETTLE=0`` disables it entirely;
    ``conv_id`` empty (headless / no identity) → transparent no-op.

Env knobs
---------
``TOFU_CACHE_SETTLE``            — master switch (default on).
``TOFU_CACHE_SETTLE_MS``         — settle window in ms: the minimum gap between
                                   a conv's prior stream END and its next big
                                   send. Default 1500 (the SDK #1451 mitigation
                                   used 2000; 1500 is the shortest window that
                                   reliably clears the race in our traffic while
                                   minimising added tool-loop latency).
``TOFU_CACHE_SETTLE_MAX_MS``     — hard cap on any single wait, so a clock skew
                                   or a bogus timestamp can never stall a
                                   request longer than this. Default 4000.
``TOFU_CACHE_SETTLE_THRESHOLD_TOKENS`` — prefix size above which settle applies.
                                   Default: the big-prefix gate threshold, so a
                                   turn that is "big" for admission is "big"
                                   here too.
"""

from __future__ import annotations

import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'settle_enabled',
    'settle_window_ms',
    'settle_max_wait_ms',
    'settle_threshold_tokens',
    'settle_before_send',
    'async_settle_before_send',
    'record_stream_end',
    '_reset_settle_for_tests',
]


def settle_enabled() -> bool:
    """Whether the cache write-visibility settle gate is active (default on)."""
    val = os.environ.get('TOFU_CACHE_SETTLE', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def settle_window_ms() -> float:
    """Minimum ms between a conv's prior stream END and its next big send.

    Default 1500. Anthropic's SDK #1451 reproducer cleared the race with a 2000
    ms sleep; 1500 is the shortest window that reliably clears it in our traffic
    while keeping added tool-loop latency minimal."""
    try:
        v = float(os.environ.get('TOFU_CACHE_SETTLE_MS', '1500'))
        return v if v > 0 else 1500.0
    except (ValueError, TypeError) as e:
        logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_MS parse failed, default: %s', e)
        return 1500.0


def settle_max_wait_ms() -> float:
    """Hard ceiling (ms) on any single settle wait. Default 4000.

    Bounds the worst case so a clock skew, a paused/resumed process, or a bogus
    stored timestamp can never stall a request longer than this."""
    try:
        v = float(os.environ.get('TOFU_CACHE_SETTLE_MAX_MS', '4000'))
        return v if v > 0 else 4000.0
    except (ValueError, TypeError) as e:
        logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_MAX_MS parse failed, default: %s', e)
        return 4000.0


def settle_threshold_tokens() -> int:
    """Prefix-size (est. tokens) above which settle applies.

    Defaults to the big-prefix admission threshold so a turn classified "big"
    for admission is "big" here too. A dedicated override
    (``TOFU_CACHE_SETTLE_THRESHOLD_TOKENS``) lets settle be tuned independently
    of admission when needed."""
    raw = os.environ.get('TOFU_CACHE_SETTLE_THRESHOLD_TOKENS')
    if raw is not None:
        try:
            v = int(raw)
            if v > 0:
                return v
        except (ValueError, TypeError) as e:
            logger.debug('[CacheSettle] TOFU_CACHE_SETTLE_THRESHOLD_TOKENS parse '
                         'failed, falling back to big-prefix threshold: %s', e)
    try:
        from lib.llm_dispatch.big_prefix_gate import threshold_tokens
        return threshold_tokens()
    except ImportError as e:
        logger.debug('[CacheSettle] big_prefix_gate threshold unavailable, '
                     'default 150000: %s', e)
        return 150000


# ── Process-global recency map: conv_id → last stream-END timestamp ──
# One entry per conversation, recording when its most recent big request's
# stream finished. Thread-safe, TTL-pruned, and size-capped so a long-lived
# server with many conversations can't grow it without bound.
_last_end: dict[str, float] = {}
_lock = threading.Lock()

# Entries older than this (seconds) are useless — well past any settle window,
# and past the cache TTL too — so they are pruned lazily on write.
_ENTRY_TTL_S = 3600.0
_MAX_ENTRIES = 4096


def _prune_locked(now: float) -> None:
    """Drop stale entries; if still over the cap, drop the oldest. Caller holds lock."""
    stale = [cid for cid, ts in _last_end.items() if now - ts > _ENTRY_TTL_S]
    for cid in stale:
        del _last_end[cid]
    if len(_last_end) > _MAX_ENTRIES:
        ordered = sorted(_last_end.items(), key=lambda kv: kv[1])
        for cid, _ in ordered[:len(_last_end) - _MAX_ENTRIES]:
            del _last_end[cid]


def record_stream_end(conv_id: str, *, now: float | None = None) -> None:
    """Record that ``conv_id``'s current request stream just ENDED.

    Called after a successful (or terminal) stream so the NEXT request on the
    same conversation can measure the gap and settle if it arrives too soon.
    No-op when the gate is disabled or ``conv_id`` is empty."""
    if not conv_id or not settle_enabled():
        return
    ts = now if now is not None else time.time()
    with _lock:
        _last_end[conv_id] = ts
        if len(_last_end) > _MAX_ENTRIES:
            _prune_locked(ts)


def _compute_wait_s(conv_id: str, est_tokens: int, now: float | None) -> tuple[float, float, float]:
    """Pure: return ``(wait_s, elapsed, window_s)`` for a prospective send.

    ``wait_s`` is 0.0 when no wait is warranted (gate off, empty conv,
    sub-threshold, no prior stream end, or window already elapsed). Shared by
    the sync and async entry points so the timing rules live in ONE place."""
    if not settle_enabled() or not conv_id:
        return 0.0, 0.0, 0.0
    if est_tokens < settle_threshold_tokens():
        return 0.0, 0.0, 0.0

    now = now if now is not None else time.time()
    with _lock:
        last = _last_end.get(conv_id)
    if last is None:
        # First big request of this conversation (this process) → nothing to
        # settle behind. Never delay the opening request of a turn.
        return 0.0, 0.0, 0.0

    window_s = settle_window_ms() / 1000.0
    elapsed = now - last
    # Guard against a clock going backwards (elapsed < 0) → treat as 0 elapsed.
    if elapsed < 0:
        elapsed = 0.0
    remaining = window_s - elapsed
    if remaining <= 0:
        # The prior write has already had the full window to settle.
        return 0.0, elapsed, window_s
    wait_s = min(remaining, settle_max_wait_ms() / 1000.0)
    return wait_s, elapsed, window_s


def _log_hold(wait_s: float, est_tokens: int, conv_id: str, elapsed: float,
              window_s: float, log_prefix: str) -> None:
    logger.info('%s [CacheSettle] holding %.2fs before big prefix (~%dk tok) '
                'conv=%s so prior round cache write settles (%.2fs since prior '
                'stream end, window %.2fs)', log_prefix, wait_s,
                est_tokens // 1000, conv_id[:8], elapsed, window_s)


def settle_before_send(conv_id: str, est_tokens: int, *,
                       abort_check=None, log_prefix: str = '',
                       now: float | None = None) -> float:
    """Wait so the prior same-conv round's cache write is visible before send.

    Returns the number of seconds actually waited (0.0 when no wait was needed
    or the gate was inactive) — the caller may log/aggregate it.

    No-op (returns 0.0) when: the gate is disabled, ``conv_id`` is empty, the
    prefix is below :func:`settle_threshold_tokens`, no prior stream end is
    recorded for this conv (the FIRST request of a turn — never delayed), or
    enough time has already elapsed since that stream end.

    The wait is the REMAINDER of the settle window since the prior stream end,
    hard-capped by :func:`settle_max_wait_ms`, and is abort-aware."""
    wait_s, elapsed, window_s = _compute_wait_s(conv_id, est_tokens, now)
    if wait_s <= 0:
        return 0.0
    _log_hold(wait_s, est_tokens, conv_id, elapsed, window_s, log_prefix)
    try:
        from lib.llm._transport import abortable_sleep
        abortable_sleep(wait_s, abort_check)
    except ImportError as e:
        logger.debug('[CacheSettle] abortable_sleep unavailable, plain sleep: %s', e)
        time.sleep(wait_s)
    return wait_s


async def async_settle_before_send(conv_id: str, est_tokens: int, *,
                                   abort_check=None, log_prefix: str = '',
                                   now: float | None = None) -> float:
    """Async counterpart of :func:`settle_before_send` for the async dispatch
    path. Same timing rules (shared :func:`_compute_wait_s`); uses
    ``async_abortable_sleep`` so it never blocks the event loop."""
    wait_s, elapsed, window_s = _compute_wait_s(conv_id, est_tokens, now)
    if wait_s <= 0:
        return 0.0
    _log_hold(wait_s, est_tokens, conv_id, elapsed, window_s, log_prefix)
    try:
        from lib.llm._transport import async_abortable_sleep
        await async_abortable_sleep(wait_s, abort_check)
    except ImportError as e:
        logger.debug('[CacheSettle] async_abortable_sleep unavailable: %s', e)
        import asyncio
        await asyncio.sleep(wait_s)
    return wait_s


def _reset_settle_for_tests() -> None:
    """Test hook: clear the recency map."""
    with _lock:
        _last_end.clear()
