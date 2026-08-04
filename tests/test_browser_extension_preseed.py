"""tests/test_browser_extension_preseed.py — zero-input pairing guards.

Owner directive 2026-08-03: the downloaded extension package must pair with
ZERO user input — nobody pastes a key that only the backend can mint. The
server bakes a freshly-minted ``agents:bridge`` credential + the request's
own origin into the zip as ``browser_extension/bridge_preseed.json``; the
extension adopts it into EMPTY storage slots only (a user-configured value
always wins).

Covers, without a running server:
  * the download route injects a preseed whose secret actually resolves
    through lib.bridge_auth (the same chain the poll gate consumes);
  * each download mints a DISTINCT key (secrets are hash-stored, so a key
    can never be re-materialised for a later package);
  * mint failure degrades to a zip WITHOUT the preseed (fail-open, logged),
    never to a failed download;
  * background.js adopts the preseed only into empty slots, before server
    detection, and tolerates an absent preseed file;
  * the popup no longer presents the secret as user-required.
"""

import io
import json
import os
import zipfile
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


@pytest.fixture()
def _isolated_key_store(tmp_path):
    """Point lib.api_keys at a temp store so the production file is untouched."""
    with patch('lib.api_keys._STORE_PATH',
               os.path.join(str(tmp_path), 'api_keys.json')):
        from lib import api_keys
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        yield
        api_keys._cache.clear()
        api_keys._cache_loaded = False


def _get_zip():
    """Run the real download handler in a bare Quart app (no auth gate).

    Returns ``(status_code, body_bytes)`` — Quart's test client defers the
    body behind an awaitable, so it is materialised here.
    """
    import asyncio
    from quart import Quart
    from routes.browser import browser_bp

    app = Quart(__name__)
    app.register_blueprint(browser_bp)

    async def _get():
        client = app.test_client()
        resp = await client.get('/api/browser/download')
        return resp.status_code, await resp.get_data()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_get())
    finally:
        loop.close()


class TestDownloadPreseed:
    def test_zip_carries_working_preseed(self, _isolated_key_store):
        status, body = _get_zip()
        assert status == 200
        zf = zipfile.ZipFile(io.BytesIO(body))
        names = zf.namelist()
        assert 'browser_extension/bridge_preseed.json' in names, (
            'the downloaded package must carry the pre-paired credential')
        pre = json.loads(zf.read('browser_extension/bridge_preseed.json'))
        assert pre['bridgeSecret'].startswith('tofu_live_')
        assert pre['serverUrl'].startswith('http')
        # The baked credential must pass the SAME chain the poll gate uses.
        from lib.bridge_auth import resolve_bridge_credential
        ok, _uid, key_id = resolve_bridge_credential(pre['bridgeSecret'])
        assert ok and key_id, 'baked key must resolve through bridge_auth'
        # The regular payload is untouched.
        assert 'browser_extension/background.js' in names
        assert 'browser_extension/manifest.json' in names

    def test_each_download_mints_a_distinct_key(self, _isolated_key_store):
        _s1, body1 = _get_zip()
        _s2, body2 = _get_zip()
        pre1 = json.loads(zipfile.ZipFile(io.BytesIO(body1))
                          .read('browser_extension/bridge_preseed.json'))
        pre2 = json.loads(zipfile.ZipFile(io.BytesIO(body2))
                          .read('browser_extension/bridge_preseed.json'))
        assert pre1['bridgeSecret'] != pre2['bridgeSecret']

    def test_mint_failure_degrades_to_plain_zip(self, _isolated_key_store):
        with patch('lib.api_keys.create_key',
                   side_effect=RuntimeError('store down')):
            status, body = _get_zip()
        assert status == 200
        zf = zipfile.ZipFile(io.BytesIO(body))
        assert 'browser_extension/bridge_preseed.json' not in zf.namelist()
        # The extension itself is still fully served.
        assert 'browser_extension/background.js' in zf.namelist()


class TestExtensionAdoption:
    def test_preseed_adoption_is_structured(self):
        src = _src('browser_extension/background.js')
        assert 'function adoptBridgePreseed(' in src
        # Reads the packaged file through the extension's own URL resolver.
        assert "chrome.runtime.getURL('bridge_preseed.json')" in src
        # Empty-slot-only adoption: a user-configured value always wins.
        assert 'if (storageData.bridgeSecret && storageData.serverUrl)' in src
        assert '!storageData.bridgeSecret' in src
        assert '!storageData.serverUrl' in src
        # An absent preseed file must be tolerated silently.
        assert '.catch(() =>' in src

    def test_adoption_runs_before_server_detection(self):
        src = _src('browser_extension/background.js')
        # init() must await adoption before autoDetectServer so a preseeded
        # serverUrl is visible to detection.
        assert 'adoptBridgePreseed(data).then(autoDetectServer)' in src
        # init now reads serverUrl too (adoption needs to know if it's empty).
        assert "['clientId', 'bridgeSecret', 'serverUrl']" in src

    def test_popup_no_longer_marks_secret_user_required(self):
        html = _src('browser_extension/popup.html')
        assert 'required — an agents:bridge' not in html
        # 2026-08-04: the field moved inside a collapsed "Advanced" details
        # whose summary says so explicitly — the visible remedy is the
        # automatic re-pair row, never a secret to paste (the structural
        # details pin lives in test_browser_bridge_auto_repair.py).
        assert 'never needed in normal use' in html
