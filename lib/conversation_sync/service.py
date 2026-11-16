"""Application service for authoritative snapshots and ordered replay."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.boot_identity import BOOT_ID
from lib.conversation_sync.settings_policy import public_conversation_settings
from lib.conversation_sync.cursor import (
    ConversationCursorError,
    ReplayPosition,
    decode_cursor,
    encode_cursor,
    is_lineage_value,
)
from lib.conversation_sync.generated_contract import STREAM_POLICY
from lib.storage.errors import StorageError
from lib.storage.generated_v2_semantics import CONVERSATION_SYNC_LINEAGE as _LINEAGE
from lib.conversation_sync.repository import ConversationSyncRepository
from lib.conversation_sync.snapshot_query import ConversationSnapshotQuery
from lib.conversation_sync.turn_images import (
    ConversationTurnImage,
    ConversationTurnImageNotFound,
    ConversationTurnImageStale,
    decode_stored_turn_image,
    turn_image_owner_scope,
)
from lib.conversation_sync.validation import decode
from lib.task_replay.task_event_contract import (
    ATTEMPT_EVIDENCE_REFERENCE_KEY,
    attempt_evidence_reference,
)
from lib.turn_image_transport import MAX_TURN_IMAGES
from lib.turn_projection_segments import (
    change_without_provider_replay,
    public_change_with_stable_segments,
    public_value_with_stable_segments,
    snapshot_with_reference_tool_segments,
)


#: Rank and history-page reads answer at the newest head the reader has not
#: passed. Storage checks the head before reading any Turn, so another commit
#: landing between observing the head and re-reading at it needs a storage
#: round trip, not a browser one. Bounded because a head that never settles
#: is not a reader's.
STREAMING_HEAD_READ_ATTEMPTS = 4


def _without_diagnostic_body(change: Any) -> Any:
    """Copy only the diagnostic envelope; never rewrite its canonical patch.

    Request Inspector already owns authenticated, demand-driven evidence reads.
    Its multi-megabyte request snapshots must not head-of-line block ordinary
    progress/terminal replay. The repository's full event remains untouched.
    """
    body = _attempt_body(change)
    if body is None or ('event' not in body and ATTEMPT_EVIDENCE_REFERENCE_KEY not in body):
        return change
    return _with_attempt_body(change, {
        key: value for key, value in body.items()
        if key not in ('event', ATTEMPT_EVIDENCE_REFERENCE_KEY)})


def _attempt_body(change: Any) -> dict[str, Any] | None:
    if not isinstance(change, dict) or change.get('type') != 'attempt.event':
        return None
    payload = change.get('payload')
    attempt = payload.get('event') if isinstance(payload, dict) else None
    body = attempt.get('payload') if isinstance(attempt, dict) else None
    return body if isinstance(body, dict) else None


def _with_attempt_body(change: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Copy-on-write: the repository's stored change is never mutated."""
    payload = change['payload']
    return {**change, 'payload': {**payload, 'event': {**payload['event'], 'payload': body}}}


def _with_referenced_evidence(change: Any, repository: ConversationSyncRepository) -> Any:
    """Resolve the evidence an attempt event names to its task-event row.

    The row is the frame's single durable copy (docs/STORAGE_EVENT_REPLAY.md,
    "Diagnostic evidence is stored once"); a row retention already removed
    leaves the body absent, as the ``none`` representation does.
    """
    body = _attempt_body(change)
    reference = attempt_evidence_reference(body)
    if reference is None:
        return change
    resolved = {key: value for key, value in body.items() if key != ATTEMPT_EVIDENCE_REFERENCE_KEY}
    frame = repository.task_event(*reference)
    if frame is not None:
        resolved['event'] = frame
    return _with_attempt_body(change, resolved)


def _require_provider_replay_payload(value: str, segment_payload: str | None = None) -> None:
    """``none`` omits provider replay evidence; a snapshot view requires refs."""
    if value not in {"full", "none"}:
        raise ValueError("Invalid conversation provider replay payload")
    if value == "none" and segment_payload not in {None, "refs"}:
        raise ValueError("Provider replay omission requires reference segments")


