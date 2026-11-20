"""Owner-scoped change signals for model-routing authority commits.

``notify_model_routing_changed`` pushes a browser wake hint. The frame carries
owner identity and the committed aggregate revision only. The revisioned HTTP
aggregate (GET /api/v4/model-routing) remains the single authority: clients
treat the hint as a trigger to re-fetch, so a lost or duplicated notification
never loses data.

``mark_model_routing_secrets_changed`` is the cross-process signal for state
derived from an owner's secrets (known-credential redaction) and for the
dispatcher's configured slot pool. The push hint reaches only this process's
sockets, so after every commit or secret write the durable repository
atomically replaces one per-owner stamp file in the config directory; both
server processes compare its stat identity (``model_routing_secrets_stamp_identity``)
and re-read through the repository when it changes. The stamp holds the owner
and the storage revision, never a secret.
"""

from __future__ import annotations

import json
import os
import tempfile

from lib.log import get_logger


logger = get_logger(__name__)

MODEL_ROUTING_PUSH_TASK_ID = "__model_routing__"


def notify_model_routing_changed(*, user_id: int, rev=None) -> None:
    """Publish one best-effort, owner-scoped model-routing wake hint."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    try:
        from lib.agent_core.events import build_push_frame
        from lib.agent_core.push import push_event

        frame_fields = {"userId": user_id}
        if rev is not None:
            try:
                revision = int(rev)
            except (TypeError, ValueError):
                logger.debug(
                    "[ModelRoutingNotify] non-int rev=%r dropped", rev)
            else:
                if revision > 0:
                    frame_fields["rev"] = revision
        payload = build_push_frame("model_routing.changed", **frame_fields)
        push_event(
            "notify", MODEL_ROUTING_PUSH_TASK_ID, payload, user_id=user_id)
    except Exception as exc:
        logger.debug("[ModelRoutingNotify] push skipped: %s", exc)


def model_routing_secrets_stamp_path(*, user_id: int) -> str:
    """Per-owner stamp file replaced whenever that owner's secrets may change."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    from lib.config_dir import config_path
    return config_path(f"model_routing_secrets.{user_id}.stamp")


def model_routing_secrets_stamp_identity(*, user_id: int):
    """Stat identity of an owner's change stamp, or None when never written.

    The cheap cross-process read side of ``mark_model_routing_secrets_changed``:
    one ``stat`` (no storage round trip) tells a process holding derived state
    whether the owner's routing changed since it built that state. Identity is
    ``(inode, size, mtime_ns)`` because the writer replaces the file atomically.
    """
    try:
        stat = os.stat(model_routing_secrets_stamp_path(user_id=user_id))
    except OSError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def mark_model_routing_secrets_changed(*, user_id: int, rev=None) -> None:
    """Atomically replace the owner's stamp, so every process sees a new inode.

    Best-effort like the push hint: the storage write already succeeded. A
    failed stamp leaves other processes on their previous derived state until
    the next change, so it is logged as an error (exception type only).
    """
    path = model_routing_secrets_stamp_path(user_id=user_id)
    tmp = None
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".model_routing_secrets.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"owner_user_id": user_id, "revision": rev}, handle)
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        logger.error("[ModelRoutingNotify] secrets stamp write failed: %s",
                     type(exc).__name__)
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


__all__ = [
    "MODEL_ROUTING_PUSH_TASK_ID",
    "mark_model_routing_secrets_changed",
    "model_routing_secrets_stamp_identity",
    "model_routing_secrets_stamp_path",
    "notify_model_routing_changed",
]
