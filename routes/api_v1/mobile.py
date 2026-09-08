"""Serve the repo-built Android APK over the LAN.

The release APK produced by ``./gradlew assembleRelease`` (same signing
config as the GitHub-published artifact) is exposed directly by this server,
so a phone on the same network installs the exact build the operator just
compiled — no GitHub round-trip and no remote/local divergence.

Endpoints:
  * ``GET /api/v1/mobile/android/apk``  — the APK attachment itself.
  * ``GET /api/v1/mobile/android/link`` — JSON: absolute URL (with the
    caller's credential embedded via the ``?token=`` first-link transport
    when one was presented) + a QR ``data:`` URI of that URL.
  * ``GET /api/v1/mobile/android``      — minimal scan-me page rendering
    the QR in the operator's browser.

The APK location defaults to the in-repo Gradle output and can be pointed
elsewhere with ``TOFU_ANDROID_APK_PATH``. All endpoints sit behind the
normal auth gate; the phone authenticates through the query-token redirect
the first link carries.
"""

from __future__ import annotations

import os
from pathlib import Path

from quart import Blueprint, Response, request

from lib.api_response import api_not_found, api_ok
from lib.file_serving import send_file_conditional
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_mobile_bp = Blueprint('api_v1_mobile', __name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_APK = (_REPO_ROOT / 'android' / 'app' / 'build' / 'outputs' /
                'apk' / 'release' / 'app-release.apk')
_APK_MIME = 'application/vnd.android.package-archive'
_APK_URL_PATH = '/api/v1/mobile/android/apk'


def _apk_path() -> Path:
    override = os.environ.get('TOFU_ANDROID_APK_PATH', '').strip()
    return Path(override) if override else _DEFAULT_APK


def _presented_token() -> str:
    """Raw credential the current request arrived with ('' when none).

    The session cookie stores the raw token, so a normal browser session
    yields its credential here without any keystore lookup.
    """
    from .auth import _extract_bearer_or_cookie
    return _extract_bearer_or_cookie()


def _download_url() -> str:
    url = f'{request.scheme}://{request.host}{_APK_URL_PATH}'
    token = _presented_token()
    if token:
        url = f'{url}?token={token}'
    return url


@api_v1_mobile_bp.route(_APK_URL_PATH, methods=['GET'])
@require_auth
@api_meta(summary='Download the locally built Android APK', tags=['mobile'])
def mobile_android_apk_v1():
    path = _apk_path()
    if not path.is_file():
        return api_not_found(
            'No local APK build found. Run ./gradlew assembleRelease in '
            'android/ first (or set TOFU_ANDROID_APK_PATH).')
    response = send_file_conditional(
        path, mimetype=_APK_MIME,
        download_name='tofu-android.apk', as_attachment=True)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@api_v1_mobile_bp.route('/api/v1/mobile/android/link', methods=['GET'])
@require_auth
@api_meta(summary='Absolute download URL + QR for the local Android APK',
          tags=['mobile'])
def mobile_android_link_v1():
    from lib.qr import qr_png_data_uri

    url = _download_url()
    payload = {
        'available': _apk_path().is_file(),
        'url': url,
        'tokenEmbedded': bool(_presented_token()),
        'qr': qr_png_data_uri(url),
    }
    path = _apk_path()
    if payload['available']:
        stat = path.stat()
        payload['sizeBytes'] = stat.st_size
        payload['modifiedMs'] = int(stat.st_mtime * 1000)
    return api_ok(payload)


@api_v1_mobile_bp.route('/api/v1/mobile/android', methods=['GET'])
@require_auth
@api_meta(summary='Scan-to-install page for the local Android APK',
          tags=['mobile'])
def mobile_android_page_v1():
    from html import escape

    from lib.qr import qr_png_data_uri

    url = _download_url()
    qr = qr_png_data_uri(url)
    path = _apk_path()
    if path.is_file():
        stat = path.stat()
        from datetime import datetime
        built = datetime.fromtimestamp(stat.st_mtime).strftime(
            '%Y-%m-%d %H:%M')
        status = (f'APK ready — {stat.st_size / (1024 * 1024):.1f} MiB, '
                  f'built {built}')
    else:
        status = ('No local build yet — run '
                  '<code>./gradlew assembleRelease</code> in android/.')
    img = (f'<img src="{qr}" width="264" height="264" alt="APK download QR">'
           if qr else
           '<p><em>QR unavailable (qrcode package missing) — use the link '
           'below.</em></p>')
    html = (
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Tofu Android — local install</title>'
        '<style>body{font:15px/1.6 system-ui,sans-serif;margin:3em auto;'
        'max-width:26em;padding:0 1.2em;color:#222;text-align:center}'
        'h1{font-size:1.3rem}code{background:#f3f3f3;padding:.1em .4em;'
        'border-radius:3px;word-break:break-all}'
        'p.hint{color:#666;font-size:.85rem}</style>'
        '<h1>Tofu Android — local build</h1>'
        f'<p>{status}</p>{img}'
        f'<p class="hint">Same Wi-Fi as this server, scan to download.<br>'
        f'<a href="{escape(url, quote=True)}">{escape(url)}</a></p>')
    return Response(html, content_type='text/html; charset=utf-8')
