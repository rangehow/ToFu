"""lib/feishu/startup.py — Bot startup, WebSocket connection, and reconnection.

Manages the Lark SDK WebSocket long-connection with patched ping settings.
Reconnection has no schedule of its own: each session is admitted, and its
end recorded, by the shared network authority (``lib.network_routes``,
docs/NETWORK.md) for the route the SDK actually takes to the Feishu open
platform, so the wait between sessions is that authority's route circuit
(docs/modules/integrations_api.md, "Feishu reconnection").
"""

import threading
import time

import lib.feishu._state as _st

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['connection_status', 'is_bot_running', 'start_bot', 'start_bot_from_saved_config']

# The daemon thread running the WebSocket loop (None until first start).
_bot_thread = None
# The long connection's last reported outcome, in the channel account-state
# vocabulary (docs/CHANNELS.md, "Messaging platform lifecycle"): ``starting``
# while an attempt runs, ``disconnected`` with the SDK's refusal while the loop
# retries, ``runtime_unavailable`` when the SDK is missing.
_connection = {'state': 'disabled', 'detail': ''}

# lark_oapi's FEISHU_DOMAIN: the SDK fetches its WebSocket endpoint here, so
# this origin's route health stands for the long connection.
FEISHU_OPEN_PLATFORM_URL = 'https://open.feishu.cn/'


class _SessionEvidence:
    """Reports one session's first delivered event as a successful request.

    Only a real request outcome forgives the shared circuit's backoff debt;
    an event arriving over the connection is that proof for a long-lived
    session.
    """

    def __init__(self, routes, route):
        self._routes = routes
        self._route = route
        self._lock = threading.Lock()
        self._reported = False

    def event_delivered(self) -> None:
        with self._lock:
            if self._reported:
                return
            self._reported = True
        self._routes.report(FEISHU_OPEN_PLATFORM_URL, self._route, True)


_session_evidence: '_SessionEvidence | None' = None


def _event_delivered() -> None:
    evidence = _session_evidence
    if evidence is not None:
        evidence.event_delivered()


def _session_route():
    """The route the SDK's own connection takes.

    lark_oapi (requests) and websockets both follow the process proxy
    environment, never Tofu's proxy pool, so the session is attributed to the
    environment route when one applies and to ``direct`` otherwise.
    """
    from requests.utils import get_environ_proxies

    from lib.proxy import network_route_specs
    specs = network_route_specs(FEISHU_OPEN_PLATFORM_URL)
    environ = get_environ_proxies(FEISHU_OPEN_PLATFORM_URL)
    if environ.get('https') or environ.get('all'):
        for route in specs:
            if route.route_id.startswith('env:'):
                return route
    return specs[0]


def _connection_loop(ws_client, *, routes=None, sleep=None) -> None:
    """Run SDK sessions back to back under the shared route policy.

    Before each session the route must be admitted by the shared manager
    (cold probe race, open circuit); otherwise the loop waits out that
    route's circuit. Every session end is recorded as a failed request on
    the route: a connection-setup failure as ``network_fail``, any other end
    as ``session_failed``, so repeated ends escalate the shared backoff and
    a delivered event (``_SessionEvidence``) forgives it.
    """
    global _session_evidence
    from lib.network_routes import is_safe_connect_failure
    if routes is None:
        from lib.network_routes import manager as routes
    sleep = sleep or time.sleep
    url = FEISHU_OPEN_PLATFORM_URL
    while True:
        route = _session_route()
        if not routes.candidates(url, [route]):
            delay = routes.circuit_delay(url, route)
            logger.info('[FeishuBot] Waiting %.1fs for the shared route circuit via %s',
                        delay, route.label)
            if _connection['state'] != 'disconnected':
                _report('disconnected', 'This server cannot reach the Feishu open platform; '
                        'Tofu reconnects once its network route recovers')
            sleep(delay)
            continue
        _session_evidence = _SessionEvidence(routes, route)
        failure_kind = 'session_failed'
        try:
            logger.info('[FeishuBot] Connecting via WebSocket (%s)...', route.label)
            _report('starting')
            ws_client.start()
            logger.info('[FeishuBot] WebSocket session ended')
            _report('disconnected', 'The Feishu long connection closed; reconnecting')
        except KeyboardInterrupt:
            logger.info('[FeishuBot] Interrupted — shutting down')
            break
        except Exception as e:
            _report('disconnected', f'{type(e).__name__}: {e}')
            if is_safe_connect_failure(e):
                failure_kind = 'network_fail'
            logger.error('[FeishuBot] WebSocket error (%s): %s', failure_kind, e, exc_info=True)
        finally:
            _session_evidence = None
        routes.report(url, route, False, failure_kind=failure_kind)


def is_bot_running() -> bool:
    """True when the Feishu bot daemon thread is alive."""
    t = _bot_thread
    return t is not None and t.is_alive()


