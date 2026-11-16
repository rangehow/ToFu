"""Finite replay storage double shared by single and multiplexed stream tests.

ScriptedConversationRepository advances pages and notifies the supplied real
broker. Routes and ConversationSyncService retain their production behavior.
"""
import hashlib

#: The authority epoch every scripted page reports (one authority open).
SCRIPTED_EPOCH = "0000000000005eed"


def scripted_digest(sequence: int) -> str:
    """The digest the double reports for the change at ``sequence``."""
    return hashlib.sha256(f"scripted-change:{sequence}".encode()).hexdigest()[:16]


def with_lineage(page: dict, after_sequence: int) -> dict:
    """Complete a scripted page with the lineage facts an authority reports.

    A page may set any of them itself, e.g. another epoch for a restarted
    authority or another anchor digest for a reused sequence.
    """
    page = dict(page)
    head = int(page.get("head") or 0)
    page.setdefault("authorityEpoch", SCRIPTED_EPOCH)
    page.setdefault("headDigest", scripted_digest(head) if head else None)
    if not page.get("resetRequired"):
        page.setdefault("changeDigests", [
            scripted_digest(int(event["syncSeq"])) for event in page.get("events") or []])
        if after_sequence:
            page.setdefault("anchorDigest", scripted_digest(after_sequence))
    return page


def stream_page(events=(), *, after, head=None, reset=None, has_more=False,
                cursor=lambda sequence: f"cursor-{sequence}"):
    """One ``ConversationSyncService.changes`` result for route doubles."""
    from lib.conversation_sync.cursor import ReplayPosition

    events = list(events)
    sequence = int(events[-1]["syncSeq"]) if events else after.sequence
    head = sequence if head is None else head
    return {
        "head": head, "events": events,
        "cursors": [cursor(int(event["syncSeq"])) for event in events],
        "position": ReplayPosition(sequence, after.epoch, after.digest),
        "cursor": cursor(sequence), "headCursor": cursor(head),
        "reset": reset, "hasMore": has_more,
    }


class ScriptedConversationRepository:
    """Per-conversation pages; an exhausted script expires the cursor.

    Advancing a page invalidates its scope like the authority watch. Heartbeats
    carry liveness only and cannot reveal the script's next state.
    ``None`` is an absent conversation. An exception fails the first page read
    after the ownership probe, as a storage failure in a live stream does.
    """

    def __init__(self, scripts: dict, broker):
        self.broker = broker
        self.scripts = {
            key: list(value) if isinstance(value, list) else value
            for key, value in scripts.items()
        }

    def changes(self, conversation_id, user_id, *, after_sequence, limit,
                max_bytes=None, cancellation=None):
        # Scripted pages are small and return whole, like an authority that
        # predates the byte bound; stream reads pass both options.
        script = self.scripts.get(conversation_id)
        if script is None:
            return None
        if limit == 1:
            return with_lineage({"head": after_sequence, "events": [],
                                 "resetRequired": False, "hasMore": False}, after_sequence)
        if isinstance(script, Exception):
            raise script
        if script:
            page = script.pop(0)
            self.broker.invalidate([{"userId": user_id, "conversationId": conversation_id}])
            return with_lineage(page, after_sequence)
        return with_lineage({"head": 0, "events": [], "resetRequired": True,
                             "resetReason": "cursor_expired"}, after_sequence)
