"""routes/chat_human_io.py — Stdin and human-guidance response endpoints.

Extracted from ``routes/chat.py``. Both endpoints resolve a pending
request that a running task is blocked on (subprocess stdin / human
guidance prompt). They share no state with the rest of ``chat.py``
beyond the public ``chat_bp`` Blueprint.
"""

from flask import jsonify, request

from lib.log import get_logger
from routes.chat import chat_bp

logger = get_logger(__name__)


@chat_bp.route('/api/chat/stdin_response', methods=['POST'])
def chat_stdin_response():
    """Provide stdin input to a subprocess waiting for user input.

    Body: { "stdinId": "stdin_...", "input": "user's text", "eof": false }
    If ``eof`` is true, stdin is closed (no input is sent).
    """
    data = request.get_json(silent=True) or {}
    stdin_id = data.get('stdinId', '')
    is_eof = data.get('eof', False)
    input_text = data.get('input', '')
    logger.info('[Stdin] /api/chat/stdin_response received: '
                'stdinId=%s, eof=%s, input_len=%d',
                stdin_id, is_eof, len(input_text))
    if not stdin_id:
        logger.warning('[Stdin] Rejected — missing stdinId')
        return jsonify({'error': 'No stdinId'}), 400

    from lib.tasks_pkg import resolve_stdin
    # EOF → resolve with None to signal stdin close
    resolved_text = None if is_eof else input_text
    try:
        ok = resolve_stdin(stdin_id, resolved_text)
    except Exception as e:
        logger.error('[Stdin] Exception resolving %s: %s',
                     stdin_id, e, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    if not ok:
        logger.warning('[Stdin] Request not found or expired: stdinId=%s',
                       stdin_id)
        return jsonify({'error': 'Stdin request not found or expired'}), 404
    logger.info('[Stdin] Successfully resolved %s', stdin_id)
    return jsonify({'ok': True, 'stdinId': stdin_id})


@chat_bp.route('/api/chat/human_response', methods=['POST'])
def chat_human_response():
    """Resolve a human guidance request — the user has answered a question.

    Body: { "guidanceId": "hg_...", "response": "user's answer text" }
    """
    data = request.get_json(silent=True) or {}
    guidance_id = data.get('guidanceId', '')
    response_text = data.get('response', '')
    logger.info('[HumanGuidance] /api/chat/human_response received: '
                'guidanceId=%s, response_len=%d',
                guidance_id, len(response_text))
    if not guidance_id:
        logger.warning('[HumanGuidance] Rejected — missing guidanceId')
        return jsonify({'error': 'No guidanceId'}), 400
    if not response_text:
        logger.warning('[HumanGuidance] Rejected — empty response for '
                       'guidanceId=%s', guidance_id)
        return jsonify({'error': 'No response text'}), 400

    from lib.tasks_pkg import resolve_human_guidance
    try:
        ok = resolve_human_guidance(guidance_id, response_text)
    except Exception as e:
        logger.error('[HumanGuidance] Exception resolving %s: %s',
                     guidance_id, e, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
    if not ok:
        logger.warning('[HumanGuidance] Guidance request not found or '
                       'expired: guidanceId=%s', guidance_id)
        return jsonify({'error': 'Guidance request not found or expired'}), 404
    logger.info('[HumanGuidance] Successfully resolved %s (response_len=%d)',
                guidance_id, len(response_text))
    return jsonify({'ok': True, 'guidanceId': guidance_id})
