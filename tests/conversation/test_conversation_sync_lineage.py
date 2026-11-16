"""Replay cursors name their change's lineage; the service fences any other.

Storage reports, from the read transaction of each page, the authority open's
epoch and the digest of each stored change (contracts/storage_v2.json
``conversationSyncLineage``). ConversationSyncService decides whether a cursor
still names the change the authority holds at its sequence. The storage-backed
end-to-end specification on both authorities is
``test_conversation_sync_cursor_identity.py``; these cover every decision
without a store.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from lib.conversation_sync.cursor import (
    STREAM_START,
    ConversationCursorError,
    ReplayPosition,
    decode_cursor,
    decode_sequence_hint,
    encode_cursor,
    encode_sequence_hint,
)
from lib.conversation_sync.service import ConversationSyncService
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit

CONVERSATION = "conv-lineage"
OWNER = 7
EPOCH = "00000000000000a1"
RESTARTED = "00000000000000b2"


def _digest(sequence: int, lineage: str = "a") -> str:
    return f"{lineage}{sequence:015x}"


def _change(sequence: int) -> dict:
    return {
        "contract": "tofu.conversation-sync.event/v1", "type": "conversation.activity",
        "conversationId": CONVERSATION, "syncSeq": sequence, "occurredAt": sequence,
        "payload": {},
    }


def _page(after: int, head: int, *, epoch: str = EPOCH, anchor: str | None = "same",
          lineage: str = "a", **extra) -> dict:
    """One authority result: the changes after ``after`` up to ``head``."""
    sequences = range(after + 1, head + 1)
    page = {
        "head": head, "events": [_change(sequence) for sequence in sequences],
        "resetRequired": False, "hasMore": False, "authorityEpoch": epoch,
        "headDigest": _digest(head, lineage) if head else None,
        "changeDigests": [_digest(sequence, lineage) for sequence in sequences],
    }
    if after:
        page["anchorDigest"] = _digest(after, lineage) if anchor == "same" else anchor
    page.update(extra)
    return page


class _Authority:
    """Serves prepared results in order and records each requested sequence."""

    def __init__(self, *results):
        self.results = list(results)
        self.reads = []

    def changes(self, conversation_id, user_id, *, after_sequence, limit, **_options):
        assert (conversation_id, user_id) == (CONVERSATION, OWNER)
        self.reads.append(after_sequence)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _cursor(sequence: int, epoch: str = EPOCH, digest: str | None = None) -> str:
    return encode_cursor(CONVERSATION, OWNER, ReplayPosition(
        sequence, epoch, _digest(sequence) if digest is None else digest))


def _changes(authority: _Authority, cursor: str | None) -> dict:
    service = ConversationSyncService(authority)
    return service.changes(
        CONVERSATION, OWNER, after=service.position_from_cursor(CONVERSATION, OWNER, cursor))


def test_a_cursor_round_trips_its_lineage_within_its_scope():
    position = ReplayPosition(1_000_000, EPOCH, _digest(1_000_000))
    cursor = encode_cursor(CONVERSATION, OWNER, position)

    assert decode_cursor(CONVERSATION, OWNER, cursor) == position
    assert len(cursor) <= 128
    for scope in ((CONVERSATION, OWNER + 1), ("other", OWNER)):
        with pytest.raises(ConversationCursorError):
            decode_cursor(*scope, cursor)
    unverified = ReplayPosition(5, EPOCH, "")
    assert decode_cursor(CONVERSATION, OWNER, encode_cursor(
        CONVERSATION, OWNER, unverified)) == unverified
    assert decode_cursor(CONVERSATION, OWNER, None) == STREAM_START
    with pytest.raises(ValueError):
        encode_cursor(CONVERSATION, OWNER, ReplayPosition(5))
    with pytest.raises(ConversationCursorError):
        decode_cursor(CONVERSATION, OWNER, cursor.replace(EPOCH, "not-an-epoch"))


def test_invalidation_hints_carry_no_lineage_and_read_either_form():
    hint = encode_sequence_hint(CONVERSATION, OWNER, 42)

    assert hint.startswith("c1.")
    assert decode_sequence_hint(CONVERSATION, OWNER, hint) == 42
    assert decode_sequence_hint(CONVERSATION, OWNER, _cursor(42)) == 42


def test_a_reconnect_in_the_same_lineage_resumes_and_names_each_change():
    authority = _Authority(_page(2, 4))

    page = _changes(authority, _cursor(2))

    assert page["reset"] is None
    assert authority.reads == [2]
    assert [event["syncSeq"] for event in page["events"]] == [3, 4]
    assert page["cursors"] == [_cursor(3), _cursor(4)]
    assert page["position"] == ReplayPosition(4, EPOCH, _digest(4))
    assert page["headCursor"] == _cursor(4)


def test_a_clean_restart_resumes_and_the_stream_continues_in_the_new_epoch():
    page = _changes(_Authority(_page(2, 3, epoch=RESTARTED)), _cursor(2))

    assert page["reset"] is None
    assert page["cursors"] == [_cursor(3, RESTARTED)]

    caught_up = _changes(_Authority(_page(2, 2, epoch=RESTARTED)), _cursor(2))
    assert caught_up["reset"] is None and caught_up["events"] == []
    assert caught_up["position"] == ReplayPosition(2, RESTARTED, _digest(2))


def test_a_sequence_that_recovery_gave_to_another_change_resets():
    page = _changes(_Authority(_page(2, 3, epoch=RESTARTED, lineage="b")), _cursor(2))

    assert page["events"] == [] and page["cursors"] == []
    assert page["reset"]["type"] == "sync.reset_required"
    assert page["reset"]["reason"] == "cursor_invalid"
    assert page["reset"]["cursor"] == _cursor(3, RESTARTED, _digest(3, "b"))


def test_a_change_digest_is_verified_in_its_own_epoch_too():
    page = _changes(_Authority(_page(2, 3, anchor=_digest(2, "f"))), _cursor(2))

    assert page["reset"]["reason"] == "cursor_invalid"


@pytest.mark.parametrize(("epoch", "reset"), [(EPOCH, None), (RESTARTED, "cursor_expired")])
def test_a_change_past_retention_resumes_only_in_the_open_that_minted_its_cursor(epoch, reset):
    page = _changes(_Authority(_page(2, 3, epoch=epoch, anchor=None)), _cursor(2))

    assert (page["reset"] or {}).get("reason") == reset


def test_a_cursor_beyond_the_head_keeps_the_authority_verdict():
    authority = _Authority({
        "head": 1, "events": [], "resetRequired": True, "resetReason": "cursor_invalid",
        "authorityEpoch": RESTARTED, "headDigest": _digest(1, "b")})

    page = _changes(authority, _cursor(2))

    assert page["reset"]["reason"] == "cursor_invalid"
    assert page["reset"]["cursor"] == _cursor(1, RESTARTED, _digest(1, "b"))


def test_a_cursor_issued_before_lineage_resets_once_and_only_past_the_start():
    legacy = encode_sequence_hint(CONVERSATION, OWNER, 2)

    page = _changes(_Authority(_page(2, 3)), legacy)
    assert page["reset"]["reason"] == "protocol_mismatch"
    assert page["reset"]["cursor"] == _cursor(3)

    started = _changes(_Authority(_page(0, 1)), encode_sequence_hint(CONVERSATION, OWNER, 0))
    assert started["reset"] is None
    assert started["cursors"] == [_cursor(1)]


@pytest.mark.parametrize("missing", ["authorityEpoch", "headDigest", "changeDigests", "anchorDigest"])
def test_an_authority_result_without_lineage_fails_closed(missing):
    result = _page(2, 3)
    del result[missing]

    with pytest.raises(StorageError) as raised:
        _changes(_Authority(result), _cursor(2))
    assert raised.value.code == "database_protocol_error"


def test_a_heartbeat_repeats_the_delivered_cursor_verbatim():
    service = ConversationSyncService(_Authority())
    held = _cursor(9)

    assert service.heartbeat(CONVERSATION, OWNER, held, degraded=False)["cursor"] == held


def test_a_stream_opened_without_a_cursor_confirms_the_start_until_it_delivers(monkeypatch):
    """The ownership probe reads a page; its position was never delivered."""
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    import routes.conversation_sync_v3 as routes

    authority = _Authority(
        _page(0, 1),
        StorageError("database_timeout", "deadline_elapsed"),
    )
    monkeypatch.setattr(routes, "_service", ConversationSyncService(authority))
    monkeypatch.setattr(routes, "sse_response", lambda body, **_kwargs: body)
    monkeypatch.setattr(routes, "push_withheld_for_conv", lambda _conversation_id: None)
    monkeypatch.setattr(routes, "_request_user_id", lambda: OWNER)
    path = (f"/api/v4/conversations/{CONVERSATION}/events"
            "?streamClientId=lineage&streamGeneration=1")

    async def first_frame():
        async with Quart(__name__).test_request_context(path):
            g.auth_ctx = local_admin_context()
            g.rate_decision = None
            stream = await routes.conversation_sync_events(CONVERSATION)
            try:
                encoded = await anext(stream)
            finally:
                await stream.aclose()
        return dict(line.split(": ", 1) for line in encoded.strip().split("\n"))

    frame = asyncio.run(first_frame())

    assert authority.reads == [0, 0]
    heartbeat = json.loads(frame["data"])
    assert (frame["event"], heartbeat["degraded"]) == ("sync.heartbeat", True)
    assert frame["id"] == heartbeat["cursor"] != _cursor(1)
    assert decode_cursor(CONVERSATION, OWNER, frame["id"]) == STREAM_START


class _Pages:
    """Serves prepared turn.sync.page results and records each read's payload."""

    def __init__(self, *results):
        self.results = list(results)
        self.reads = []

    def turn_page(self, conversation_id, user_id, **request):
        assert (conversation_id, user_id) == (CONVERSATION, OWNER)
        self.reads.append(request)
        return self.results.pop(0)


