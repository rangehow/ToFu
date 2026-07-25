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

Fingerprint — why content, not mtime (MEASURED, do not "simplify" back):

  This deployment's filesystem (dolphinfs, FUSE) has **1-SECOND mtime
  granularity**: ``st_mtime_ns % 1_000_000_000 == 0`` for every file,
  ctime is equally coarse, and inode does not change on rewrite. A
  ``(mtime_ns, size)`` fingerprint is therefore BLIND to the edit class
  "same 1-second tick, same byte count, different content" — exactly what a
  fast sibling edit (or an atomic tmp+os.replace inside the same tick)
  produces. Verified live: 10 same-size writes within one second changed
  the fingerprint ZERO times.

  So for files ≤ ``_CONTENT_HASH_MAX_BYTES`` (256 KiB — covers every source
  file in the tree; blake2b at GB/s costs <0.1 ms) the fingerprint is
  CONTENT-addressed: ``('c', size, blake2b-128)``. Same-second same-size
  edits with different content are caught; a content-preserving touch is
  NOT stale (nothing to clobber). Files ABOVE the threshold keep the
  ``('m', mtime_ns, size)`` fast path — they are data/asset payloads, not
  the source-edit scenario, and the residual same-second blind spot there
  is documented rather than paid for with a full read on a FUSE mount.

Env: ``TOFU_WRITE_FRESHNESS_GATE=0`` disables recording AND checks.
This module is a leaf: stdlib + lib.log only, so both ``lib/project_mod``
(write-side recording) and ``lib/tasks_pkg`` (the gate) can import it
without cycles.
"""

from __future__ import annotations

import hashlib
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


# Files at or below this size get a CONTENT fingerprint (see the module
# docstring for the measured 1-second-mtime-granularity reason). 256 KiB
# covers every source file in the tree; the hash cost is sub-millisecond.
_CONTENT_HASH_MAX_BYTES = 256 * 1024


def _fingerprint(abs_path: str) -> tuple | None:
    """Fingerprint of *abs_path*, or None when it can't be read.

    Two shapes (see module docstring):
      * ``('c', size, blake2b_hex)`` — content-addressed, files ≤ 256 KiB;
      * ``('m', mtime_ns, size)``  — fast path, larger files.
    """
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    if st.st_size <= _CONTENT_HASH_MAX_BYTES:
        try:
            with open(abs_path, 'rb') as f:
                data = f.read()
        except OSError:
            return None
        return ('c', st.st_size,
                hashlib.blake2b(data, digest_size=16).hexdigest())
    return ('m', st.st_mtime_ns, st.st_size)


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
        _tokens[key] = (fp, time.time())
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
    return cur != entry[0]


def drop(conv_key: str, abs_path: str) -> None:
    """Forget one token (e.g. after an intentional delete). Never raises."""
    with _lock:
        _tokens.pop((conv_key, abs_path), None)


def _reset_for_tests() -> None:
    """Clear ALL tokens — test isolation helper (the store is process-global)."""
    with _lock:
        _tokens.clear()


__all__ = ['record', 'is_stale', 'drop', '_reset_for_tests', '_fingerprint',
           '_CONTENT_HASH_MAX_BYTES']
