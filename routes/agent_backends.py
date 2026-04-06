"""routes/agent_backends.py — Agent backend status and selection API.

Endpoints:
  GET  /api/agent-backends/status — List all backends with availability/auth/capabilities
  POST /api/agent-backends/set    — Set active backend (stored in conversation settings)
"""

from flask import Blueprint, jsonify, request

from lib.log import get_logger

logger = get_logger(__name__)

agent_backends_bp = Blueprint('agent_backends', __name__)


@agent_backends_bp.route('/api/agent-backends/status', methods=['GET'])
def backends_status():
    """List all registered backends with their status.

    Returns::

        {
          "backends": [
            {
              "name": "builtin",
              "displayName": "Tofu (Built-in)",
              "available": true,
              "authenticated": true,
              "version": "2.1.0",
              "capabilities": { "modelSelector": true, ... }
            },
            {
              "name": "claude-code",
              "displayName": "Claude Code",
              "available": true,
              "authenticated": true,
              "version": "1.0.33",
              "capabilities": { "modelSelector": false, ... }
            },
            ...
          ]
        }
    """
    from lib.agent_backends import list_backends

    try:
        backends = list_backends()
    except Exception as e:
        logger.error('[AgentBackends] Failed to list backends: %s', e, exc_info=True)
        return jsonify({'error': 'Failed to query backends'}), 500

    return jsonify({'backends': backends})


@agent_backends_bp.route('/api/agent-backends/set', methods=['POST'])
def set_backend():
    """Set the active backend for a conversation.

    Body::

        {
          "convId": "conv-abc123",  // optional — global if omitted
          "backend": "claude-code"
        }

    Returns::

        {"ok": true, "backend": "claude-code"}
    """
    from lib.agent_backends import get_backend

    data = request.get_json(silent=True) or {}
    backend_name = data.get('backend', '')

    if not backend_name:
        return jsonify({'error': 'No backend specified'}), 400

    backend = get_backend(backend_name)
    if backend is None:
        return jsonify({'error': f'Unknown backend: {backend_name}'}), 400

    if not backend.is_available():
        return jsonify({
            'error': f'{backend.display_name} CLI is not installed. '
                     f'Install it first, then try again.',
        }), 400

    if not backend.is_authenticated():
        return jsonify({
            'error': f'{backend.display_name} is not authenticated. '
                     f'Run the CLI and log in first.',
        }), 401

    logger.info('[AgentBackends] Backend set to: %s (conv=%s)',
                backend_name, data.get('convId', 'global'))

    return jsonify({
        'ok': True,
        'backend': backend_name,
        'displayName': backend.display_name,
        'capabilities': backend.get_capabilities().to_dict(),
    })
