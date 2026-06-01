"""routes/chat_queue.py — Server-side message queue endpoints.

Extracted from ``routes/chat.py`` so the queue API has its own module.
The handlers register on the same ``chat_bp`` Blueprint (imported here)
to keep the public URLs unchanged.
"""

from flask import jsonify, request

from lib.log import get_logger
from routes.chat import chat_bp

logger = get_logger(__name__)


@chat_bp.route('/api/chat/queue', methods=['POST'])
def chat_queue_enqueue():
    """Legacy enqueue endpoint — kept for programmatic/API use.

    The primary send path is now ``/api/chat/send`` which auto-detects
    whether to start immediately or enqueue.  This endpoint is a thin
    wrapper around ``enqueue_message`` for backward compat.
    """
    data = request.get_json(silent=True) or {}
    conv_id = data.get('convId', '')
    if not conv_id:
        return jsonify({'error': 'convId required'}), 400

    message_data = data.get('message', {})
    config = data.get('config', {})

    if not message_data.get('text') and not message_data.get('images') and not message_data.get('pdfTexts'):
        return jsonify({'error': 'Empty message'}), 400

    from lib.message_queue import enqueue_message
    result = enqueue_message(conv_id, message_data, config)
    return jsonify(result)


@chat_bp.route('/api/chat/queue/<conv_id>', methods=['GET'])
def chat_queue_get(conv_id):
    """Get all queued messages for a conversation.

    This endpoint is polled frequently by the frontend.  When the DB
    connection pool is saturated (e.g. during startup / burst traffic)
    ``get_queue`` can raise ``psycopg.OperationalError: timeout expired``.
    Bubbling that to a 500 produces scary stack traces in ``error.log``
    and breaks the frontend poll loop.  Since "empty queue" is a safe
    degraded response for a polling endpoint, we catch DB-side failures
    here and return ``[]`` with a warning log; the next poll will retry
    cleanly once the pool frees up.
    """
    from lib.message_queue import get_queue
    try:
        queue = get_queue(conv_id)
    except Exception as e:
        logger.warning('[chat_queue_get] get_queue failed for conv=%s: %s — returning empty list',
                       conv_id, e)
        return jsonify([])
    return jsonify(queue)


@chat_bp.route('/api/chat/queue/<conv_id>/<queue_id>', methods=['DELETE'])
def chat_queue_remove(conv_id, queue_id):
    """Remove a specific message from the queue."""
    from lib.message_queue import remove_from_queue
    removed = remove_from_queue(conv_id, queue_id)
    if not removed:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'ok': True})


@chat_bp.route('/api/chat/queue/<conv_id>', methods=['DELETE'])
def chat_queue_clear(conv_id):
    """Clear all queued messages for a conversation."""
    from lib.message_queue import clear_queue
    count = clear_queue(conv_id)
    return jsonify({'cleared': count})
