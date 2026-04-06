"""lib/agent_backends/session_store.py — Backend session persistence.

Maps conversation IDs to backend-native session IDs so multi-turn
works across page reloads.

When Claude Code returns a ``session_id`` in its ``result`` event,
we store it here.  Next time the user sends a message in the same
conversation, we pass ``--resume {session_id}`` for continuity.

Storage uses PostgreSQL via the existing ``lib.database`` module.
Table creation is handled in ``lib/database/_schema.py``.
"""

from __future__ import annotations

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)


def save_session(conv_id: str, backend: str, session_id: str) -> None:
    """Save or update a backend session mapping.

    Args:
        conv_id: Our conversation ID.
        backend: Backend name (e.g. 'claude-code', 'codex').
        session_id: The backend's native session ID.
    """
    if not conv_id or not backend or not session_id:
        return

    try:
        db_execute_with_retry(
            DOMAIN_CHAT,
            """INSERT INTO agent_sessions (conv_id, backend, session_id, last_used_at)
               VALUES (%s, %s, %s, NOW())
               ON CONFLICT (conv_id, backend)
               DO UPDATE SET session_id = EXCLUDED.session_id,
                             last_used_at = NOW()""",
            (conv_id, backend, session_id),
        )
        logger.debug('[SessionStore] Saved session: conv=%s backend=%s session=%s',
                     conv_id[:8], backend, session_id[:16])
    except Exception as e:
        logger.warning('[SessionStore] Failed to save session: conv=%s backend=%s error=%s',
                       conv_id[:8], backend, e)


def get_session(conv_id: str, backend: str) -> str | None:
    """Get the backend's native session ID for a conversation.

    Args:
        conv_id: Our conversation ID.
        backend: Backend name.

    Returns:
        Session ID string, or None if no session exists.
    """
    if not conv_id or not backend:
        return None

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT session_id FROM agent_sessions WHERE conv_id=%s AND backend=%s',
            (conv_id, backend),
        ).fetchone()
        if row:
            return row['session_id']
    except Exception as e:
        logger.debug('[SessionStore] Failed to get session: conv=%s backend=%s error=%s',
                     conv_id[:8], backend, e)

    return None


def list_sessions(backend: str | None = None) -> list[dict]:
    """List all stored sessions, optionally filtered by backend.

    Args:
        backend: Optional backend name filter.

    Returns:
        List of dicts with conv_id, backend, session_id, last_used_at.
    """
    try:
        db = get_thread_db(DOMAIN_CHAT)
        if backend:
            rows = db.execute(
                'SELECT conv_id, backend, session_id, last_used_at '
                'FROM agent_sessions WHERE backend=%s ORDER BY last_used_at DESC',
                (backend,),
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT conv_id, backend, session_id, last_used_at '
                'FROM agent_sessions ORDER BY last_used_at DESC',
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug('[SessionStore] Failed to list sessions: %s', e)
        return []
