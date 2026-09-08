"""HTTP wiring contract for the local Android APK distribution surface."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.api


def _app():
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import mobile as route

    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_mobile_bp)
    return app


def _point_at(monkeypatch: pytest.MonkeyPatch, path) -> None:
    monkeypatch.setenv('TOFU_ANDROID_APK_PATH', str(path))


def test_apk_route_404_when_no_local_build(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _point_at(monkeypatch, tmp_path / 'missing.apk')

    async def _run():
        response = await _app().test_client().get(
            '/api/v1/mobile/android/apk')
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 404
    assert 'assembleRelease' in str(body)


def test_apk_route_serves_bytes_with_apk_mime(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(b'PK\x03\x04fake-apk-bytes')
    _point_at(monkeypatch, apk)

    async def _run():
        response = await _app().test_client().get(
            '/api/v1/mobile/android/apk')
        return (response.status_code, response.headers, await response.data)

    status, headers, data = asyncio.run(_run())
    assert status == 200
    assert headers['Content-Type'].startswith(
        'application/vnd.android.package-archive')
    assert 'tofu-android.apk' in headers['Content-Disposition']
    assert data == b'PK\x03\x04fake-apk-bytes'


def test_link_route_embeds_presented_token_and_qr(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(b'PK\x03\x04fake')
    _point_at(monkeypatch, apk)

    async def _run():
        response = await _app().test_client().get(
            '/api/v1/mobile/android/link',
            headers={
                'Authorization': 'Bearer tofu_admin_testtoken',
                'Host': '192.168.1.20:8080',
            })
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    data = body['data'] if 'data' in body else body
    assert data['available'] is True
    assert data['tokenEmbedded'] is True
    assert data['url'] == (
        'http://192.168.1.20:8080/api/v1/mobile/android/apk'
        '?token=tofu_admin_testtoken')
    assert data['qr'].startswith('data:image/png;base64,')
    assert data['sizeBytes'] == len(b'PK\x03\x04fake')
    assert isinstance(data['modifiedMs'], int)


def test_link_route_without_token_reports_not_embedded(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _point_at(monkeypatch, tmp_path / 'missing.apk')

    async def _run():
        response = await _app().test_client().get(
            '/api/v1/mobile/android/link',
            headers={'Host': '10.0.0.5:8080'})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    data = body['data'] if 'data' in body else body
    assert data['available'] is False
    assert data['tokenEmbedded'] is False
    assert data['url'] == 'http://10.0.0.5:8080/api/v1/mobile/android/apk'
    assert 'sizeBytes' not in data


def test_scan_page_renders_qr_for_authenticated_browser(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    apk = tmp_path / 'app-release.apk'
    apk.write_bytes(b'PK\x03\x04fake')
    _point_at(monkeypatch, apk)

    async def _run():
        response = await _app().test_client().get(
            '/api/v1/mobile/android',
            headers={'Host': '192.168.1.20:8080'})
        return response.status_code, await response.data

    status, data = asyncio.run(_run())
    assert status == 200
    html = data.decode()
    assert 'data:image/png;base64,' in html
    assert 'APK ready' in html


def test_routes_require_authentication(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from quart import Quart
    from routes.api_v1 import mobile as route

    _point_at(monkeypatch, tmp_path / 'missing.apk')
    app = Quart(__name__)
    app.register_blueprint(route.api_v1_mobile_bp)

    async def _run():
        client = app.test_client()
        return [(await client.get(path)).status_code for path in (
            '/api/v1/mobile/android',
            '/api/v1/mobile/android/apk',
            '/api/v1/mobile/android/link',
        )]

    assert asyncio.run(_run()) == [401, 401, 401]
