"""A change page's projection work is proportional to the Turn records it carries.

2026-11-13 API-host stalls (5.0-5.5 s, /tmp/fd1_stalls): ``changes()`` ran the
generic public-value walk over every replayed event. It read and rebuilt every
nested projection-patch operation, attempt and diagnostic body while holding
the GIL, so one catch-up page cost the size of everything it carried. The
change contract places TurnRecords only at ``payload.turns`` and, for
``attempt.event``, at ``payload.event.payload.turns``; nothing else needs a
read-side projection.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

import lib.turn_projection_segments as projection_segments
from lib.conversation_sync.cursor import STREAM_START
from lib.conversation_sync.service import ConversationSyncService
from lib.turn_projection_segments import public_value_with_stable_segments
from tests.support.conversation_replay import with_lineage


pytestmark = pytest.mark.unit

CONVERSATION = "conversation-a"
USER = 7


class _Untouchable(dict):
    """Patch/attempt/diagnostic content the projection must never read."""

    reads = 0

    def _read(self):
        type(self).reads += 1

    def __iter__(self):
        self._read()
        return super().__iter__()

    def __getitem__(self, key):
        self._read()
        return super().__getitem__(key)

    def __contains__(self, key):
        self._read()
        return super().__contains__(key)

    def get(self, key, default=None):
        self._read()
        return super().get(key, default)

    def items(self):
        self._read()
        return super().items()

    def keys(self):
        self._read()
        return super().keys()

    def values(self):
        self._read()
        return super().values()


def _turn(turn_id: str, status: str) -> dict:
    return {
        "turnId": turn_id, "presentationId": turn_id,
        "conversationId": CONVERSATION, "laneId": "main", "parentTurnId": None,
        "ordinal": 1, "actor": "assistant", "kind": "reply", "runId": "run",
        "status": status, "currentAttemptId": None,
        "projection": {"content": f"answer {turn_id}", "thinking": "",
                       "toolRounds": []},
        "projectionRevision": 3, "modelContextRevision": 3,
        "settlement": {}, "createdAt": 1, "updatedAt": 2,
    }


def _operations(count: int) -> list:
    return [
        _Untouchable(op="append", path=["toolRounds"], value=[_Untouchable(
            toolCallId=f"call-{index}", toolName="read_files",
            toolArgs=_Untouchable(path=f"lib/mod_{index}.py"),
            toolContent="x" * 64, results=[_Untouchable(meta=_Untouchable(lines=1))],
        )])
        for index in range(count)
    ]


def _attempt_event(sequence: int, *, operations: int, turns=(), diagnostic=None) -> dict:
    body = {
        "projectionPatch": _Untouchable(
            version=1, baseRevision=sequence - 1, targetRevision=sequence,
            operations=_operations(operations)),
        "status": "running",
        "attempts": [_Untouchable(attemptId="attempt-a", resumeAnchor=_Untouchable(
            segments=[_Untouchable(type="text", text="resume")]))],
    }
    if turns:
        body["turns"] = list(turns)
    if diagnostic is not None:
        body["event"] = diagnostic
    return {
        "contract": "tofu.conversation-sync.event/v1", "type": "attempt.event",
        "conversationId": CONVERSATION, "syncSeq": sequence, "occurredAt": sequence,
        "turnId": "turn-live", "attemptId": "attempt-a",
        "payload": {"event": {
            "conversationId": CONVERSATION, "turnId": "turn-live",
            "attemptId": "attempt-a", "seq": sequence, "projectionRevision": sequence,
            "type": "projection_updated", "payload": body,
        }},
    }


def _turn_upsert(sequence: int, turns) -> dict:
    return {
        "contract": "tofu.conversation-sync.event/v1", "type": "turn.upsert",
        "conversationId": CONVERSATION, "syncSeq": sequence, "occurredAt": sequence,
        "turnId": "turn-live",
        "payload": {
            "turns": list(turns),
            "attempts": [_Untouchable(attemptId="attempt-a", resumeAnchor=_Untouchable(
                prompt="resume"))],
            "queueItemUpserts": [_Untouchable(queueId="queue-a")],
            "conversationRevision": sequence,
        },
    }


def _turn_patch(sequence: int, operations: int) -> dict:
    return {
        "contract": "tofu.conversation-sync.event/v1", "type": "turn.patch",
        "conversationId": CONVERSATION, "syncSeq": sequence, "occurredAt": sequence,
        "turnId": "turn-old",
        "payload": {
            "turnPatches": [_Untouchable(
                turnId="turn-old", baseProjectionRevision=1,
                targetProjectionRevision=2, updatedAt=sequence,
                projectionPatch=_Untouchable(
                    version=1, baseRevision=1, targetRevision=2,
                    operations=_operations(operations)))],
            "conversationRevision": sequence,
        },
    }


class _Repository:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def changes(self, conversation_id, user_id, *, after_sequence, limit):
        page = [event for event in self.events
                if event["syncSeq"] > after_sequence][:limit]
        return with_lineage({"head": self.events[-1]["syncSeq"], "events": page,
                             "hasMore": False}, after_sequence)


@pytest.fixture()
def stabilized(monkeypatch):
    """Count stable-segment derivations, the per-Turn projection unit."""
    calls = []
    derive = projection_segments.projection_with_stable_segments

    def counted(projection, **kwargs):
        calls.append(kwargs.get("status"))
        return derive(projection, **kwargs)

    monkeypatch.setattr(
        projection_segments, "projection_with_stable_segments", counted)
    _Untouchable.reads = 0
    return calls


def _page(events, **options) -> list[dict]:
    service = ConversationSyncService(_Repository(events))
    return service.changes(
        CONVERSATION, USER, after=STREAM_START, limit=500, **options)["events"]


@pytest.mark.parametrize("diagnostic_payload", ["full", "none"])
def test_change_page_never_reads_patches_attempts_or_diagnostic_bodies(
    stabilized, diagnostic_payload,
):
    diagnostic = _Untouchable(type="messages_snapshot", messages=[_Untouchable(
        role="user", content=[_Untouchable(type="text", text="history")])])
    events = [
        _attempt_event(1, operations=40, diagnostic=diagnostic),
        _turn_upsert(2, [_turn("turn-new", "pending")]),
        _turn_patch(3, operations=40),
        _attempt_event(4, operations=1),
    ]

    page = _page(events, diagnostic_payload=diagnostic_payload)

    assert [event["syncSeq"] for event in page] == [1, 2, 3, 4]
    assert _Untouchable.reads == 0
    assert stabilized == []


def test_projection_work_does_not_grow_with_patch_size(stabilized):
    small = _page([_attempt_event(1, operations=1)])
    small_reads = _Untouchable.reads
    large = _page([_attempt_event(1, operations=2_000)])

    assert small_reads == _Untouchable.reads == 0
    assert len(large[0]["payload"]["event"]["payload"]["projectionPatch"][
        "operations"]) == 2_000
    assert len(small) == len(large) == 1


def test_a_change_without_settled_turns_is_shared_not_copied(stabilized):
    live = _turn("turn-live", "running")
    events = [
        _attempt_event(1, operations=3, turns=[live]),
        _turn_upsert(2, [_turn("turn-new", "pending")]),
        _turn_patch(3, operations=3),
    ]

    page = _page(events)

    assert all(projected is source for projected, source in zip(page, events))
    assert stabilized == []


def test_stable_segments_run_once_per_settled_turn_record(stabilized):
    settled_upsert = _turn("turn-a", "completed")
    settled_related = _turn("turn-b", "failed")
    live = _turn("turn-live", "running")
    events = [
        _turn_upsert(1, [_turn("turn-new", "pending"), settled_upsert]),
        _attempt_event(2, operations=5, turns=[settled_related, live]),
    ]
    before = deepcopy(events)
    _Untouchable.reads = 0

    page = _page(events)

    assert sorted(stabilized) == ["completed", "failed"]
    upserted = page[0]["payload"]["turns"]
    related = page[1]["payload"]["event"]["payload"]["turns"]
    assert upserted[1]["projection"]["segments"]
    assert related[0]["projection"]["segments"]
    # Live Turns stay exact positional-patch bases; untouched values are shared.
    assert related[1] is live
    assert upserted[0] is events[0]["payload"]["turns"][0]
    assert (page[1]["payload"]["event"]["payload"]["projectionPatch"]
            is events[1]["payload"]["event"]["payload"]["projectionPatch"])
    assert events == before
    assert _Untouchable.reads == 0


def test_declared_turn_projection_equals_the_generic_public_walk(stabilized):
    events = [
        _turn_upsert(1, [_turn("turn-new", "pending"), _turn("turn-a", "completed")]),
        _attempt_event(2, operations=2, turns=[_turn("turn-b", "interrupted"),
                                               _turn("turn-live", "running")]),
        _turn_patch(3, operations=2),
    ]
    expected = public_value_with_stable_segments(deepcopy(events))

    assert _page(deepcopy(events)) == expected
