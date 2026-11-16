"""A stream's change-page read never outlives its client.

On 2026-11-13 a lane census found 59 extra threads and 149 response-frame
reservations still alive after every client had closed: sync carriers were
finishing, and retrying, change-page reads for streams nobody would receive.
Closing a stream's subscription now cancels its page read. An in-flight
exchange is interrupted, a cancelled read is never replayed, and no further
page is read.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from lib.conversation_sync.cursor import STREAM_START
from lib.storage.errors import StorageError
from tofu_db._request_cancellation import RequestCancellation


pytestmark = pytest.mark.unit


class _Subscription:
    def __init__(self):
        self.closed = False
        self.close_reason = ""
        self._callbacks = []

    def add_close_callback(self, callback):
        self._callbacks.append(callback)

    def close(self, reason="closed"):
        self.closed, self.close_reason = True, reason
        for callback in self._callbacks:
            callback()

    async def wait(self, timeout):
        await asyncio.sleep(0)
        return False


def test_closing_a_stream_interrupts_its_page_read_and_reads_no_more(monkeypatch):
    import routes.conversation_sync_v3 as routes

    entered = threading.Event()
    tokens = []

    def read(*args, cancellation=None, **kwargs):
        tokens.append(cancellation)
        entered.set()
        # A storage exchange that ends only when its owner interrupts it.
        give_up = time.monotonic() + 10
        while not (cancellation is not None and cancellation.cancelled):
            if time.monotonic() > give_up:
                raise AssertionError("the page read outlived its client")
            time.sleep(0.01)
        raise StorageError("database_unavailable", "interrupted", True, 50)

    monkeypatch.setattr(routes, "_stream_changes_page", read)
    subscription = _Subscription()

    async def run():
        frames = routes._conversation_change_frames(
            "conversation", 1, subscription, after=STREAM_START, delivered_cursor="cursor-0",
            catch_up_budget=None, diagnostic_options={}, frame=lambda event, cursor: "")
        consumer = asyncio.ensure_future(frames.__anext__())
        assert await asyncio.to_thread(entered.wait, 10)
        subscription.close("client_gone")
        with pytest.raises(StopAsyncIteration):
            await consumer

    asyncio.run(run())

    assert len(tokens) == 1
    assert isinstance(tokens[0], RequestCancellation) and tokens[0].cancelled
    with pytest.raises(OSError, match="before dispatch"):
        with tokens[0].bind(lambda: None):
            pass


@pytest.mark.parametrize("client_path", [
    "lib.storage.v2_client:StorageV2Client",
    "lib.storage.client:StorageClient",
])
def test_a_cancelled_read_is_never_replayed(monkeypatch, client_path):
    import importlib

    module_name, class_name = client_path.split(":")
    client_class = getattr(importlib.import_module(module_name), class_name)
    cancellation = RequestCancellation()
    attempts = []

    def attempt(self, *args, **kwargs):
        attempts.append(kwargs.get("cancellation"))
        cancellation.cancel()
        raise StorageError("database_unavailable", "interrupted", True, 50)

    monkeypatch.setattr(client_class, "_attempt", attempt)
    client = object.__new__(client_class)
    client._timeout, client._read_attempts = 5.0, 3

    with pytest.raises(StorageError):
        client.query("turn.sync.changes", {"conversation_id": "c", "user_id": 1},
                     cancellation=cancellation)

    assert attempts == [cancellation]


def test_the_embedded_session_accepts_an_owner_interrupt():
    from lib.storage.v2_embedded_client import _EmbeddedSessionAdapter

    cancellation = RequestCancellation()
    session = object.__new__(_EmbeddedSessionAdapter)
    with cancellation.bind(session.interrupt):
        cancellation.cancel()
    assert cancellation.cancelled
