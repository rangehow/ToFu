"""Desktop Agent — egress executor (``egress_http``).

Executes an HTTP request from the USER's machine (which typically has the
working proxy/VPN) on behalf of the Tofu server, whose own egress may be
geo-blocked. Only subscription-provider hosts are reachable: the whitelist
is hardcoded HERE and re-checked on every command — the agent never trusts
a server-supplied "extra allow" field (defense in depth, design §7.1).

Proxy discovery order for ``proxy_mode='env'`` (the default):
  process env vars → OS system proxy (Clash "system proxy" writes the
  registry / macOS system settings, NOT env vars — without this step the
  agent would still go out naked on the user's machine and the whole
  scheme spins its wheels) → direct.

Log hygiene: request/response bodies and headers carry the user's
subscription access token — this module logs only method/host/status/ms.
"""

from __future__ import annotations

import base64
import os
import subprocess
import time
from urllib.parse import urlparse

import requests

from lib.log import get_logger

logger = get_logger(__name__)

#: Hardcoded agent-side whitelist (mirror of lib/desktop/egress.py —
#: deliberately a SECOND copy: a compromised server cannot widen it).
_ALLOWED_HOSTS = frozenset({
    'api.anthropic.com',
    'console.anthropic.com',
    'claude.ai',
    'auth.openai.com',
    'auth0.openai.com',
    'chatgpt.com',
    'api.openai.com',
})

_MAX_BODY_BYTES = 2 * 1024 * 1024       # request body cap (design §7.3)
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # non-stream response cap

try:
    from lib.compat import IS_WINDOWS as _IS_WINDOWS, IS_MACOS as _IS_MACOS
except Exception:  # pragma: no cover - compat shim always present in-app
    import sys as _sys
    _IS_WINDOWS = _sys.platform.startswith('win')
    _IS_MACOS = _sys.platform == 'darwin'


def _host_allowed(url: str) -> bool:
    try:
        return (urlparse(url).hostname or '').lower() in _ALLOWED_HOSTS
    except Exception:
        return False


def _windows_proxy_url() -> str:
    """Read the Windows "system proxy" (Clash's 允许局域网/系统代理 writes
    here) from the registry. '' when disabled/unavailable."""
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        enable, _ = winreg.QueryValueEx(key, 'ProxyEnable')
        server, _ = winreg.QueryValueEx(key, 'ProxyServer')
        if enable and server:
            server = str(server).strip()
            # ProxyServer can be "http=host:1;https=host:2" — prefer https.
            if '=' in server and '://' not in server:
                parts = dict(p.split('=', 1) for p in server.split(';') if '=' in p)
                server = parts.get('https') or parts.get('http') or ''
            if server:
                return server if '://' in server else f'http://{server}'
    except Exception as e:
        logger.debug('[Egress] winreg proxy read failed: %s', e)
    return ''


