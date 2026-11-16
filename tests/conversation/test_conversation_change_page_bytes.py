"""Change pages are bounded by the subscription's window bytes.

A catch-up page used to hold up to ``limit`` (500) changes whatever their size.
Every settled ``turn.upsert`` carries its whole TurnRecord, so on 2026-11-13 a
lane API host decoded ~17 MB page responses for several streams at once:
184 MB of decoded objects in flight, RSS 372 MB -> 2.97 GB in ten minutes and
full collections over the in-flight graph (the production 5 s stalls).
``turn.sync.changes`` now takes ``max_bytes``: changes stay in order while
their encoded bytes (each change's compact UTF-8 JSON, exactly as the response
carries it) total at most ``max_bytes``; one larger change is returned alone.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import orjson
import pytest

from lib.conversation_sync.cursor import STREAM_START
from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.operations_pkg._common import _dump
from lib.storage_authority_sql.schema import initialize_schema


pytestmark = pytest.mark.unit

CONVERSATION = "conversation"


@pytest.fixture()
def session(tmp_path):
    connection = sqlite3.connect(tmp_path / "change-pages.db")
    connection.row_factory = sqlite3.Row
    # A backend open's identity; sync reads report the lineage epoch it derives.
    authority = SQLiteSession(connection, event_read_epoch="change-pages-open")
    initialize_schema(authority)
    authority.execute(
        "INSERT INTO storage_conversations("
        "id,user_id,title,messages_json,created_at_ms,updated_at_ms,"
        "settings_json,msg_count,search_text,rev) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (CONVERSATION, 1, "pages", _dump([]), 1, 1, _dump({}), 0, "", 1),
    )
    try:
        yield authority
    finally:
        connection.close()


def _append(session, size: int, index: int) -> None:
    from lib.storage_authority_sql.operations_pkg._turns_core import (
        _append_conversation_change,
    )

    # Multi-byte text: the bound is UTF-8 bytes, not characters.
    _append_conversation_change(
        session, conversation_id=CONVERSATION, user_id=1,
        change_type="conversation.activity",
        payload={"index": index, "text": "版" * (size // 3)},
    )


def _seed(session, sizes) -> None:
    for index, size in enumerate(sizes):
        _append(session, size, index)


def _encoded(event) -> int:
    return len(orjson.dumps(event))


def _page(session, after: int, **options):
    from lib.storage_authority_sql.operations_pkg._turns_read import _turn_sync_changes

    return _turn_sync_changes(session, {
        "conversation_id": CONVERSATION, "user_id": 1, "after": after, **options})


def _sequences(page) -> list[int]:
    return [int(event["syncSeq"]) for event in page["events"]]


def _expected_boundaries(events, max_bytes: int) -> list[int]:
    """Greedy longest prefixes by encoded size, never an empty page."""
    boundaries, used, count, last = [], 0, 0, 0
    for event in events:
        size = _encoded(event)
        if count and used + size > max_bytes:
            boundaries.append(last)
            used, count = 0, 0
        used, count, last = used + size, count + 1, int(event["syncSeq"])
    return boundaries + [last]


def _catch_up(session, **options):
    pages, after = [], 0
    while True:
        page = _page(session, after, **options)
        pages.append(page)
        if not page["events"] or not page["hasMore"]:
            return pages
        after = _sequences(page)[-1]


def test_a_page_stays_within_max_bytes(session):
    _seed(session, [40_000] * 12)

    page = _page(session, 0, max_bytes=100_000)

    assert _sequences(page) == [1, 2]
    assert sum(_encoded(event) for event in page["events"]) <= 100_000
    assert page["hasMore"] is True
    assert page["maxBytesApplied"] is True


def test_a_single_oversized_change_is_a_page_of_exactly_one(session):
    _seed(session, [300_000, 1_000, 1_000])

    first = _page(session, 0, max_bytes=65_536)
    second = _page(session, 1, max_bytes=65_536)

    assert _sequences(first) == [1]
    assert first["hasMore"] is True
    assert _sequences(second) == [2, 3]
    assert second["hasMore"] is False


def test_catch_up_across_bounded_pages_is_complete_and_ordered(session):
    _seed(session, [17_000, 90_000, 2_000, 2_000, 130_000, 64_000, 1_000, 33_000])
    unbounded = _page(session, 0)["events"]

    pages = _catch_up(session, max_bytes=65_536)
    events = [event for page in pages for event in page["events"]]

    assert events == unbounded
    assert [int(event["syncSeq"]) for event in events] == list(range(1, 9))
    # Each page is the longest prefix that fits, so its boundaries follow
    # from the encoded sizes alone and agree across authorities.
    boundaries = [_sequences(page)[-1] for page in pages]
    assert boundaries == _expected_boundaries(unbounded, 65_536)
    assert len(set(boundaries)) == len(boundaries) >= 5
    for page in pages:
        assert (len(page["events"]) == 1
                or sum(_encoded(event) for event in page["events"]) <= 65_536)


def _attempt_change(session, size: int):
    from tests.conversation.test_conversation_change_storage import (
        _capture_attempt_change,
        _seed_attempt,
    )

    attempt_event = _seed_attempt(session)
    attempt_event["payload"]["projectionPatch"]["operations"] = [
        {"op": "append", "path": ["toolRounds"], "value": {"text": "x" * size}}]
    session.execute(
        "UPDATE storage_attempt_events SET payload_json=? "
        "WHERE attempt_id='attempt' AND sequence=1", (_dump(attempt_event),))
    _capture_attempt_change(session, attempt_event)


def test_referenced_attempt_bodies_count_toward_the_bound(session):
    _attempt_change(session, 80_000)
    _append(session, 1_000, 1)

    page = _page(session, 0, max_bytes=65_536)

    # The reference row's own document is tiny; its 80 kB body is not.
    assert [event["type"] for event in page["events"]] == ["attempt.event"]
    assert page["hasMore"] is True


def test_mixed_references_and_inline_changes_resume_contiguously(session):
    _append(session, 30_000, 0)
    _attempt_change(session, 30_000)
    _seed(session, [20_000, 40_000, 3_000])
    unbounded = _page(session, 0)["events"]

    pages = _catch_up(session, max_bytes=65_536)

    assert [event for page in pages for event in page["events"]] == unbounded
    assert len(pages) > 1


def test_without_max_bytes_the_page_is_unchanged(session):
    _attempt_change(session, 70_000)
    _seed(session, [40_000] * 5)
    from lib.storage_authority_sql.operations_pkg._turns_read import _turn_sync_changes

    page = _page(session, 0, limit=4)

    assert _sequences(page) == [1, 2, 3, 4]
    assert page["hasMore"] is True
    assert "maxBytesApplied" not in page
    # A bound larger than the whole page selects the same changes.
    bounded = _turn_sync_changes(session, {
        "conversation_id": CONVERSATION, "user_id": 1, "after": 0, "limit": 4,
        "max_bytes": 16_777_216})
    assert bounded["events"] == page["events"]


@pytest.mark.parametrize("value", [0, 65_535, 16_777_217, True, "65536", 1.5])
def test_max_bytes_must_be_within_the_window_contract(session, value):
    _seed(session, [1_000])
    with pytest.raises(StorageError):
        _page(session, 0, max_bytes=value)


class _Client:
    def __init__(self, result):
        self.calls, self.result = [], result

    def query(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        return self.result


def test_the_repository_forwards_the_page_bound(caplog):
    import lib.conversation_sync.repository as repository_module
    from lib.conversation_sync.repository import SidecarConversationSyncRepository

    client = _Client({"head": 0, "events": [], "hasMore": False, "maxBytesApplied": True})
    repository = SidecarConversationSyncRepository(client_factory=lambda write=False: client)
    repository.changes(CONVERSATION, 1, after_sequence=3, limit=500, max_bytes=262_144)
    repository.changes(CONVERSATION, 1, after_sequence=3, limit=500)

    assert client.calls[0] == ("turn.sync.changes", {
        "conversation_id": CONVERSATION, "user_id": 1, "after": 3, "limit": 500,
        "max_bytes": 262_144})
    assert "max_bytes" not in client.calls[1][1]

    old_engine = _Client({"head": 0, "events": [], "hasMore": False})
    repository = SidecarConversationSyncRepository(client_factory=lambda write=False: old_engine)
    repository_module._UNBOUNDED_PAGES_REPORTED = False
    with caplog.at_level(logging.WARNING, logger=repository_module.logger.name):
        repository.changes(CONVERSATION, 1, after_sequence=0, limit=500, max_bytes=262_144)
        repository.changes(CONVERSATION, 1, after_sequence=0, limit=500, max_bytes=262_144)
    # An authority that predates the bound is tolerated and reported once.
    assert [record.message for record in caplog.records].count(
        next(record.message for record in caplog.records)) == 1
    assert "predates byte-bounded change pages" in caplog.records[0].message


@pytest.mark.parametrize(("window_bytes", "expected"), [
    (262_144, 262_144),
    (None, "contract-maximum"),
])
def test_streams_read_pages_bounded_by_their_window_bytes(monkeypatch, window_bytes, expected):
    import routes.conversation_sync_v3 as routes
    from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS

    maximum = (OPENAPI_SCHEMAS["ConversationEventSubscription"]["properties"]
               ["windowBytes"]["maximum"])
    reads = []

    def read(*args, **kwargs):
        reads.append(kwargs)
        return {"head": 0, "events": [], "hasMore": False}

    class Subscription:
        closed = False
        close_reason = ""

        def add_close_callback(self, callback):
            pass

        async def wait(self, timeout):
            self.closed = True
            return False

    monkeypatch.setattr(routes, "_stream_changes_page", read)

    async def drain():
        frames = routes._conversation_change_frames(
            CONVERSATION, 1, Subscription(), after=STREAM_START, delivered_cursor="cursor-0",
            catch_up_budget=None, page_bytes=routes._change_page_bytes(window_bytes),
            diagnostic_options={}, frame=lambda event, cursor: "")
        return [frame async for frame in frames]

    asyncio.run(drain())

    assert reads and reads[0]["max_bytes"] == (
        maximum if expected == "contract-maximum" else expected)
