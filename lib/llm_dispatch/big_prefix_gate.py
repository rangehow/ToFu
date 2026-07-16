"""lib/llm_dispatch/big_prefix_gate.py — Per-key big-prefix admission control.

Why this exists
===============
Anthropic's prompt cache is keyed **per API key**: each upstream key has one
shared server-side cache namespace with a finite working set.  When several
conversations with LARGE prompt prefixes (25万–43万 token opus turns) are
in-flight on the *same* key at once, they LRU-evict each other's cached
prefix — every eviction costs a full ``cache_creation`` re-write + 0% read on
the next round, then the prefix "rebounds" to a hit once it re-writes and the
competitor gets evicted instead.  Live evidence (2026-07-16, key ``key_0``):
in a dense 20:42–20:47 window 5 concurrent opus convs oscillated between
``cache_r=0`` (whole prefix evicted), ``cache_r≈79k`` (only the shared
system/tools floor survived) and full read-back — the classic mutual-eviction
signature.  Because opus was configured on a *single* key, all that load piled
into one cache namespace.

This gate is the client-side root guard that holds regardless of how many keys
exist: cap the number of concurrently-inflight BIG-prefix requests on any one
key.  A request over :func:`threshold_tokens` acquires a per-key slot before
its stream and releases it after; when the key is at capacity a new big request
briefly WAITS instead of piling on and blowing the cache.  Small requests
(translate, cheap models, short turns) never gate.

It is a *soft* guard: bounded wait, then proceed degraded — better to serve a
big prefix slightly late than to block a task forever.  Fully env-gated and
reversible.

Env knobs
---------
``TOFU_BIG_PREFIX_GATE``            — master switch (default on).
``TOFU_BIG_PREFIX_THRESHOLD_TOKENS`` — prefix size (est. tokens) above which a
                                     request is "big" and gated (default 150000).
``TOFU_BIG_PREFIX_MAX_PER_KEY``    — max concurrent big requests per key
                                     (default 2).
``TOFU_BIG_PREFIX_WAIT_MS``        — max time to wait for a slot before
                                     proceeding degraded (default 45000).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'gate_enabled',
    'threshold_tokens',
    'max_per_key',
    'wait_budget_ms',
    'estimate_prefix_tokens',
    'big_prefix_slot',
]


def gate_enabled() -> bool:
    """Whether the per-key big-prefix admission gate is active (default on)."""
    val = os.environ.get('TOFU_BIG_PREFIX_GATE', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def threshold_tokens() -> int:
    """Estimated-token prefix size above which a request is gated. Default 150000."""
    try:
        v = int(os.environ.get('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '150000'))
        return v if v > 0 else 150000
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_THRESHOLD_TOKENS parse failed, default: %s', e)
        return 150000


def max_per_key() -> int:
    """Max concurrent big-prefix requests allowed on one key. Default 2."""
    try:
        v = int(os.environ.get('TOFU_BIG_PREFIX_MAX_PER_KEY', '2'))
        return v if v >= 1 else 2
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_MAX_PER_KEY parse failed, default: %s', e)
        return 2


def wait_budget_ms() -> float:
    """Max time (ms) to wait for a per-key slot before proceeding degraded. Default 45000."""
    try:
        v = float(os.environ.get('TOFU_BIG_PREFIX_WAIT_MS', '45000'))
        return v if v > 0 else 45000.0
    except (ValueError, TypeError) as e:
        logger.debug('[BigPrefixGate] TOFU_BIG_PREFIX_WAIT_MS parse failed, default: %s', e)
        return 45000.0


def estimate_prefix_tokens(body_or_messages) -> int:
    """Cheap char-based estimate of a request's prompt size in tokens.

    Deliberately approximate (chars / 4) and lock-free — it runs on the dispatch
    hot path once per attempt and only needs to be good enough to separate BIG
    prefixes (>threshold) from ordinary turns.  Handles both a raw messages list
    and a body dict carrying ``messages``.  A tools blob (if present) is counted
    too since it sits in the cached prefix.

    Args:
        body_or_messages: the messages list, or a body dict with 'messages'.

    Returns:
        Estimated token count (>= 0). Returns 0 on any structural surprise so a
        malformed body never falsely trips the gate.
    """
    try:
        if isinstance(body_or_messages, dict):
            messages = body_or_messages.get('messages') or []
            tools = body_or_messages.get('tools') or []
        elif isinstance(body_or_messages, list):
            messages = body_or_messages
            tools = []
        else:
            return 0
        chars = 0
        for m in messages:
            content = m.get('content') if isinstance(m, dict) else None
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        # text / thinking blocks carry the bulk; count their text.
                        t = block.get('text') or block.get('thinking') or ''
                        if isinstance(t, str):
                            chars += len(t)
                        # image blocks: count base64 payload length (it's in the prefix).
                        src = block.get('source')
                        if isinstance(src, dict):
                            data = src.get('data')
                            if isinstance(data, str):
                                chars += len(data)
                    elif isinstance(block, str):
                        chars += len(block)
        if tools:
            try:
                import json
                chars += len(json.dumps(tools, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        return chars // 4
    except Exception as e:
        logger.debug('[BigPrefixGate] estimate_prefix_tokens failed: %s', e)
        return 0


# Per-key bounded gates, created lazily. Keyed by (key_name, capacity) so a
# runtime change to max_per_key rebuilds the semaphore cleanly rather than
# leaking permits on the old one.
_gates: dict[str, threading.BoundedSemaphore] = {}
_gates_cap: dict[str, int] = {}
_gates_lock = threading.Lock()


def _semaphore_for(key_name: str, capacity: int) -> threading.BoundedSemaphore:
    """Return (creating if needed) the per-key gate sized to *capacity*."""
    with _gates_lock:
        if _gates_cap.get(key_name) != capacity:
            _gates[key_name] = threading.BoundedSemaphore(capacity)
            _gates_cap[key_name] = capacity
        return _gates[key_name]


@contextlib.contextmanager
def big_prefix_slot(key_name: str, est_tokens: int, *, log_prefix: str = ''):
    """Admit at most :func:`max_per_key` big-prefix requests per key concurrently.

    Context manager. A no-op (immediate yield) when the gate is disabled, the
    request is below :func:`threshold_tokens`, or ``key_name`` is empty. For a
    BIG request it acquires the per-key slot, waiting up to
    :func:`wait_budget_ms`; if it can't acquire in time it proceeds anyway
    (degraded, logged at INFO) rather than blocking the task. Always releases on
    exit — success, exception, or degraded-proceed.

    Args:
        key_name: the API key this request will run on (from the picked slot).
        est_tokens: estimated prompt size (see :func:`estimate_prefix_tokens`).
        log_prefix: optional tag for correlating log lines.
    """
    if not gate_enabled() or not key_name or est_tokens < threshold_tokens():
        yield
        return

    sem = _semaphore_for(key_name, max_per_key())
    budget_s = wait_budget_ms() / 1000.0
    t0 = time.time()
    acquired = sem.acquire(timeout=budget_s)
    waited = time.time() - t0
    if acquired:
        if waited > 0.05:
            logger.info('%s [BigPrefixGate] admitted big prefix (~%dk tok) on '
                        'key=%s after waiting %.1fs (cap=%d)',
                        log_prefix, est_tokens // 1000, key_name, waited, max_per_key())
    else:
        # Degraded: proceed without a permit rather than stall the task. This
        # keeps the gate a soft guard — worst case is the pre-fix behavior.
        logger.info('%s [BigPrefixGate] proceeding DEGRADED for big prefix '
                    '(~%dk tok) on key=%s — %d already inflight, waited %.1fs '
                    '(budget %.0fs)', log_prefix, est_tokens // 1000, key_name,
                    max_per_key(), waited, budget_s)
    try:
        yield
    finally:
        if acquired:
            try:
                sem.release()
            except ValueError as e:
                # BoundedSemaphore raises if released too many times — never
                # expected here (one acquire ↔ one release), log and swallow.
                logger.warning('%s [BigPrefixGate] semaphore release on key=%s '
                               'raised: %s', log_prefix, key_name, e)
