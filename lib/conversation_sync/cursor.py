"""Versioned opaque cursors for conversation-scoped ordered replay.

A replay cursor names the lineage of the last change its holder observed
(contracts/conversation_sync_v3.yaml ``x-tofu-stream.cursorLineage``): the
storage authority open that minted it (epoch), the change's sequence, and the
digest of that stored change. Crash recovery or a restore can give a sequence
to a different change; the digest tells the two apart, so such a cursor is
answered with a reset instead of a replay that would skip the new change.

``c1`` cursors were issued before cursors carried lineage. They decode to a
position without an epoch only so the stream can answer them with one reset;
the same ``c1`` form remains the lineage-free ordering hint of invalidation
frames, which never resume replay.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from lib.storage.generated_v2_semantics import CONVERSATION_SYNC_LINEAGE

_HEX_DIGITS = int(CONVERSATION_SYNC_LINEAGE["hexDigits"])
_LINEAGE_PATTERN = re.compile(
    rf"^c2\.([0-9a-f]{{16}})\.([0-9a-f]{{{_HEX_DIGITS}}})\.([0-9a-z]+)"
    rf"\.([0-9a-f]{{{_HEX_DIGITS}}}|-)$"
)
_LEGACY_PATTERN = re.compile(r"^c1\.([0-9a-f]{16})\.([0-9a-z]+)$")
_LINEAGE_VALUE = re.compile(rf"^[0-9a-f]{{{_HEX_DIGITS}}}$")
_NO_DIGEST = "-"


class ConversationCursorError(ValueError):
    """The caller supplied a malformed or wrong-conversation cursor."""


@dataclass(frozen=True)
class ReplayPosition:
    """The last change a cursor's holder observed.

    ``epoch`` names the authority open that minted the cursor; it is empty for
    a cursor issued before lineage and for the stream start. ``digest`` is the
    stored change's digest at ``sequence``; it is empty at sequence 0 and when
    the change had already left replay retention at minting.
    """

    sequence: int
    epoch: str = ""
    digest: str = ""


STREAM_START = ReplayPosition(0)


def is_lineage_value(value: Any) -> bool:
    """Whether ``value`` is an epoch or digest exactly as storage reports one."""
    return isinstance(value, str) and _LINEAGE_VALUE.fullmatch(value) is not None


def _scope_fingerprint(conversation_id: str, user_id: Any) -> str:
    """Return the stable scope fingerprint, memoized per (user, conversation).

    The SSE events loop encodes one cursor per replayed event (up to the page
    limit), and every invalidation publish re-derives the same fingerprint.
    The derivation is pure over a small, bounded key, so a per-process LRU
    removes a sha256 from each of those hot paths without changing the wire
    value. The cache holds only a deterministic digest (reconstructible), not
    durable state, so eviction is always safe.
    """
    return _scope_fingerprint_cached(str(user_id), conversation_id)


@lru_cache(maxsize=1024)
def _scope_fingerprint_cached(user_id: str, conversation_id: str) -> str:
    scope = f"{user_id}:{conversation_id}".encode("utf-8")
    return hashlib.sha256(scope).hexdigest()[:16]


def _base36(value: int) -> str:
    if value < 0:
        raise ValueError("cursor sequence cannot be negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded


def encode_cursor(conversation_id: str, user_id: Any, position: ReplayPosition) -> str:
    """Encode a replay position minted from one authority read."""
    if not is_lineage_value(position.epoch) or (
        position.digest and not is_lineage_value(position.digest)
    ):
        raise ValueError("a replay cursor needs its authority epoch and change digest")
    return (
        f"c2.{_scope_fingerprint(conversation_id, user_id)}.{position.epoch}."
        f"{_base36(int(position.sequence))}.{position.digest or _NO_DIGEST}"
    )


def decode_cursor(
    conversation_id: str, user_id: Any, cursor: str | None,
) -> ReplayPosition:
    """Decode and scope-check a cursor; an omitted cursor means stream start."""
    if cursor in (None, ""):
        return STREAM_START
    if not isinstance(cursor, str) or len(cursor) > 128:
        raise ConversationCursorError("cursor_invalid")
    scope = _scope_fingerprint(conversation_id, user_id)
    lineage = _LINEAGE_PATTERN.fullmatch(cursor)
    if lineage is not None and lineage.group(1) == scope:
        digest = lineage.group(4)
        return ReplayPosition(
            int(lineage.group(3), 36), lineage.group(2),
            "" if digest == _NO_DIGEST else digest,
        )
    legacy = _LEGACY_PATTERN.fullmatch(cursor)
    if legacy is not None and legacy.group(1) == scope:
        return ReplayPosition(int(legacy.group(2), 36))
    raise ConversationCursorError("cursor_invalid")


def encode_sequence_hint(conversation_id: str, user_id: Any, sequence: int) -> str:
    """Name a committed sequence for an invalidation hint, without lineage."""
    return f"c1.{_scope_fingerprint(conversation_id, user_id)}.{_base36(int(sequence))}"


def stream_start_cursor(conversation_id: str, user_id: Any) -> str:
    """The cursor of a holder that has seen no change.

    Sequence 0 names no change, so it has no lineage to verify; the
    lineage-free form decodes to the stream start in every authority open.
    """
    return encode_sequence_hint(conversation_id, user_id, 0)


def decode_sequence_hint(conversation_id: str, user_id: Any, hint: str | None) -> int:
    """Read the sequence an invalidation hint names; either cursor form is accepted."""
    if not hint:
        raise ConversationCursorError("cursor_invalid")
    return decode_cursor(conversation_id, user_id, hint).sequence


__all__ = [
    "ConversationCursorError",
    "ReplayPosition",
    "STREAM_START",
    "decode_cursor",
    "decode_sequence_hint",
    "encode_cursor",
    "encode_sequence_hint",
    "is_lineage_value",
    "stream_start_cursor",
]