def _macos_proxy_url() -> str:
    """Parse ``scutil --proxy`` for the macOS system proxy (HTTPS preferred)."""
    try:
        out = subprocess.run(
            ['scutil', '--proxy'], capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return ''
        kv = {}
        for line in out.stdout.splitlines():
            line = line.strip()
            if ' : ' in line:
                k, v = line.split(' : ', 1)
                kv[k.strip()] = v.strip()
        for proto in ('HTTPS', 'HTTP'):
            if kv.get(f'{proto}Enable') == '1' and kv.get(f'{proto}Proxy'):
                host = kv[f'{proto}Proxy']
                port = kv.get(f'{proto}Port', '8080')
                return f'http://{host}:{port}'
    except Exception as e:
        logger.debug('[Egress] scutil proxy read failed: %s', e)
    return ''


def _os_proxy_url() -> str:
    """OS-level system proxy URL, '' when none/disabled/unsupported."""
    if _IS_WINDOWS:
        return _windows_proxy_url()
    if _IS_MACOS:
        return _macos_proxy_url()
    return ''


def _resolve_proxies(mode: str):
    """Map proxy_mode → requests ``proxies=`` argument.

    ``direct`` → bypass everything (incl. env); ``env``/``auto`` → let
    requests honor env vars (None), falling back to the OS system proxy
    when env is silent. ``auto`` additionally retries direct on connection
    failure (handled by the caller).
    """
    if mode == 'direct':
        return {'no_proxy': '*'}
    env_has = any(os.environ.get(k) for k in
                  ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'))
    if env_has:
        return None  # requests honors env vars itself
    os_proxy = _os_proxy_url()
    if os_proxy:
        return {'http': os_proxy, 'https': os_proxy}
    return None


def _do_request(method, url, headers, body, timeout_s, proxies):
    t0 = time.monotonic()
    resp = requests.request(method, url, headers=headers, data=body,
                            timeout=timeout_s, proxies=proxies, stream=False)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    content = resp.content
    if len(content) > _MAX_RESPONSE_BYTES:
        return {'status': 0,
                'error': f'response too large ({len(content)} bytes > {_MAX_RESPONSE_BYTES})'}
    return {
        'status': resp.status_code,
        'headers': {k: v for k, v in resp.headers.items()
                    if k.lower() != 'set-cookie'},
        'body_b64': base64.b64encode(content).decode('ascii'),
        'elapsed_ms': elapsed_ms,
    }


def cmd_egress_http(params: dict) -> dict:
    """Execute one HTTP request on behalf of the server (``egress_http``).

    Params: ``url`` (whitelisted), ``method``, ``headers``, ``body_b64``,
    ``timeout_ms``, ``proxy_mode`` (env/direct/auto). Returns the wire
    result dict; network failures come back as ``status: 0`` + ``error``.
    """
    url = str((params or {}).get('url') or '')
    if not _host_allowed(url):
        logger.warning('[Egress] REFUSED non-whitelisted host: %s',
                       (urlparse(url).hostname or url)[:80])
        return {'error': 'egress host not in agent whitelist'}
    method = str(params.get('method') or 'POST').upper()
    if method not in ('GET', 'POST'):
        return {'error': f'unsupported method: {method}'}
    try:
        body = base64.b64decode(params.get('body_b64') or '')
    except Exception as e:
        return {'error': f'bad body_b64: {e}'}
    if len(body) > _MAX_BODY_BYTES:
        return {'error': f'request body too large ({len(body)} bytes)'}
    headers = params.get('headers') or {}
    if not isinstance(headers, dict):
        return {'error': 'headers must be a dict'}
    timeout_s = min(max(int(params.get('timeout_ms') or 30000) / 1000.0, 1), 60)
    mode = str(params.get('proxy_mode') or 'env')

    host = urlparse(url).hostname or '?'
    try:
        out = _do_request(method, url, headers, body, timeout_s,
                          _resolve_proxies(mode))
        logger.info('[Egress] %s %s → %s (%sms)',
                    method, host, out.get('status'), out.get('elapsed_ms', '?'))
        return out
    except (requests.ConnectionError, requests.Timeout) as e:
        if mode == 'auto':
            logger.info('[Egress] %s via proxy failed (%s) — retrying direct',
                        host, e)
            try:
                out = _do_request(method, url, headers, body, timeout_s,
                                  {'no_proxy': '*'})
                logger.info('[Egress] %s %s direct → %s', method, host,
                            out.get('status'))
                return out
            except Exception as e2:
                logger.warning('[Egress] %s direct retry failed: %s', host, e2)
                return {'status': 0, 'error': f'{type(e2).__name__}: {e2}'}
        logger.warning('[Egress] %s %s network error: %s', method, host, e)
        return {'status': 0, 'error': f'{type(e).__name__}: {e}'}
    except Exception as e:
        logger.error('[Egress] %s %s failed: %s', method, host, e, exc_info=True)
        return {'status': 0, 'error': f'{type(e).__name__}: {e}'}