def _report(state: str, detail: str = '') -> None:
    global _connection
    _connection = {'state': state, 'detail': detail}


def connection_status() -> dict:
    """``{state, detail}`` of the bot's long connection, with the real reason when it is down.

    ``connected`` means the connection loop runs and its current attempt has
    reported no failure: the SDK's ``start()`` blocks while connected and
    raises when the platform refuses (a stale App ID/Secret) or the network
    fails. Feishu delivers each event to one of the app's long connections, so
    a second server running the same app silently receives a share of the
    messages; nothing here can see that, so one app is served by one server.
    """
    if not _st.ENABLED:
        return {'state': 'disabled', 'detail': ''}
    state = dict(_connection)
    if state['state'] == 'starting' and is_bot_running():
        return {'state': 'connected', 'detail': ''}
    if not is_bot_running() and state['state'] in ('starting', 'disabled'):
        return {'state': 'disconnected', 'detail': 'The Feishu bot is not running; save the settings again'}
    return state


def _patch_websockets_ping_settings():
    """Patch websockets.connect defaults to increase ping tolerance.

    The lark_oapi SDK calls websockets.connect() with no ping arguments,
    so websockets uses its defaults: ping_interval=20s, ping_timeout=20s.
    This is too aggressive for long-lived Feishu connections — network hiccups
    or CPU-heavy tool execution cause ping timeouts that kill the connection
    with error 1011 ("keepalive ping timeout; no close frame received").

    The SDK has its OWN application-level ping at 120s intervals, so the
    websockets-level ping is redundant. We disable the websockets ping timeout
    (set ping_timeout=None) but keep ping_interval to maintain keepalive
    traffic through proxies. This prevents spurious disconnects while still
    allowing the SDK's built-in reconnection to handle real outages.
    """
    import websockets
    _original_connect = websockets.connect

    class _PatchedConnect(_original_connect.__class__
                          if isinstance(_original_connect, type)
                          else type(_original_connect)):
        pass

    def _patched_connect(*args, **kwargs):
        kwargs.setdefault('ping_interval', 30)
        kwargs.setdefault('ping_timeout', None)
        return _original_connect(*args, **kwargs)

    try:
        websockets.connect = _patched_connect
        logger.debug('[FeishuBot] Patched websockets.connect: ping_timeout=None')
    except Exception as e:
        logger.warning('[FeishuBot] Failed to patch websockets ping: %s', e, exc_info=True)


def start_bot() -> bool:
    """Start the Feishu bot on a background daemon thread.

    Returns True if the bot was started, False if disabled/missing credentials.
    """
    global _bot_thread
    if is_bot_running():
        logger.info('[FeishuBot] Already running — not starting a second connection')
        return True
    # Live read: GUI-saved credentials applied via _state.apply_config must
    # be honoured when the bot first starts after boot.
    if not _st.ENABLED:
        logger.info('[FeishuBot] Disabled — app_id / app_secret not configured')
        _report('disabled')
        return False
    _report('starting')

    def _run():
        try:
            import lark_oapi as lark
            from lark_oapi.adapter.websocket import WebSocket
        except ImportError as error:
            logger.error('[FeishuBot] The Lark SDK is unavailable: %s', error)
            _report('runtime_unavailable', f'The Lark SDK (lark_oapi) is not installed on this server: {error}')
            return

        from lib.feishu.events import handle_menu_event, handle_message_event

        _patch_websockets_ping_settings()

        def on_message(data):
            _event_delivered()
            return handle_message_event(data)

        def on_menu(data):
            _event_delivered()
            return handle_menu_event(data)

        event_handler = lark.EventDispatcherHandler.builder(
            '', ''  # verification token, encrypt key — unused with WS
        ).register_p2_im_message_receive_v1(
            on_message
        ).register_p1_application_bot_menu_v6(
            on_menu
        ).build()

        ws_client = WebSocket.builder(_st.APP_ID, _st.APP_SECRET) \
            .event_handler(event_handler) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        _connection_loop(ws_client)

    t = threading.Thread(target=_run, daemon=True, name='feishu-bot')
    t.start()
    _bot_thread = t
    logger.info('[FeishuBot] Bot thread started (lark_oapi loading in background...)')
    return True


def start_bot_from_saved_config() -> bool:
    """Apply ``server_config.json``'s ``feishu`` block, then start when enabled.

    Boot counterpart of the settings-UI save path: credentials entered
    graphically (no environment variables) must start the bot on the next
    server launch exactly like env-configured ones.
    """
    from lib import _SERVER_CONFIG_PATH
    from lib.json_store import read_json
    saved = read_json(_SERVER_CONFIG_PATH, default={})
    block = saved.get('feishu') if isinstance(saved, dict) else None
    if isinstance(block, dict) and block:
        changed = _st.apply_config(block)
        logger.info('[FeishuBot] Applied saved server_config feishu block '
                    '(creds_changed=%s, enabled=%s)', changed, _st.ENABLED)
    return start_bot()
