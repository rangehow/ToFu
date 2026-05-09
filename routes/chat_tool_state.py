"""routes/chat_tool_state.py — Lightweight tool-state PATCH endpoint.

Extracted from ``routes/chat.py``. Patches the ``settings`` column of a
conversation without touching messages or search-text indexes; safe to
call frequently (every tool toggle).
"""

import json

from flask import jsonify, request

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import get_logger
from routes.chat import chat_bp
from routes.common import DEFAULT_USER_ID

logger = get_logger(__name__)


@chat_bp.route('/api/chat/tool-state/<conv_id>', methods=['PATCH'])
def chat_tool_state(conv_id):
    """Lightweight tool-state sync: merge tool settings into conversation settings.

    Unlike the full PUT /api/conversations/<id>, this only touches the settings
    column — no messages, no msg_count, no search_text update.
    Safe to call frequently (e.g. on every tool toggle).

    Body: { model?, searchMode?, fetchEnabled?, browserEnabled?, projectPath?, ... }
    """
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({'error': 'No settings provided'}), 400

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()

        if not row:
            # Conv not in DB yet (no messages sent) — that's OK, skip
            return jsonify({'ok': True, 'skipped': True})

        try:
            settings = json.loads(row['settings'] or '{}')
        except (json.JSONDecodeError, TypeError):
            settings = {}

        settings.update(data)
        settings_json = json.dumps(settings, ensure_ascii=False)

        db_execute_with_retry(db, '''
            UPDATE conversations SET settings=? WHERE id=? AND user_id=?
        ''', (settings_json, conv_id, DEFAULT_USER_ID))

        logger.debug('[ToolState] conv=%s patched %d keys: %s',
                     conv_id[:8], len(data), list(data.keys())[:10])
        return jsonify({'ok': True})

    except Exception as e:
        logger.error('[ToolState] Failed for conv=%s: %s', conv_id[:8], e, exc_info=True)
        return jsonify({'error': 'internal_error'}), 500
