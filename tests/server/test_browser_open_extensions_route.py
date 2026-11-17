"""tests/server/test_browser_open_extensions_route.py — POST
/api/v4/browser/open-extensions, the browser extension dialog's one-click
"open the extensions page" action: loopback-gated and shell-free.

A REMOTE peer must never make the server spawn a browser on its own
(headless) machine, and the spawn must be a list-form argv — no shell for a
future argument to travel through.
"""
import pytest

pytestmark = pytest.mark.unit


def _post_open_extensions(flask_client, monkeypatch, client_addr, *,
                          chrome='/fake/chrome'):
    """Drive ONE real POST through the full stack with a chosen socket peer.

    ``scope_base={'client': ...}`` sets the ASGI peer the way Hypercorn
    would (the mechanism tests/server/test_proxy_trust.py established), so the REAL
    ``_remote_is_loopback`` runs against it. A disposable API key gives a
    remote peer a valid credential: open mode refuses its synthetic admin
    grant to non-loopback peers, so without one the request would die at the
    auth gate (401) before ever reaching the route under test.
    """
    import subprocess as _sp

    from routes.api_v1 import browser as browser_routes

    from lib.storage import start_storage
    from lib.api_keys import create_key, revoke_key
    start_storage()
    credential, token = create_key(name='open-extensions regression', scopes=[], owner_user_id=1, admin=True)
    calls = []

    def _fake_popen(argv, **kwargs):
        calls.append({'argv': argv, 'kwargs': kwargs})
        return object()

    monkeypatch.setattr(_sp, 'Popen', _fake_popen)
    # The route now resolves the browser through a PROBE that also reports the
    # family-specific extensions URL (chrome:// vs edge://) — the Chrome-only
    # _find_chrome_binary this used to patch is gone. `chrome=None` still
    # means "no browser on this machine".
    monkeypatch.setattr(
        browser_routes, '_detect_local_browser',
        lambda: ({'binary': chrome, 'family': 'chrome', 'name': 'Chrome',
                  'extensionsUrl': 'chrome://extensions'} if chrome else None))
    try:
        resp = flask_client.post(
            '/api/v4/browser/open-extensions',
            headers={'Authorization': 'Bearer ' + token},
            scope_base={'client': client_addr})
        return resp.status_code, (resp.get_json(silent=True) or {}), calls
    finally:
        revoke_key(credential['id'], owner_user_id=1)


def test_open_extensions_refuses_a_remote_peer(flask_client, monkeypatch):
    """The page opens on the SERVER — a remote peer is refused with NO spawn.

    An authenticated remote user is exactly who the gate exists for: valid
    credential, wrong machine. Spawning here would open a browser window on
    a headless server nobody is looking at.
    """
    status, body, calls = _post_open_extensions(
        flask_client, monkeypatch, ('203.0.113.7', 5555))
    assert status == 403, (
        f"a non-loopback peer must be refused — got {status} with {body}")
    assert calls == [], (
        f"the route spawned a browser for a REMOTE peer: {calls}")


def test_open_extensions_argv_is_shell_free(flask_client, monkeypatch):
    """The launch is a list-form argv headed by the browser binary."""
    status, body, calls = _post_open_extensions(
        flask_client, monkeypatch, ('127.0.0.1', 5555))
    assert status == 200, f"loopback launch failed: {status} {body}"
    assert body.get('ok') is True
    assert len(calls) == 1, f"expected exactly one spawn, got {calls}"
    argv, kwargs = calls[0]['argv'], calls[0]['kwargs']
    assert isinstance(argv, list) and argv[0] == '/fake/chrome', (
        f"argv must be list-form headed by the browser binary, got {argv!r}")
    assert 'chrome://extensions' in argv, (
        f"the extensions page URL is missing from {argv!r}")
    assert kwargs.get('shell') is not True, (
        "shell=True today is where the next interpolated argument becomes "
        "an injection vector tomorrow")


def test_open_extensions_reports_when_no_chrome_exists(flask_client,
                                                       monkeypatch):
    """No Chrome found → a clear 404, and NOTHING is spawned.

    The UI keeps the manual instruction for this case; the route must say
    'not found', not crash and not pretend success.
    """
    status, body, calls = _post_open_extensions(
        flask_client, monkeypatch, ('127.0.0.1', 5555), chrome=None)
    assert status == 404, (
        f"expected 404 when no browser exists, got {status} with {body}")
    assert calls == []