def _history(head: int, *, epoch: str = EPOCH, lineage: str = "a", **anchor) -> dict:
    """One exact history page at ``head``, with the anchor digest if named."""
    page = {
        "conversationId": CONVERSATION, "conversationRevision": head, "syncSequence": head,
        "laneId": "main", "beforeOrdinal": 9, "nextBeforeOrdinal": None, "hasMore": False,
        "totalTurns": 0, "turns": [], "attempts": [], "authorityEpoch": epoch,
        "headDigest": _digest(head, lineage),
    }
    page.update(anchor)
    return page


def _turn_page(pages: _Pages, cursor: str | None, sequence: int) -> dict:
    service = ConversationSyncService(pages)
    position = None if cursor is None else service.position_from_cursor(
        CONVERSATION, OWNER, cursor)
    return service.turn_page(
        CONVERSATION, OWNER, lane_id="main", expected_sync_sequence=sequence,
        position=position, before_ordinal=9, limit=8)


def test_a_history_page_of_the_reader_lineage_answers_at_the_newest_head():
    pages = _Pages({"stale": True, "syncSequence": 4},
                   _history(4, epoch=RESTARTED, anchorDigest=_digest(2)))

    page = _turn_page(pages, _cursor(2), 2)

    assert page["syncSeq"] == 4
    assert page["cursor"] == _cursor(4, RESTARTED)
    assert [read["anchor_sequence"] for read in pages.reads] == [2, 2]
    assert [read["expected_sync_sequence"] for read in pages.reads] == [2, 4]


