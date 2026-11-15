"""Owner-scoped cancellation fence for a command still entering the executor.

The fence is a durable record because the request process that accepts a Stop
and the execution host that later starts the accepted attempt are different
processes: a process-local store let a stopped send start anyway. It never owns
transcript state; accepted attempts are cancelled through the durable attempt
API instead.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Mapping
from typing import Any

from lib.agent_core.storage_errors import StorageError
from lib.identity import require_user_id


_NAMESPACE = "conversation.pending_abort.v1"
_FENCE_TTL_SECONDS = 300.0


def _key(conversation_id: str, owner: int) -> str:
    return f"{owner}:{hashlib.sha256(conversation_id.encode()).hexdigest()}"


def _occurred_at(row: Mapping[str, Any], owner: int, conversation_id: str) -> float:
    value = row.get("value")
    if (not isinstance(value, Mapping) or value.get("owner") != owner
            or value.get("conversationId") != conversation_id
            or type(value.get("occurredAt")) not in (int, float)):
        raise StorageError("database_integrity", "Conversation Stop fence is malformed")
    return float(value["occurredAt"])


def mark_pending_abort(
    conversation_id: str,
    user_id: Any,
    *,
    occurred_at: float | None = None,
) -> float | None:
    """Publish a bounded cancellation fence and return its server timestamp.

    The stored time only moves forward, so a delayed earlier Stop cannot
    reopen the window a later Stop closed.
    """
    if not conversation_id:
        return None
    from lib.storage import get_storage_client

    owner = require_user_id(user_id, context="conversation Stop fence")
    timestamp = time.time() if occurred_at is None else float(occurred_at)
    key = _key(conversation_id, owner)
    value = {"owner": owner, "conversationId": conversation_id, "occurredAt": timestamp}
    while True:
        row = get_storage_client(write=False).query(
            "record.get", {"namespace": _NAMESPACE, "key": key})
        if row is not None and _occurred_at(row, owner, conversation_id) >= timestamp:
            return timestamp
        try:
            get_storage_client(write=True).command("record.put", {
                "namespace": _NAMESPACE, "key": key, "value": value,
                "expected_version": row["version"] if row else 0,
            }, "conversation-stop-fence:" + uuid.uuid4().hex)
            return timestamp
        except StorageError as exc:
            # A conflict means another Stop committed first; re-read and keep the later time.
            if exc.code != "database_conflict":
                raise


def was_pending_abort_after(
    conversation_id: str,
    user_id: Any,
    request_started_at: float | None,
) -> bool:
    """Return whether this owner stopped the conversation after request start."""
    if not conversation_id or request_started_at is None:
        return False
    from lib.storage import get_storage_client

    owner = require_user_id(user_id, context="conversation Stop fence")
    row = get_storage_client(write=False).query(
        "record.get", {"namespace": _NAMESPACE, "key": _key(conversation_id, owner)})
    if row is None:
        return False
    occurred_at = _occurred_at(row, owner, conversation_id)
    return (occurred_at >= float(request_started_at)
            and time.time() - occurred_at <= _FENCE_TTL_SECONDS)


__all__ = ["mark_pending_abort", "was_pending_abort_after"]