def _backfill_legacy_observation_attempt_ids(turn: Any) -> Any:
    """Legacy/read-adapter boundary: client-perception receipts persisted
    before attempt tracking lack the now-required attemptId. Backfill the
    empty identity so historical turns stay readable; rows missing other
    required fields still fail closed at decode. Copy-on-write: repository
    state is never mutated.
    """
    if not isinstance(turn, dict):
        return turn
    projection = turn.get("projection")
    if not isinstance(projection, dict):
        return turn
    trace = projection.get("timingTrace")
    if not isinstance(trace, dict):
        return turn
    observations = trace.get("clientObservations")
    if not isinstance(observations, list) or not any(
        isinstance(item, dict) and "attemptId" not in item
        for item in observations
    ):
        return turn
    patched = [
        {**item, "attemptId": ""}
        if isinstance(item, dict) and "attemptId" not in item
        else item
        for item in observations
    ]
    return {
        **turn,
        "projection": {
            **projection,
            "timingTrace": {**trace, "clientObservations": patched},
        },
    }


# Persisted-read adapter registry: every legacy-shape lift applied at the
# snapshot/turn-page read boundary, in registration order. Each adapter is
# copy-on-write and must be pinned by a legacy fixture under
# contracts/fixtures/sync_v3/ (tests/conversation_sync/test_wire_fixtures.py).
TURN_READ_ADAPTERS: tuple[Callable[[Any], Any], ...] = (
    _backfill_legacy_observation_attempt_ids,
)


def adapt_turn_read(turn: Any) -> Any:
    """Run the registered persisted-read adapters over one stored turn."""
    for adapter in TURN_READ_ADAPTERS:
        turn = adapter(turn)
    return turn


_LINEAGE_RESETS = STREAM_POLICY["cursorLineage"]["resetReasons"]
_ABSENT = object()


def _stored_lineage(stored: Any, *, operation: str) -> tuple[str, str]:
    """The epoch and head digest an authority read beside its rows.

    A result without them comes from an engine older than this tree; minting
    lineage-free cursors from it would reopen the silent-divergence gap, so the
    read fails closed instead.
    """
    epoch = stored.get(_LINEAGE["epochField"])
    head_digest = stored.get(_LINEAGE["headDigestField"], _ABSENT)
    if not is_lineage_value(epoch) or not (
            head_digest is None or is_lineage_value(head_digest)):
        raise StorageError(
            "database_protocol_error", f"{operation} result carries no replay lineage")
    return epoch, head_digest or ""


def _lineage_reset_reason(
    after: ReplayPosition, stored: Any, epoch: str, *, operation: str,
) -> str | None:
    """The reset a position's lineage requires at this read, if any.

    Storage reports the digest of the change now at the position's sequence
    (``anchorDigest``) from the same transaction as the page. A different
    digest means crash recovery or a restore gave the sequence to another
    change. An absent change left retention: only the open that minted the
    cursor can vouch that no recovery reused its sequence since.
    """
    if after.sequence and not after.epoch:
        return _LINEAGE_RESETS["withoutLineage"]
    if stored.get("resetRequired"):
        return str(stored.get("resetReason") or "cursor_invalid")
    if not after.sequence:
        return None
    anchor = stored.get(_LINEAGE["anchorDigestField"], _ABSENT)
    if anchor is None:
        return None if after.epoch == epoch else _LINEAGE_RESETS["lineageUnproven"]
    if not is_lineage_value(anchor):
        raise StorageError(
            "database_protocol_error",
            f"{operation} result does not name the cursor's change")
    return None if anchor == after.digest else _LINEAGE_RESETS["changeReplaced"]


class ConversationSyncNotFound(LookupError):
    pass