_AHEAD = {"stale": True, "syncSequence": 3}


@pytest.mark.parametrize(("results", "reason"), [
    # Recovery gave the reader's head to another change.
    ([_history(2, epoch=RESTARTED, lineage="b", anchorDigest=_digest(2, "b"))],
     "cursor_invalid"),
    # The same, read at a newer head, also within one open.
    ([_AHEAD, _history(3, anchorDigest=_digest(2, "f"))], "cursor_invalid"),
    # The reader's change left retention and another open cannot prove it.
    ([_AHEAD, _history(3, epoch=RESTARTED, anchorDigest=None)], "cursor_expired"),
    # The authority's head is behind the reader: its changes were lost.
    ([{"stale": True, "syncSequence": 1}], "cursor_invalid"),
])
def test_a_history_page_of_another_lineage_is_stale_without_a_head(results, reason):
    from lib.conversation_sync.service import ConversationTurnPageStale

    with pytest.raises(ConversationTurnPageStale) as raised:
        _turn_page(_Pages(*results), _cursor(2), 2)

    assert raised.value.current_sync_sequence is None
    assert raised.value.reason == reason


def test_a_history_cursor_past_retention_answers_in_the_open_that_minted_it():
    page = _turn_page(_Pages(_AHEAD, _history(3, anchorDigest=None)), _cursor(2), 2)

    assert page["syncSeq"] == 3


