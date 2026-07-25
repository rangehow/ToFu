"""Per-conversation write-freshness tokens (shared-HEAD overwrite guard).

On a shared working tree, conversation A can read a file at T0, think for
many rounds, and at T2 blindly overwrite a change sibling B committed at T1.
The read-before-edit gate (``lib/tasks_pkg/handlers/_read_gate.py``) proves
"you read this file at SOME point in this conversation"; it cannot prove
"the file has not changed SINCE". This module closes that gap with an
optimistic-concurrency token:

  • Every successful read / write / edit records a fingerprint
    ``(mtime_ns, size)`` of the target under ``(conv_key, abs_path)``.
  • Write tools are CHECKED before execution (see
    ``lib/tasks_pkg/handlers/_write_freshness_gate.py``): a recorded
    fingerprint that no longer matches the on-disk one means someone else
    touched the file → the write is refused with a re-read instruction
    instead of silently clobbering the sibling's change.

Deliberate failure directions:

  • A MISSING token never blocks (``is_stale`` → False). Blind full-rewrite
    flows that never read the file keep working — the read-before-edit gate
    owns the "must read first" axis for the edit tools; this module owns
    ONLY the "your knowledge went stale" axis.
  • A VANISHED file drops the token and allows the write (creation
    semantics); the read gate surfaces the cleaner "File not found" path
    for the edit tools.
  • Fingerprint = ``(st_mtime_ns, st_size)``, O(1) — no content hashing
    (writes already cost O(size); hashing would add a full read on a
    possibly-FUSE path). Cost of a false bounce (external touch without a
    content change): the model re-reads once. That is the safe direction.

Env: ``TOFU_WRITE_FRESHNESS_GATE=0`` disables recording AND checks.
This module is a leaf: stdlib + lib.log only, so both ``lib/project_mod``
(write-side recording) and ``lib/tasks_pkg`` (the gate) can import it
without cycles.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from lib.log import get_logger

logger = get_logger(__name__)

# Bounded LRU: (conv_key, abs_path) -> (mtime_ns, size, record_ts).
# 4096 entries ≈ plenty for concurrent conversations' working sets; eviction
# fails OPEN (an evicted token reads as "no token" → write allowed, which is
# the pre-guard behaviour).
_MAX_ENTRIES = 4096
_lock = threading.Lock()
_tokens: OrderedDict = OrderedDict()


def _gate_enabled() -> bool:
    val = os.environ.get('TOFU_WRITE_FRESHNESS_GATE', '1').strip().lower()
    return val not in ('0', 'false', 'no', 'off', '')


def _fingerprint(abs_path: str) -> tuple | None:
    """(mtime_ns, size) of *abs_path*, or None when it can't be stat'd."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def record(conv_key: str, abs_path: str) -> None:
    """Record the current fingerprint of *abs_path* for *conv_key*.

    Called after every successful read/write of the file by this
    conversation. Never raises; a stat failure simply skips recording.
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return
    fp = _fingerprint(abs_path)
    if fp is None:
        return
    key = (conv_key, abs_path)
    with _lock:
        _tokens.pop(key, None)  # refresh recency on re-record
        _tokens[key] = (fp[0], fp[1], time.time())
        while len(_tokens) > _MAX_ENTRIES:
            _tokens.popitem(last=False)


def is_stale(conv_key: str, abs_path: str) -> bool:
    """True iff a token exists AND the file exists AND its fingerprint moved.

    False (fail-open) when: gate disabled, no token recorded, or the file
    has since vanished (creation semantics — the stale token is dropped).
    """
    if not conv_key or not abs_path or not _gate_enabled():
        return False
    key = (conv_key, abs_path)
    with _lock:
        entry = _tokens.get(key)
    if entry is None:
        return False
    cur = _fingerprint(abs_path)
    if cur is None:
        with _lock:
            _tokens.pop(key, None)
        return False
    return (cur[0], cur[1]) != (entry[0], entry[1])


def drop(conv_key: str, abs_path: str) -> None:
    """Forget one token (e.g. after an intentional delete). Never raises."""
    with _lock:
        _tokens.pop((conv_key, abs_path), None)


def _reset_for_tests() -> None:
    """Clear ALL tokens — test isolation helper (the store is process-global)."""
    with _lock:
        _tokens.clear()


__all__ = ['record', 'is_stale', 'drop', '_reset_for_tests', '_fingerprint']
