"""Cache-aware tool-result ordering, prefix-count gate & diagnostics.

``get_cache_prefix_count`` reads the shared ``_cache_states`` singleton and
the single-source ``EDITABLE_TAIL_COUNT`` bound; ``sort_tool_results`` uses it
to avoid re-ordering already-cached bytes; ``get_cache_diagnostics`` snapshots
both ``_cache_states`` and the TTL-latch table.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.cache_tracking._state import (
    _cache_lock,
    _cache_states,
    _state_key,
)
from lib.tasks_pkg.cache_tracking._detect import EDITABLE_TAIL_COUNT
from lib.tasks_pkg.cache_tracking._ttl import _ttl_latch

logger = get_logger(__name__)


def sort_tool_results(messages: list, conv_id: str = '') -> None:
    """Sort consecutive tool-result messages by tool_call_id for cache stability.

    When multiple tool results come back from parallel tool execution, their
    order in the messages array may vary between rounds if tools complete in
    different orders.  This causes the prefix to differ even though the
    content is identical, breaking automatic prefix caching (OpenAI/Qwen).

    For Anthropic explicit breakpoints, this is less critical since the
    breakpoints mark exact positions.  But it doesn't hurt and improves
    determinism.

    This function finds consecutive runs of tool-role messages and sorts
    them by tool_call_id.  It's called before build_body to ensure
    deterministic ordering.

    ★ CACHE-CRITICAL: reordering messages inside the prompt-cache PREFIX
    rewrites the cached prefix bytes and forces a full re-cache — the exact
    silent cache-killer this module otherwise hunts. So the sort is gated to
    indices at/after ``get_cache_prefix_count(conv_id)``: a run that begins
    inside the prefix is left untouched (it was already cached in some order;
    re-sorting it now can only HURT). Newly-appended tool results (the tail,
    which is what actually varies round-over-round) are still sorted. Runs
    that straddle the boundary are skipped entirely rather than partially
    sorted, which would itself mutate prefix bytes.

    Args:
        messages: The messages list (mutated in place).
        conv_id:  Conversation ID — used to look up the cache-prefix boundary.
            When empty (no cache tracked), behaves as before (sort everywhere).
    """
    if not messages or len(messages) < 2:
        return

    _prefix_count = 0
    if conv_id:
        try:
            _prefix_count = get_cache_prefix_count(conv_id)
        except Exception as e:
            logger.debug('[CacheTrack] sort_tool_results prefix lookup failed: %s', e)

    i = 0
    n = len(messages)
    while i < n:
        # Find start of a tool-result run
        if messages[i].get('role') == 'tool':
            run_start = i
            while i < n and messages[i].get('role') == 'tool':
                i += 1
            run_end = i
            # Only sort if there are 2+ consecutive tool results AND the whole
            # run lies OUTSIDE the cached prefix (run_start >= prefix_count).
            # A run that begins inside the prefix \u2014 or straddles the boundary
            # \u2014 is skipped: re-ordering already-cached bytes guarantees a miss.
            if run_end - run_start >= 2 and run_start >= _prefix_count:
                # Sort by tool_call_id for deterministic ordering
                tool_run = messages[run_start:run_end]
                tool_run.sort(key=lambda m: m.get('tool_call_id', ''))
                messages[run_start:run_end] = tool_run
        else:
            i += 1


def get_cache_prefix_count(conv_id: str) -> int:
    """Get the number of messages in the cache prefix for this conversation.

    Microcompact should skip editing messages[0:N] where N is this count,
    to keep cached content byte-identical for automatic prefix caching
    providers (OpenAI, Qwen, etc.).

    Returns the message count from the previous call if cache was active.
    For Anthropic (explicit breakpoints), this is less critical since
    add_cache_breakpoints places markers at the conversation tail.
    """
    with _cache_lock:
        state = _cache_states.get(_state_key(conv_id))
        # ★ Gate on WRITE as well as READ. The previous round may have only
        #   WRITTEN the prefix (cache_read=0, large cache_write) — e.g. round 1
        #   of a fresh conversation. That prefix is fully cached and reusable
        #   next round, so it must be protected from micro-compact mutation.
        #   Gating on read alone left round-2 unprotected after a round-1
        #   write, letting L1 mutate the just-written prefix → guaranteed miss.
        if state and (state.last_cache_read_tokens > 1000
                      or state.last_cache_write_tokens > 1000):
            # Cache was active — protect the prefix. Keep the last
            # EDITABLE_TAIL_COUNT messages editable (single-sourced bound).
            return max(0, state.message_count - EDITABLE_TAIL_COUNT)
    return 0


def get_cache_diagnostics() -> dict[str, Any]:
    """Return a diagnostic snapshot of all active cache states.

    Useful for admin endpoints, debugging, or periodic health checks.

    Returns:
        Dict with overall stats and per-conversation summaries.
    """
    now = time.time()
    with _cache_lock:
        convs = []
        total_breaks = 0
        total_reads = 0
        total_writes = 0
        for key, state in _cache_states.items():
            cid = key[0]
            age = now - state.last_update_time if state.last_update_time else 0
            convs.append({
                'conv_id': cid[:8],
                'model': state.model,
                'calls': state.call_count,
                'last_cache_read': state.last_cache_read_tokens,
                'last_cache_write': state.last_cache_write_tokens,
                'total_breaks': state.total_breaks,
                'age_s': round(age, 1),
                'compaction_pending': state.compaction_pending,
            })
            total_breaks += state.total_breaks
            total_reads += state.total_cache_read
            total_writes += state.total_cache_write
        return {
            'active_conversations': len(convs),
            'total_breaks': total_breaks,
            'total_cache_read_tokens': total_reads,
            'total_cache_write_tokens': total_writes,
            'ttl_latches_active': len(_ttl_latch),
            'conversations': sorted(
                convs, key=lambda c: c['age_s']),
        }