def test_a_history_cursor_issued_before_lineage_repairs_once_past_the_start():
    from lib.conversation_sync.service import ConversationTurnPageStale

    with pytest.raises(ConversationTurnPageStale) as raised:
        _turn_page(_Pages(_history(2)), encode_sequence_hint(CONVERSATION, OWNER, 2), 2)
    assert raised.value.reason == "protocol_mismatch"

    started = _Pages(_history(0, headDigest=None))
    assert _turn_page(started, encode_sequence_hint(CONVERSATION, OWNER, 0), 0)["syncSeq"] == 0
    assert "anchor_sequence" not in started.reads[0]


def test_a_history_read_without_a_cursor_keeps_the_sequence_fence():
    from lib.conversation_sync.service import ConversationTurnPageStale

    pages = _Pages(_history(2, epoch=RESTARTED, lineage="b"))
    assert _turn_page(pages, None, 2)["syncSeq"] == 2
    assert "anchor_sequence" not in pages.reads[0]

    with pytest.raises(ConversationTurnPageStale) as behind:
        _turn_page(_Pages({"stale": True, "syncSequence": 1}), None, 2)
    assert (behind.value.current_sync_sequence, behind.value.reason) == (1, None)


def test_a_history_page_without_the_anchor_it_was_asked_for_fails_closed():
    with pytest.raises(StorageError) as raised:
        _turn_page(_Pages(_AHEAD, _history(3)), _cursor(2), 2)

    assert raised.value.code == "database_protocol_error"


def test_a_live_stream_resets_when_its_restarted_authority_reused_its_position(monkeypatch):
    """A storage restart under a live API host is fenced, not only a reconnect."""
    import routes.conversation_sync_v3 as routes

    authority = _Authority(
        _page(0, 2),
        # The authority restarted: sequence 2 now names another change.
        _page(2, 3, epoch=RESTARTED, lineage="b"),
    )
    monkeypatch.setattr(routes, "_service", ConversationSyncService(authority))

    class Subscription:
        closed = False
        close_reason = ""
        wakes = 1

        def add_close_callback(self, _callback):
            pass

        async def wait(self, _timeout):
            self.wakes -= 1
            return self.wakes >= 0

    async def stream():
        frames = routes._conversation_change_frames(
            CONVERSATION, OWNER, Subscription(), after=STREAM_START,
            delivered_cursor="", catch_up_budget=None, diagnostic_options={},
            frame=lambda event, cursor: (event["type"], cursor, event.get("reason")))
        return [frame async for frame in frames]

    frames = asyncio.run(stream())

    assert authority.reads == [0, 2]
    assert frames[:2] == [("conversation.activity", _cursor(1), None),
                          ("conversation.activity", _cursor(2), None)]
    assert frames[-1] == ("sync.reset_required", _cursor(3, RESTARTED, _digest(3, "b")),
                          "cursor_invalid")
