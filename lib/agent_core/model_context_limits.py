"""Backend-independent bounds for serialized model continuation projections.

Storage admission and compaction restore consume the same limits. This module
has no storage dependencies so transient/core compositions can validate context.
"""

import json

MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 16384
# Normal Continue archive search; the cold snapshot must prove this covers
# the whole owned scope before describing its selection candidates.
MAX_RESUME_ARCHIVE_CANDIDATES = 8
# Bounds only legacy per-Turn witness lists (checkpoints without historyProof).
# A proof checkpoint carries one terminal witness whatever the lane length.
MAX_TURN_WITNESSES = 4096
# Largest integer every JSON peer represents exactly (RFC 7493, I-JSON). A
# history-proof count is a plain counter; this representation limit is its
# only bound, mirrored by Tofu-DB's model_continuation::MAX_JSON_SAFE_INTEGER.
MAX_JSON_SAFE_INTEGER = 2**53 - 1


def json_within_bytes(value, remaining):
    """Check a detached public JSON value without assembling another document."""
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        for part in encoder.iterencode(value):
            remaining -= len(part.encode('utf-8'))
            if remaining < 0:
                return False
    except (TypeError, ValueError, RecursionError):
        return False
    return True
