"""routes/oauth.py — OAuth authentication API endpoints.

Browser-centric flow:
  1. POST /api/oauth/login   → returns auth_url, starts relay server
  2. Browser opens auth_url in popup → user authenticates
  3. OAuth redirects to localhost:PORT → relay server serves HTML page
  4. Relay page uses postMessage() to send code back to opener window
  5. POST /api/oauth/callback → frontend sends code, server exchanges for tokens
  6. GET  /api/oauth/status   → poll auth state
  7. POST /api/oauth/logout   → delete tokens
"""

from flask import Blueprint, jsonify, request

from lib.log import get_logger

logger = get_logger(__name__)

oauth_bp = Blueprint('oauth', __name__)


@oauth_bp.route('/api/oauth/login', methods=['POST'])
def oauth_login():
    """Start an OAuth login flow.

    Generates PKCE codes, auth URL, and starts a relay server on the
    registered callback port. The frontend should open auth_url in a
    popup and listen for postMessage('oauth_callback', ...) to receive
    the authorization code.

    Body: { "provider": "claude" | "codex" }
    Returns: { "auth_url": "...", "status": "started", "provider": "...", "callback_port": N }
    """
    try:
        from lib.oauth.manager import start_oauth_flow

        data = request.get_json(force=True, silent=True) or {}
        provider = data.get('provider', '')

        if provider not in ('claude', 'codex'):
            return jsonify({'error': 'Invalid provider. Use "claude" or "codex".'}), 400

        result = start_oauth_flow(provider)

        if 'error' in result:
            return jsonify(result), 400

        return jsonify(result)

    except Exception as e:
        logger.error('[OAuth API] Login failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@oauth_bp.route('/api/oauth/callback', methods=['POST'])
def oauth_callback():
    """Exchange an authorization code for tokens.

    Called by the frontend after receiving the code via postMessage
    from the relay page, or via manual URL paste.

    Body: { "provider": "claude" | "codex", "code": "XXX" }
      or: { "provider": "claude" | "codex", "callback_url": "http://localhost:.../callback?code=XXX" }
    """
    try:
        from lib.oauth.manager import exchange_code
        from urllib.parse import urlparse, parse_qs

        data = request.get_json(force=True, silent=True) or {}
        provider = data.get('provider', '')
        code = data.get('code', '')
        callback_url = data.get('callback_url', '')

        if provider not in ('claude', 'codex'):
            return jsonify({'error': 'Invalid provider'}), 400

        # Extract code from callback URL if provided
        if callback_url and not code:
            parsed = urlparse(callback_url)
            params = parse_qs(parsed.query)
            code = params.get('code', [None])[0]
            if not code:
                return jsonify({'error': 'No authorization code found in the URL'}), 400

        if not code:
            return jsonify({'error': 'No authorization code provided'}), 400

        result = exchange_code(provider, code)

        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)

    except Exception as e:
        logger.error('[OAuth API] Callback failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@oauth_bp.route('/api/oauth/status')
def oauth_status():
    """Get OAuth status for all providers or a specific one.

    Query: ?provider=claude (optional)
    Returns: { "claude": {...}, "codex": {...} } or single provider dict.
    """
    try:
        from lib.oauth.manager import get_oauth_status, get_all_oauth_status

        provider = request.args.get('provider', '')

        if provider:
            if provider not in ('claude', 'codex'):
                return jsonify({'error': 'Invalid provider'}), 400
            return jsonify(get_oauth_status(provider))

        return jsonify(get_all_oauth_status())

    except Exception as e:
        logger.error('[OAuth API] Status check failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@oauth_bp.route('/api/oauth/logout', methods=['POST'])
def oauth_logout():
    """Logout from an OAuth provider.

    Body: { "provider": "claude" | "codex" }
    """
    try:
        from lib.oauth.manager import logout_oauth

        data = request.get_json(force=True, silent=True) or {}
        provider = data.get('provider', '')

        if provider not in ('claude', 'codex'):
            return jsonify({'error': 'Invalid provider'}), 400

        result = logout_oauth(provider)
        return jsonify(result)

    except Exception as e:
        logger.error('[OAuth API] Logout failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500