class ConversationTurnPageStale(RuntimeError):
    """No history page answers the reader at this read.

    ``current_sync_sequence`` names the head the reader can catch up to. It is
    None when the reader's own lineage is gone (``reason``, a cursor-lineage
    reset reason): no head of the authority's lineage extends that replica,
    which repairs from one snapshot instead.
    """

    def __init__(self, current_sync_sequence: int | None, *, reason: str | None = None) -> None:
        super().__init__("Conversation history page cursor is stale")
        self.current_sync_sequence = current_sync_sequence
        self.reason = reason


class ConversationSyncService:
    """Pure orchestration over an injected, user-scoped repository."""

    def __init__(
        self,
        repository: ConversationSyncRepository,
        *,
        snapshot_query_factory: Callable[
            [Callable[..., dict[str, Any]]], ConversationSnapshotQuery
        ] = ConversationSnapshotQuery,
    ) -> None:
        self._repository = repository
        self._snapshot_query = snapshot_query_factory(
            self._load_authoritative_snapshot
        )

    @property
    def heartbeat_interval_ms(self) -> int:
        return int(STREAM_POLICY["heartbeatIntervalMs"])

    def snapshot(
        self,
        conversation_id: str,
        user_id: int,
        *,
        push_withheld: bool = False,
        segment_payload: str = "full",
        tool_payload: str = "full",
        provider_replay_payload: str = "full",
        turn_limit: int | None = None,
        include_artifact_hint: bool = False,
        max_projection_bytes: int = 0,
    ) -> dict[str, Any]:
        if tool_payload not in {"full", "lazy"} or (tool_payload == "lazy" and segment_payload != "refs"):
            raise ValueError("Lazy tool payload requires reference segments")
        _require_provider_replay_payload(provider_replay_payload, segment_payload)
        if segment_payload not in {"full", "refs"}:
            raise ValueError("Invalid conversation segment payload")
        if turn_limit is not None and not 1 <= turn_limit <= 256:
            raise ValueError("Invalid conversation snapshot turn limit")
        snapshot = self._snapshot_query.read(
            conversation_id,
            user_id,
            push_withheld=push_withheld,
            representation=segment_payload,
            project_representation=(
                snapshot_with_reference_tool_segments
                if segment_payload == "refs"
                else None
            ),
            project_representation_kwargs=(
                {
                    **({"lazy_tool_results": True} if tool_payload == "lazy" else {}),
                    **({"provider_replay": "none"} if provider_replay_payload == "none" else {}),
                    "owner_cache_scope": turn_image_owner_scope(
                        user_id, conversation_id
                    ),
                }
                if segment_payload == "refs"
                else None
            ),
            turn_limit=turn_limit,
            include_artifact_hint=include_artifact_hint,

            max_projection_bytes=max_projection_bytes,
        )
        # The shared authority has already passed generated-schema validation.
        # A refs flight only removes optional fields and introduces declared
        # references; its executable contract is pinned in projection tests.
        return snapshot

    def _load_authoritative_snapshot(
        self,
        conversation_id: str,
        user_id: int,
        turn_limit: int | None = None,
        *,
        include_artifact_hint: bool = False,
        max_projection_bytes: int = 0,
    ) -> dict[str, Any]:
        snapshot_options: dict[str, Any] = {}
        if turn_limit is not None:
            snapshot_options["turn_limit"] = turn_limit
        if include_artifact_hint:
            snapshot_options["include_artifact_hint"] = True

        if max_projection_bytes:
            snapshot_options["max_projection_bytes"] = max_projection_bytes
        stored = self._repository.snapshot(
            conversation_id,
            user_id,
            **snapshot_options,
        )
        if stored is None:
            raise ConversationSyncNotFound("Conversation not found")
        sequence = int(stored.get("syncSequence") or 0)
        epoch, head_digest = _stored_lineage(stored, operation="turn.sync.snapshot")
        # Legacy/read-adapter boundary: pre-presentation Turn rows remain
        # readable without teaching the browser a second identity state
        # machine. New storage rows already carry the explicit value; only an
        # absent/empty legacy field deterministically falls back to turnId.
        stored_turns: list[Any] = []
        for raw_turn in list(stored.get("turns") or []):
            if isinstance(raw_turn, dict) and not raw_turn.get("presentationId"):
                raw_turn = {
                    **raw_turn,
                    "presentationId": str(raw_turn.get("turnId") or ""),
                }
            stored_turns.append(adapt_turn_read(raw_turn))
        response = {
            "ok": True,
            "contract": "tofu.conversation-sync.snapshot/v1",
            "scope": {
                "kind": "conversation",
                "ownerId": user_id,
                "threadId": conversation_id,
            },
            "conversationId": conversation_id,
            "conversationRevision": int(stored.get("conversationRevision") or 0),
            "syncSeq": sequence,
            "cursor": encode_cursor(
                conversation_id, user_id, ReplayPosition(sequence, epoch, head_digest)),
            "serverBootId": BOOT_ID,
            "heartbeatIntervalMs": self.heartbeat_interval_ms,
            "settings": public_conversation_settings(stored.get("settings") or {}),
            "turns": public_value_with_stable_segments(
                stored_turns
            ),
            "attempts": list(stored.get("attempts") or []),
            "queueItems": list(stored.get("queueItems") or []),
            # Read-side delivery-wedge signal (withheld authoritative pushes);
            # the query layer stamps the current request-local value after any
            # shared authority read.
            "pushWithheld": False,
        }
        if isinstance(stored.get("activeAttemptPhase"), dict):
            response["activeAttemptPhase"] = stored["activeAttemptPhase"]
        if isinstance(stored.get("turnWindow"), dict):
            response["turnWindow"] = dict(stored["turnWindow"])
        if include_artifact_hint and isinstance(stored.get("hasArtifacts"), bool):
            response["hasArtifacts"] = stored["hasArtifacts"]
        return decode("ConversationSyncSnapshot", response)

    def round_directory(
        self, conversation_id: str, user_id: int, *, lane_id: str,
        expected_sync_sequence: int, offset: int = 0, limit: int = 50,
        anchor_turn_id: str = "",
    ) -> dict[str, Any]:
        """Return exact input ranks at the newest head the caller has not passed.

        Ranks change only when a Turn is created or deleted, yet every streamed
        attempt event advances the sync head, so while a reply streams the
        caller's replayed head is almost always behind. Answering at the head
        the read observed keeps the ranks exact for that head (``syncSeq``
        reports it); the caller's own replay delivers any rank change in
        between. Only a head behind the caller remains ``stale``.
        """
        sequence = expected_sync_sequence
        for _attempt in range(STREAMING_HEAD_READ_ATTEMPTS):
            stored = self._repository.round_directory(
                conversation_id, user_id, lane_id=lane_id,
                expected_sync_sequence=sequence, offset=offset,
                limit=limit, anchor_turn_id=anchor_turn_id,
            )
            if stored is None:
                raise ConversationSyncNotFound("Conversation not found")
            head = int(stored.get("syncSeq") or 0)
            if stored.get("stale") is not True or head < expected_sync_sequence:
                break
            sequence = head
        return decode("ConversationRoundDirectory", stored)

    def turn_page(
        self,
        conversation_id: str,
        user_id: int,
        *,
        lane_id: str,
        expected_sync_sequence: int,
        position: ReplayPosition | None = None,
        before_ordinal: int | None = None,
        limit: int = 64,
        segment_payload: str = "full",
        tool_payload: str = "full",
        provider_replay_payload: str = "full",
        max_projection_bytes: int = 0,
    ) -> dict[str, Any]:
        """Return one bounded lane page at the newest head the caller has not passed.

        Every streamed attempt event advances the sync head and the caller's
        replayed head trails it by the stream latency, so an exact-head read
        fails almost every time while a reply streams. Storage still reads at
        one exact head; a stale read is repeated at the head storage reported,
        and ``syncSeq`` names the head the page is exact for. The caller
        publishes the page once its own replay reaches that head. Only a head
        behind the caller, or one that keeps moving, remains stale.

        ``position`` is the caller's replay position at
        ``expected_sync_sequence``, decoded from the cursor it holds there.
        Each read then reports the digest of the change now at that sequence,
        and the page answers only in the caller's lineage, decided as for a
        change page. A replaced or unprovable change there, or a head behind
        the caller, is stale without a head: no head of this lineage extends
        the caller's replica, which repairs from one snapshot. A caller without
        a position (a browser loaded before lineage) keeps the sequence fence.
        """
        if tool_payload not in {"full", "lazy"} or (tool_payload == "lazy" and segment_payload != "refs"):
            raise ValueError("Lazy tool payload requires reference segments")
        _require_provider_replay_payload(provider_replay_payload, segment_payload)
        if segment_payload not in {"full", "refs"}:
            raise ValueError("Invalid conversation segment payload")
        anchor = (
            {"anchor_sequence": position.sequence}
            if position is not None and position.sequence else {}
        )
        sequence = expected_sync_sequence
        for _attempt in range(STREAMING_HEAD_READ_ATTEMPTS):
            stored = self._repository.turn_page(
                conversation_id,
                user_id,
                lane_id=lane_id,
                expected_sync_sequence=sequence,
                before_ordinal=before_ordinal,
                limit=limit,

                **({"max_projection_bytes": max_projection_bytes} if max_projection_bytes else {}),
                **anchor,
            )
            if stored is None:
                raise ConversationSyncNotFound("Conversation not found")
            if stored.get("stale") is not True:
                break
            head = int(stored.get("syncSequence") or 0)
            if head < expected_sync_sequence:
                if position is not None:
                    # As for a change page: a cursor beyond the head is invalid.
                    raise ConversationTurnPageStale(None, reason="cursor_invalid")
                raise ConversationTurnPageStale(head)
            sequence = head
        else:
            raise ConversationTurnPageStale(sequence)
        sequence = int(stored.get("syncSequence") or 0)
        epoch, head_digest = _stored_lineage(stored, operation="turn.sync.page")
        if position is not None:
            reason = _lineage_reset_reason(
                position, stored, epoch, operation="turn.sync.page")
            if reason is not None:
                raise ConversationTurnPageStale(None, reason=reason)
        response = {
            "ok": True,
            "contract": "tofu.conversation-sync.turn-page/v1",
            "conversationId": conversation_id,
            "conversationRevision": int(
                stored.get("conversationRevision") or 0
            ),
            "syncSeq": sequence,
            "cursor": encode_cursor(
                conversation_id, user_id, ReplayPosition(sequence, epoch, head_digest)),
            "laneId": str(stored.get("laneId") or lane_id),
            "beforeOrdinal": int(stored.get("beforeOrdinal") or 0),
            "nextBeforeOrdinal": stored.get("nextBeforeOrdinal"),
            "hasMore": bool(stored.get("hasMore")),
            "totalTurns": int(stored.get("totalTurns") or 0),
            "turns": public_value_with_stable_segments(
                [
                    adapt_turn_read(turn)
                    for turn in list(stored.get("turns") or [])
                ]
            ),
            "attempts": list(stored.get("attempts") or []),
        }
        if segment_payload == "refs":
            response = snapshot_with_reference_tool_segments(
                response,
                lazy_tool_results=tool_payload == "lazy",
                provider_replay=provider_replay_payload,
                owner_cache_scope=turn_image_owner_scope(
                    user_id, conversation_id
                ),
            )
        return decode("ConversationTurnPage", response)

    def turn_read(self, conversation_id: str, turn_id: str, user_id: int) -> dict[str, Any]:
        """Return a complete owner-scoped projection for an explicit edit."""
        stored = self._repository.turn(conversation_id, turn_id, user_id)
        if stored is None:
            raise ConversationSyncNotFound("Conversation Turn not found")
        if not stored.get("presentationId"):
            stored = {**stored, "presentationId": str(stored.get("turnId") or "")}
        turn = public_value_with_stable_segments([adapt_turn_read(stored)])[0]
        return decode("ConversationTurnRead", {
            "ok": True, "contract": "tofu.conversation-sync.turn-read/v1", "turn": turn,
        })

    def inspect_tool_effect(self, conversation_id: str, turn_id: str, user_id: int,
                            *, operation_id: str, producer_task_id: str | None = None) -> dict[str, Any]:
        """Inspect domain evidence only after an owner-scoped occurrence lookup."""
        from .tool_effects import inspect_tool_effect
        stored = self._repository.turn(conversation_id, turn_id, user_id)
        if stored is None:
            raise ConversationSyncNotFound("Tool occurrence not found")
        outcome, reason = inspect_tool_effect(
            stored, conversation_id=conversation_id, operation_id=operation_id,
            producer_task_id=producer_task_id)
        return decode("ConversationToolEffectInspection", {
            "ok": True, "contract": "tofu.conversation-sync.tool-effect-inspection/v1",
            "conversationId": conversation_id, "turnId": turn_id,
            "operationId": operation_id, "outcome": outcome, "reason": reason,
        })

    def turn_tool_result(
        self, conversation_id: str, turn_id: str, user_id: int,
        *, round_index: int, projection_revision: int,
    ) -> dict[str, Any]:
        """Read immutable detail through the existing owner-scoped Turn repository."""
        from .tool_results import tool_result_from_turn
        stored = self._repository.turn(conversation_id, turn_id, user_id)
        if stored is None:
            raise ConversationSyncNotFound("Conversation tool result not found")
        return decode("ConversationTurnToolResult", tool_result_from_turn(
            stored, conversation_id=conversation_id, turn_id=turn_id,
            round_index=round_index, projection_revision=projection_revision,
        ))

    def turn_image(
        self,
        conversation_id: str,
        turn_id: str,
        user_id: int,
        *,
        projection_revision: int,
        image_index: int,
    ) -> ConversationTurnImage:
        """Load and verify one immutable owner-scoped historical image."""
        if (
            not isinstance(conversation_id, str)
            or not conversation_id
            or len(conversation_id) > 256
            or not isinstance(turn_id, str)
            or not turn_id
            or len(turn_id) > 128
            or not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id < 1
            or not isinstance(projection_revision, int)
            or isinstance(projection_revision, bool)
            or not 1 <= projection_revision <= 2**63 - 1
            or not isinstance(image_index, int)
            or isinstance(image_index, bool)
            or not 0 <= image_index < MAX_TURN_IMAGES
        ):
            raise ValueError("Invalid conversation Turn image request")
        stored = self._repository.turn_image(
            conversation_id,
            turn_id,
            user_id,
            projection_revision=projection_revision,
            image_index=image_index,
        )
        if stored is None:
            raise ConversationTurnImageNotFound("Conversation Turn image not found")
        if stored.get("stale") is True:
            raise ConversationTurnImageStale(
                int(stored.get("projectionRevision") or 0)
            )
        return decode_stored_turn_image(stored.get("base64"))

    def position_from_cursor(
        self, conversation_id: str, user_id: int, cursor: str | None
    ) -> ReplayPosition:
        return decode_cursor(conversation_id, user_id, cursor)

    def changes(
        self,
        conversation_id: str,
        user_id: int,
        *,
        after: ReplayPosition,
        limit: int = 500,
        diagnostic_payload: str = "full",
        provider_replay_payload: str = "full",
        max_bytes: int | None = None,
        cancellation=None,
    ) -> dict[str, Any]:
        """Replay after a position, or the reset its lineage requires.

        ``cursors`` names each returned event; ``position`` and ``cursor``
        name the position after this page, verified in the read's authority
        open; ``headCursor`` names the head. The stream passes ``position``
        to its next page read, so each read re-verifies the lineage.
        """
        if diagnostic_payload not in {"full", "none"}:
            raise ValueError("Invalid conversation diagnostic payload")
        _require_provider_replay_payload(provider_replay_payload)
        # Only stream page reads carry these; other repositories take neither.
        page_options = {key: value for key, value in (
            ("max_bytes", max_bytes), ("cancellation", cancellation))
            if value is not None}
        stored = self._repository.changes(
            conversation_id,
            user_id,
            after_sequence=after.sequence,
            limit=limit,
            **page_options,
        )
        if stored is None:
            raise ConversationSyncNotFound("Conversation not found")
        head = int(stored.get("head") or 0)
        epoch, head_digest = _stored_lineage(stored, operation="turn.sync.changes")
        head_cursor = encode_cursor(
            conversation_id, user_id, ReplayPosition(head, epoch, head_digest))
        reason = _lineage_reset_reason(after, stored, epoch, operation="turn.sync.changes")
        if reason is not None:
            return {
                "head": head, "events": [], "cursors": [], "position": after,
                "cursor": head_cursor, "headCursor": head_cursor,
                "reset": self.reset_required(
                    conversation_id, user_id, head_cursor, reason=reason),
                "hasMore": False,
            }
        events = list(stored.get("events") or [])
        digests = stored.get(_LINEAGE["changeDigestsField"])
        if (not isinstance(digests, list) or len(digests) != len(events)
                or not all(is_lineage_value(digest) for digest in digests)):
            raise StorageError(
                "database_protocol_error",
                "turn.sync.changes result does not name each change's lineage",
            )
        positions = [
            ReplayPosition(int(event.get("syncSeq") or 0), epoch, digest)
            for event, digest in zip(events, digests)
        ]
        if diagnostic_payload == "none":
            events = [_without_diagnostic_body(event) for event in events]
        else:
            events = [_with_referenced_evidence(event, self._repository) for event in events]
        events = [public_change_with_stable_segments(event) for event in events]
        if provider_replay_payload == "none":
            events = [change_without_provider_replay(event) for event in events]
        for event in events:
            decode("ConversationChange", event)
        if positions:
            position = positions[-1]
        else:
            anchor = stored.get(_LINEAGE["anchorDigestField"])
            position = ReplayPosition(
                after.sequence, epoch, anchor if is_lineage_value(anchor) else after.digest)
        return {
            "head": head,
            "events": events,
            "cursors": [
                encode_cursor(conversation_id, user_id, item) for item in positions
            ],
            "position": position,
            "cursor": encode_cursor(conversation_id, user_id, position),
            "headCursor": head_cursor,
            "reset": None,
            "hasMore": bool(stored.get("hasMore")),
        }

    def reset_required(
        self, conversation_id: str, user_id: int, cursor: str, *, reason: str,
    ) -> dict[str, Any]:
        """Require one authoritative snapshot instead of replay from a cursor."""
        return decode("SyncResetRequired", {
            "contract": "tofu.conversation-sync.event/v1",
            "type": "sync.reset_required",
            "conversationId": conversation_id,
            "cursor": cursor,
            "reason": reason,
        })

    def heartbeat(
        self,
        conversation_id: str,
        user_id: int,
        cursor: str,
        *,
        degraded: bool,
        push_withheld: bool = False,
    ) -> dict[str, Any]:
        """Confirm liveness at the stream's last delivered cursor, verbatim."""
        heartbeat = {
            "contract": "tofu.conversation-sync.event/v1",
            "type": "sync.heartbeat",
            "conversationId": conversation_id,
            "cursor": cursor,
            "serverBootId": BOOT_ID,
            "degraded": bool(degraded),
            # Always explicit: the post-wedge heartbeat is what clears the
            # client's wedge status back to the normal phase row.
            "pushWithheld": bool(push_withheld),
        }
        return decode("SyncHeartbeat", heartbeat)


__all__ = [
    "ConversationCursorError",
    "ConversationSyncNotFound",
    "ConversationTurnPageStale",
    "ConversationSyncService",
]
