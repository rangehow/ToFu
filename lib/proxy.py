"""lib/proxy.py — Centralized proxy configuration.

Manages **two** concerns that are unified for the user:

1. **Proxy address** — the ``http_proxy`` / ``https_proxy`` values that
   ``requests`` uses.  These can come from environment variables (traditional)
   **or** from the Settings UI (persisted to ``server_config.json``).

2. **Proxy bypass** — domain suffixes / hosts whose traffic should bypass
   the proxy entirely.  Configured via one Settings UI field (or the
   ``PROXY_BYPASS_DOMAINS`` env var).  Under the hood, bypass domains
   feed **both**:

   - Per-request bypass via ``proxies_for(url)`` (suffix match)
   - Global ``no_proxy`` environment variable (for any code using
     ``requests`` directly without explicit ``proxies=`` kwarg)

Usage in any module::

    from lib.proxy import proxies_for

    resp = requests.post(url, json=body, proxies=proxies_for(url), timeout=30)

The Settings UI (Network tab) lets users configure the proxy address
and a single unified bypass list without touching environment variables.

**Proxy pool** (2026-08-07, epic pt_bb2389f3): an ordered, scoped list of
proxy entries persisted as ``proxy_pool`` in server_config.json. Entries
carry a ``scope`` — ``subscription`` applies ONLY to
:data:`SUBSCRIPTION_HOSTS` (the OAuth provider endpoints, so a proxy that
can reach them need not bend ALL traffic), ``global`` applies to any
non-bypassed host. Credentials never persist in the URL: the save path
splits userinfo into the credentials vault (``proxy_<id>_auth``) and
stores a stripped URL. Per-entry health (consecutive failures → 60s
cooldown) is fed by :func:`report_outcome` — the same real-traffic feed
netpath uses — and by the egress subscription probe. An EMPTY pool
preserves the legacy single-proxy/env behaviour byte for byte.
"""

import os
import re
import threading
import time
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'proxies_for', 'report_outcome',
    'get_bypass_domains', 'set_bypass_domains',
    'get_proxy_config', 'set_proxy_config',
    'register_no_proxy_host', 'register_no_proxy_url',
    'async_proxy_for',
    'SUBSCRIPTION_HOSTS', 'is_subscription_host',
    'sanitize_proxy_pool', 'get_proxy_pool', 'set_proxy_pool',
    'pool_probe_entries', 'pool_note_outcome', 'test_proxy_entry',
    'first_global_proxy_url',
]


# ── Adaptive path selection (lib.netpath), lazily wired ──
# Kept behind a lazy import so lib.proxy stays importable in minimal
# contexts; a missing/broken netpath module degrades to env behaviour.
_netpath_mod = None
# One-shot guard so a broken netpath on the per-request hot path (proxies_for)
# logs a single warning instead of one line per request.
_np_decide_warned = False


def _np():
    global _netpath_mod
    if _netpath_mod is None:
        try:
            from lib import netpath as _m
            _netpath_mod = _m
        except Exception as e:
            # Runs once (the result is cached). Without this trace a broken /
            # missing netpath silently degrades to env behaviour with no signal.
            logger.debug('[Proxy] netpath unavailable — adaptive path selection off: %s', e)
            _netpath_mod = False
    return _netpath_mod or None


def report_outcome(url: str, ok: bool, latency_ms=None) -> None:
    """Forward a real request outcome to the netpath scorer AND attribute
    it to the pool entry the request actually used (when one did).

    Called by the LLM transports and lib.http_client on success/failure.
    Never raises; a no-op when netpath is disabled or the host is not
    managed (explicit bypass rules win over learned decisions).
    """
    np = _np()
    if np is not None:
        try:
            np.report_outcome(url, ok, latency_ms)
        except Exception as e:
            # If this keeps firing the netpath scorer freezes on stale scores and
            # adaptive routing silently stops learning — surface it.
            logger.debug('[Proxy] netpath.report_outcome failed for %s: %s', url, e)
    try:
        host = (urlparse(url).hostname or '').lower()
        with _pool_lock:
            pid = _pool_choice.get(host) if host else None
        if pid:
            pool_note_outcome(pid, ok, latency_ms)
    except Exception as e:
        logger.debug('[Proxy] pool outcome attribution failed for %s: %s', url, e)

# ── The "real" bypass dict that makes requests skip env proxies ──
# NOTE: ``{'http': None, 'https': None}`` does NOT reliably bypass in all
# requests versions.  ``{'no_proxy': '*'}`` is the only fully reliable method.
_NO_PROXY = {'no_proxy': '*'}

# ── Standard always-bypass entries (never need a proxy) ──
_ALWAYS_BYPASS = ('localhost', '127.0.0.1', '0.0.0.0')

_lock = threading.Lock()

# ═══════════════════════════════════════════════════════
#  Proxy Address (http_proxy / https_proxy)
# ═══════════════════════════════════════════════════════
# Snapshot the *original* env vars at import time so we can tell the UI
# what came from the environment vs what was set via Settings.

_ENV_HTTP_PROXY = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY', '')
_ENV_HTTPS_PROXY = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY', '')
_ENV_NO_PROXY = os.environ.get('no_proxy') or os.environ.get('NO_PROXY', '')

# Persisted proxy config (empty = not configured via Settings, use env)
_proxy_config: dict = {}   # keys: http_proxy, https_proxy


def get_proxy_config() -> dict:
    """Return the current proxy configuration for the Settings UI.

    Returns a dict with:
      - ``http_proxy``, ``https_proxy``: effective values
      - ``no_proxy``: effective no_proxy env var (read-only, auto-managed)
      - ``env_*``: original env values (read-only)
      - ``configured``: whether proxy was set via Settings
    """
    return {
        'http_proxy':  _proxy_config.get('http_proxy', '') or _ENV_HTTP_PROXY,
        'https_proxy': _proxy_config.get('https_proxy', '') or _ENV_HTTPS_PROXY,
        'no_proxy':    os.environ.get('no_proxy', ''),  # effective, read-only
        'env_http_proxy':  _ENV_HTTP_PROXY,
        'env_https_proxy': _ENV_HTTPS_PROXY,
        'env_no_proxy':    _ENV_NO_PROXY,
        'configured': bool(_proxy_config),
    }


def set_proxy_config(http_proxy: str = '', https_proxy: str = ''):
    """Apply proxy address configuration at runtime.

    Updates ``os.environ`` so all ``requests`` calls pick up the new
    values immediately.

    Bypass domains (the ``no_proxy`` env var) are managed solely by
    ``set_bypass_domains()``, which auto-syncs to the ``no_proxy``
    environment variable — this function only touches the proxy address.

    Called by ``routes/config.py`` on Settings save and by ``server.py``
    at startup when loading persisted config.

    Args:
        http_proxy:  HTTP proxy URL (e.g. ``http://10.0.0.1:8080``), or
                     empty string to clear (fall back to env).
        https_proxy: HTTPS proxy URL, or empty to clear.
    """
    global _proxy_config
    with _lock:
        prev_effective = (
            _proxy_config.get('http_proxy', '') or _ENV_HTTP_PROXY,
            _proxy_config.get('https_proxy', '') or _ENV_HTTPS_PROXY,
        )
        _proxy_config = {
            'http_proxy':  http_proxy.strip(),
            'https_proxy': https_proxy.strip(),
        }
        # Apply to environment — requests reads these on every call
        _apply_to_env('http_proxy',  http_proxy.strip() or _ENV_HTTP_PROXY)
        _apply_to_env('https_proxy', https_proxy.strip() or _ENV_HTTPS_PROXY)
        # no_proxy is auto-managed — sync it so state is consistent
        _sync_no_proxy()
        new_effective = (
            _proxy_config.get('http_proxy', '') or _ENV_HTTP_PROXY,
            _proxy_config.get('https_proxy', '') or _ENV_HTTPS_PROXY,
        )
    # A changed proxy address invalidates every proxy-path measurement.
    if new_effective != prev_effective:
        _on_proxy_topology_changed()

    logger.info('[Proxy] Config updated: http=%s https=%s',
                http_proxy.strip() or '(env)', https_proxy.strip() or '(env)')


def _apply_to_env(key: str, value: str):
    """Set both lower-case and UPPER-CASE env vars for maximum compatibility."""
    if value:
        os.environ[key] = value
        os.environ[key.upper()] = value
    else:
        os.environ.pop(key, None)
        os.environ.pop(key.upper(), None)



# ═══════════════════════════════════════════════════════
#  Proxy Pool (ordered, scoped, health-tracked — 2026-08-07)
# ═══════════════════════════════════════════════════════
# Motivation (epic pt_bb2389f3, owner directive): a proxy that CAN reach
# the subscription providers (OpenAI/Anthropic OAuth endpoints) must not
# bend ALL outbound traffic. The pool is an ordered list of scoped entries
# persisted as ``proxy_pool`` in server_config.json; an EMPTY pool
# preserves the legacy single-proxy/env behaviour byte for byte.
#
#   scope 'subscription' — ONLY :data:`SUBSCRIPTION_HOSTS` (OAuth paths:
#                          token exchange/refresh + subscription streams,
#                          plus the egress layer's reachability probe)
#   scope 'global'       — any non-bypassed host
#
# Credentials never persist in the config: the save path splits URL
# userinfo into the credentials vault (``proxy_<id>_auth``) and stores a
# stripped URL. Credentialed entries are subscription-only (owner rule) —
# a global proxy WITH credentials remains servable by env / legacy
# proxy_config.
#
# Health: per-entry consecutive-failure counting fed by report_outcome()
# (the same real-traffic feed netpath learns from) and by the egress
# probe. ``_POOL_FAIL_THRESHOLD`` consecutive failures → 60s cooldown;
# the ordered pool is the failover chain (no blind round-robin).

#: OAuth provider endpoints whitelisted for desktop egress AND served by
#: the ``subscription`` proxy scope. Single source of truth —
#: ``lib.desktop.egress.ALLOWED_EGRESS_HOSTS`` aliases this set.
SUBSCRIPTION_HOSTS = frozenset({
    'api.anthropic.com',
    'console.anthropic.com',
    'platform.claude.com',
    'claude.ai',
    'auth.openai.com',
    'auth0.openai.com',
    'chatgpt.com',
    'api.openai.com',
})

_SCOPE_GLOBAL = 'global'
_SCOPE_SUBSCRIPTION = 'subscription'
_POOL_SCOPES = (_SCOPE_GLOBAL, _SCOPE_SUBSCRIPTION)
_POOL_MAX = 16
_POOL_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
_POOL_FAIL_THRESHOLD = 2
_POOL_COOLDOWN_S = 60.0
_CRED_CACHE_TTL_S = 60.0

_pool_lock = threading.Lock()
_proxy_pool: list = []          # sanitized entries (persisted shape)
_pool_health: dict = {}         # id → {fails, ewma_ms, samples, cooldown_until}
_pool_choice: dict = {}         # host → entry id (report_outcome attribution)
_cred_cache: dict = {}          # vault name → (value|None, fetched_at epoch)


def is_subscription_host(host: str) -> bool:
    """True when *host* is one of the OAuth subscription provider endpoints."""
    return (host or '').lower() in SUBSCRIPTION_HOSTS


def _on_proxy_topology_changed() -> None:
    """A proxy topology change invalidates every cached routing verdict:
    the egress subscription probe cache (2026-08-07 root fix — a stale
    ``geo_blocked`` verdict kept misrouting subscription traffic to desktop
    agents for up to 300s after the proxy changed) and netpath's per-host
    proxy stats."""
    try:
        from lib.desktop import egress as _eg
        _eg.invalidate_probe_cache()
    except Exception as e:
        logger.debug('[Proxy] egress probe invalidation failed: %s', e)
    np = _np()
    if np is not None:
        try:
            np.reset_proxy_stats()
        except Exception as e:
            logger.warning('[Proxy] netpath.reset_proxy_stats failed after '
                           'proxy topology change: %s', e)


def _mint_pool_id(name: str, host: str, taken: set) -> str:
    """Derive a stable slug id from the display name (else the host)."""
    base = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    if not base:
        base = re.sub(r'[^a-z0-9]+', '-', (host or '').lower()).strip('-')
    base = (base or 'proxy')[:24].strip('-') or 'proxy'
    cid, n = base, 2
    while cid in taken:
        cid = f'{base}-{n}'
        n += 1
    return cid


def sanitize_proxy_pool(raw_list):
    """Validate + normalize a raw ``proxy_pool`` payload.

    Returns ``(entries, creds, error)``: ``entries`` is the SANITIZED
    persisted form (URLs stripped of userinfo and path), ``creds`` maps
    entry id → plaintext ``user:pass`` destined for the credentials vault
    (never written to the config), ``error`` is a human-readable message
    when the payload is invalid.
    """
    if not isinstance(raw_list, list):
        return None, None, 'proxy_pool must be an array'
    if len(raw_list) > _POOL_MAX:
        return None, None, f'proxy_pool holds at most {_POOL_MAX} entries'
    entries = []
    creds = {}
    taken = set()
    for idx, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            return None, None, f'entry #{idx + 1} is not an object'
        raw_url = str(raw.get('url') or '').strip()
        if not raw_url:
            return None, None, f'entry #{idx + 1}: url is required'
        try:
            parts = urlsplit(raw_url if '://' in raw_url
                             else 'http://' + raw_url)
            scheme = (parts.scheme or '').lower()
            host = parts.hostname
            port = parts.port  # ValueError on a bad port
        except (ValueError, TypeError) as e:
            return None, None, f'entry #{idx + 1}: unparseable url ({e})'
        if scheme not in ('http', 'https'):
            return None, None, (
                f"entry #{idx + 1}: scheme {scheme!r} unsupported — "
                'http/https only')
        if not host:
            return None, None, f'entry #{idx + 1}: url carries no host'
        # userinfo → vault credential (the URL itself never persists it)
        cred = str(raw.get('credential') or '').strip()
        if parts.username:
            cred = '%s:%s' % (unquote(parts.username),
                              unquote(parts.password or ''))
        vault_ref = str(raw.get('credential_vault') or '').strip()
        scope = str(raw.get('scope') or _SCOPE_SUBSCRIPTION).strip().lower()
        if scope not in _POOL_SCOPES:
            return None, None, (
                f"entry #{idx + 1}: scope must be one of {_POOL_SCOPES}")
        if (cred or vault_ref) and scope == _SCOPE_GLOBAL:
            return None, None, (
                f'entry #{idx + 1}: credentialed proxies must be '
                "'subscription' scope — a global proxy with credentials is "
                'what env vars / legacy proxy_config are for')
        entry_id = str(raw.get('id') or '').strip().lower()
        if not _POOL_ID_RE.match(entry_id):
            entry_id = _mint_pool_id(str(raw.get('name') or ''), host, taken)
        if entry_id in taken:
            return None, None, f'entry #{idx + 1}: duplicate id {entry_id!r}'
        taken.add(entry_id)
        hostport = f'[{host}]' if ':' in host else host
        if port is not None:
            hostport += f':{port}'
        entry = {
            'id': entry_id,
            'name': str(raw.get('name') or '').strip()[:40],
            'url': urlunsplit((scheme, hostport, '', '', '')),
            'scope': scope,
            'enabled': bool(raw.get('enabled', True)),
        }
        if cred:
            entry['credential_vault'] = f'proxy_{entry_id}_auth'
            creds[entry_id] = cred
        elif vault_ref:
            entry['credential_vault'] = vault_ref
        entries.append(entry)
    return entries, creds, ''


def get_proxy_pool() -> list:
    """Public, credential-free pool view for the Settings UI."""
    with _pool_lock:
        pool = [dict(e) for e in _proxy_pool]
        health = {k: dict(v) for k, v in _pool_health.items()}
    now = time.monotonic()
    out = []
    for e in pool:
        h = health.get(e.get('id')) or {}
        out.append({
            'id': e.get('id', ''),
            'name': e.get('name', ''),
            'url': e.get('url', ''),
            'scope': e.get('scope', _SCOPE_SUBSCRIPTION),
            'enabled': bool(e.get('enabled', True)),
            'credential_vault': e.get('credential_vault', ''),
            'has_credential': bool(e.get('credential_vault')),
            'health': {
                'fails': int(h.get('fails') or 0),
                'ewma_ms': h.get('ewma_ms'),
                'cooling': bool(h.get('cooldown_until')
                                and now < h['cooldown_until']),
            },
        })
    return out


def set_proxy_pool(entries: list) -> int:
    """Apply a sanitized pool at runtime (persisted shape — no credentials).

    Replaces the WHOLE pool; clears health/choice/credential caches so a
    changed entry never routes on stale measurements, and invalidates all
    learned routing verdicts (:func:`_on_proxy_topology_changed`). Bad rows
    are skipped with a warning (startup loads persisted config). Returns
    the applied entry count.
    """
    global _proxy_pool
    clean = []
    with _pool_lock:
        for e in entries or []:
            if not isinstance(e, dict) or not e.get('url'):
                logger.warning('[Proxy] pool entry skipped (malformed): %s',
                               str(e)[:120])
                continue
            clean.append(dict(e))
        _proxy_pool = clean
        _pool_health.clear()
        _pool_choice.clear()
        _cred_cache.clear()
    _on_proxy_topology_changed()
    logger.info('[Proxy] pool updated: %d entries (%s)', len(clean),
                ', '.join('%s:%s' % (e.get('id') or '?', e.get('scope'))
                          for e in clean) or 'empty')
    return len(clean)


def _pool_candidates(host: str) -> list:
    """Enabled pool entries that may serve *host*, in failover order.

    A subscription host sees subscription entries first, then globals
    (subscription is the more specific intent); any other host sees only
    globals. An empty list → callers take the legacy env path.
    """
    with _pool_lock:
        pool = [e for e in _proxy_pool if e.get('enabled')]
    if not pool:
        return []
    if is_subscription_host(host):
        return ([e for e in pool if e.get('scope') == _SCOPE_SUBSCRIPTION]
                + [e for e in pool if e.get('scope') == _SCOPE_GLOBAL])
    return [e for e in pool if e.get('scope') == _SCOPE_GLOBAL]


def _pick_resolved(entries: list):
    """First entry not in failure cooldown whose credential resolves,
    returned as ``(entry, resolved_url)`` — ``(None, None)`` when none
    qualifies. Pool order is the failover order. An entry whose
    credential is broken counts one failure inline so the NEXT call
    skips it without waiting for transport errors.
    """
    now = time.monotonic()
    with _pool_lock:
        candidates = []
        for e in entries:
            h = _pool_health.get(e.get('id'))
            if (h and h['fails'] >= _POOL_FAIL_THRESHOLD
                    and now < h.get('cooldown_until', 0.0)):
                continue
            candidates.append(dict(e))
    for e in candidates:
        resolved = _resolve_entry(e)
        if resolved:
            return e, resolved
        pool_note_outcome(e.get('id', ''), False)
    return None, None


def _inject_credential(url: str, credential: str) -> 'str | None':
    """Return *url* with ``user:pass`` injected into the authority."""
    user, _, pw = (credential or '').partition(':')
    if not user:
        return None
    userinfo = quote(user, safe='')
    if pw:
        userinfo += ':' + quote(pw, safe='')
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, userinfo + '@' + (parts.netloc or ''),
                       '', '', ''))


def _vault_credential(name: str) -> 'str | None':
    """Vault lookup with a short-lived cache (per-request hot path). Never
    raises and never logs the value."""
    now = time.time()
    with _pool_lock:
        hit = _cred_cache.get(name)
        if hit and now - hit[1] < _CRED_CACHE_TTL_S:
            return hit[0]
    try:
        from lib.credentials_vault import get_entry
        value = get_entry(name)
    except Exception as e:
        logger.warning('[Proxy] vault credential %s unreadable: %s', name, e)
        value = None
    with _pool_lock:
        _cred_cache[name] = (value, now)
    return value


def _resolve_entry(entry: dict) -> 'str | None':
    """The full usable proxy URL for an entry (credentials injected), or
    None when a required credential cannot be resolved."""
    url = (entry.get('url') or '').strip()
    if not url:
        return None
    cv = entry.get('credential_vault') or ''
    if not cv:
        return url
    cred = _vault_credential(cv)
    if not cred:
        return None
    return _inject_credential(url, cred)


def pool_note_outcome(pid: str, ok: bool, latency_ms=None) -> None:
    """Feed one request outcome to an entry's health state."""
    if not pid:
        return
    with _pool_lock:
        h = _pool_health.setdefault(
            pid, {'fails': 0, 'ewma_ms': None, 'samples': 0,
                  'cooldown_until': 0.0})
        if ok:
            was_bad = h['fails'] >= _POOL_FAIL_THRESHOLD
            h['fails'] = 0
            h['cooldown_until'] = 0.0
            if latency_ms is not None and 0 < latency_ms <= 30000:
                h['samples'] += 1
                if h['ewma_ms'] is None:
                    h['ewma_ms'] = float(latency_ms)
                else:
                    h['ewma_ms'] = 0.3 * float(latency_ms) + 0.7 * h['ewma_ms']
        else:
            h['fails'] += 1
            was_bad = h['fails'] > _POOL_FAIL_THRESHOLD
            if h['fails'] == _POOL_FAIL_THRESHOLD:
                h['cooldown_until'] = time.monotonic() + _POOL_COOLDOWN_S
    if not ok and h['fails'] == _POOL_FAIL_THRESHOLD:
        logger.warning('[Proxy] pool entry %s marked unhealthy (%d consecutive '
                       'failures) — failing over for %ds',
                       pid, h['fails'], int(_POOL_COOLDOWN_S))
    elif ok and was_bad:
        logger.info('[Proxy] pool entry %s healthy again', pid)


def pool_probe_entries() -> list:
    """``[(id, resolved_url), …]`` for every enabled subscription entry.

    Used by the egress reachability probe (probing is ALSO the cooldown
    revival path, so cooling entries are included). Entries whose
    credential cannot be resolved are skipped with a warning.
    """
    with _pool_lock:
        pool = [dict(e) for e in _proxy_pool
                if e.get('enabled') and e.get('scope') == _SCOPE_SUBSCRIPTION]
    out = []
    for e in pool:
        resolved = _resolve_entry(e)
        if resolved:
            out.append((e['id'], resolved))
        else:
            logger.warning('[Proxy] subscription pool entry %s skipped in '
                           'probe — credential unresolvable', e.get('id'))
    return out


def first_global_proxy_url() -> str:
    """Resolved URL of the first healthy enabled global entry ('' = none).

    Consumed by lib/search_bridge so tofu-search rides the SAME proxy the
    rest of the app uses; credentials never leave the process.
    """
    with _pool_lock:
        pool = [dict(e) for e in _proxy_pool
                if e.get('enabled') and e.get('scope') == _SCOPE_GLOBAL]
    _pick, resolved = _pick_resolved(pool)
    return resolved or ''


def _scrub_secret(text: str, *secrets: str) -> str:
    """Strip credential material from an error string before it is shown."""
    for s in secrets:
        if s:
            text = text.replace(s, '***')
    return text


def test_proxy_entry(entry: dict, credential: 'str | None' = None) -> dict:
    """Live-probe ONE proxy entry against the subscription canaries.

    POSTs the real OAuth endpoints WITHOUT auth through the entry's proxy —
    any HTTP answer proves the app layer was reached (403 = geo/policy
    block, anything else = path works). Pure diagnostic: no health state is
    mutated and no credential ever leaves in the result.
    """
    if credential is not None and credential:
        resolved = _inject_credential(entry.get('url') or '', credential)
    else:
        resolved = _resolve_entry(entry)
        credential = ''
    if not resolved:
        return {'ok': False, 'results': [],
                'error': 'proxy credential could not be resolved'}
    secret_bits = [resolved]
    if '@' in resolved:
        secret_bits.append(resolved.split('://', 1)[1].split('@', 1)[0])
    if credential:
        secret_bits.append(credential)
    targets = (
        ('https://auth.openai.com/oauth/token', 'OpenAI Auth'),
        ('https://api.anthropic.com/v1/messages', 'Anthropic API'),
    )
    import requests  # local: keep module import light for non-server use
    results = []
    for url, label in targets:
        t0 = time.monotonic()
        try:
            resp = requests.post(
                url, json={}, timeout=(5, 8),
                proxies={'http': resolved, 'https': resolved})
            latency = (time.monotonic() - t0) * 1000.0
            resp.close()
            results.append({
                'target': url, 'label': label,
                'status': resp.status_code,
                'latency_ms': round(latency),
                'verdict': 'geo_blocked' if resp.status_code == 403 else 'ok',
            })
        except Exception as e:
            latency = (time.monotonic() - t0) * 1000.0
            results.append({
                'target': url, 'label': label, 'status': 0,
                'latency_ms': round(latency),
                'verdict': 'network_fail',
                'error': _scrub_secret(str(e), *secret_bits)[:200],
            })
    ok = any(r['verdict'] == 'ok' for r in results)
    return {'ok': ok, 'results': results}


# ═══════════════════════════════════════════════════════
#  Proxy Bypass Domains (unified: per-request + env no_proxy)
# ═══════════════════════════════════════════════════════

# ── Baseline from env var (read once at import time) ──
_env_domains: tuple = tuple(
    d.strip() for d in os.environ.get('PROXY_BYPASS_DOMAINS', '').split(',')
    if d.strip()
)

# ── Dynamic domains set via Settings UI (hot-reloaded) ──
_settings_domains: tuple = ()

# ── Merged tuple (rebuilt on any change) ──
_bypass_domains: tuple = _env_domains


def _rebuild():
    """Rebuild the merged bypass tuple from env + settings sources."""
    global _bypass_domains
    seen = set()
    merged = []
    for d in _env_domains + _settings_domains:
        dl = d.lower().strip()
        if dl and dl not in seen:
            seen.add(dl)
            merged.append(dl)
    _bypass_domains = tuple(merged)


def _sync_no_proxy():
    """Rebuild ``no_proxy`` env var from: always-bypass + env baseline + bypass domains.

    Called automatically whenever bypass domains or proxy config change,
    ensuring the global ``no_proxy`` env var stays in sync with the
    unified bypass list.
    """
    parts = []
    seen = set()

    def _add(d):
        if d and d not in seen:
            parts.append(d)
            seen.add(d)

    # 1. Standard always-bypass entries
    for d in _ALWAYS_BYPASS:
        _add(d)
    # 2. Original env no_proxy baseline
    for d in _ENV_NO_PROXY.split(','):
        _add(d.strip())
    # 3. All bypass domains (env PROXY_BYPASS_DOMAINS + Settings UI)
    for d in _bypass_domains:
        _add(d)

    merged = ','.join(parts)
    _apply_to_env('no_proxy', merged)


def proxies_for(url: str) -> dict:
    """Return ``{'no_proxy': '*'}`` when *url* should bypass the HTTP proxy.

    Returns an empty dict otherwise, letting ``requests`` use the
    environment-level ``http_proxy`` / ``https_proxy`` as normal.

    This is the **single entry point** for proxy decisions — every module
    that makes HTTP requests should call this.
    """
    host = (urlparse(url).hostname or '').lower()
    if not host:
        return {}
    # Always bypass the standard local hosts. ``requests`` already does this
    # via its NO_PROXY env handling, but ``httpx`` doesn't honour NO_PROXY
    # when given an explicit ``proxy=`` URL — so we MUST return the bypass
    # marker for these hosts too, otherwise async callers send localhost
    # traffic through the corporate proxy.
    if host in _ALWAYS_BYPASS:
        return _NO_PROXY
    # Exact-match registered hosts (typically self-hosted local LLM endpoints
    # whose IPs live in publicly-routable space and therefore aren't matched
    # by RFC1918 detection).  We need both the literal host AND the global
    # bypass-domain suffix list to bypass the proxy.
    if host in _registered_hosts:
        return _NO_PROXY
    if _bypass_domains and host.endswith(_bypass_domains):
        return _NO_PROXY
    # ── Learned decision (direct vs proxy) from lib.netpath ──
    # Explicit rules above always win; below here the host is registered
    # for probing and a measured 'direct' pin bypasses the proxy.
    np = _np()
    if np is not None:
        try:
            np.note_url(url)
            if np.decide(host) == 'direct':
                return _NO_PROXY
        except Exception as e:
            # Hot path (every request). A persistent netpath failure here means
            # every request silently falls back to the proxy — an LLM dispatch
            # latency regression with no signal. Warn ONCE, not per request.
            global _np_decide_warned
            if not _np_decide_warned:
                _np_decide_warned = True
                logger.warning('[Proxy] netpath decide/note_url failing; requests '
                               'fall back to proxy without learned direct-pin (first: %s)', e)
    # ── Proxy pool: explicit per-request proxy, scoped + health-picked ──
    entries = _pool_candidates(host)
    if entries:
        pick, resolved = _pick_resolved(entries)
        if pick is not None:
            with _pool_lock:
                _pool_choice[host] = pick.get('id', '')
            return {'http': resolved, 'https': resolved}
    return {}


def async_proxy_for(url: str) -> 'str | None':
    """Proxy decision for httpx-style clients that take an explicit ``proxy=``.

    httpx does NOT honour the ``no_proxy`` environment variable once an
    explicit proxy URL is handed in, so the async path cannot defer to the
    environment the way ``requests`` does when :func:`proxies_for` returns
    ``{}`` — before this helper, async LLM calls to an internal gateway
    (e.g. aigc.sankuai.com, bypassed via env ``no_proxy`` for the sync path)
    silently hairpinned through the corporate proxy.  The async decision is
    made identical to the sync one BY CONSTRUCTION: bypass when
    ``proxies_for()`` says so (explicit rules / registered hosts / learned
    direct pins), or when the SAME predicate ``requests`` itself uses
    (``should_bypass_proxies`` over the live env ``no_proxy``, kept in sync
    by ``_sync_no_proxy``) matches — otherwise return the env proxy URL.

    Returns ``None`` for a direct connection.
    """
    pf = proxies_for(url)
    if pf:
        if pf.get('no_proxy'):
            return None
        # Explicit pool-member URL — honour it (an empty dict would mean
        # "fall back to env", but a populated dict is a real choice).
        pooled = pf.get('https') or pf.get('http')
        if pooled:
            return pooled
    try:
        from requests.utils import should_bypass_proxies
        if should_bypass_proxies(url, no_proxy=None):
            return None
    except Exception as e:
        # A broken bypass probe must not break the request — fall through to
        # the env proxy (the pre-fix behaviour), never raise here.
        logger.debug('[Proxy] env no_proxy check failed for %s: %s', url, e)
    return (os.environ.get('https_proxy')
            or os.environ.get('HTTPS_PROXY')
            or os.environ.get('http_proxy')
            or os.environ.get('HTTP_PROXY')
            or None)

# Hosts (typically raw IPs of self-hosted LLM endpoints) that must bypass the
# proxy. Populated at runtime by the dispatcher / probe paths so we don't have
# to hardcode private-but-publicly-routable corporate IP ranges in env vars.
_registered_hosts: set = set()


def register_no_proxy_host(host: str) -> bool:
    """Mark *host* as proxy-bypass for the lifetime of this process.

    Idempotent and thread-safe. Updates both the per-request ``proxies_for``
    decision AND the ``no_proxy`` env var (so any third-party library using
    ``requests`` without our wrapper still bypasses correctly).

    Returns True if the host was newly registered.
    """
    if not host:
        return False
    h = host.strip().lower()
    if not h:
        return False
    with _lock:
        if h in _registered_hosts:
            return False
        _registered_hosts.add(h)
        # Append to no_proxy env var if not already present (substring check
        # is cheap and false positives are harmless — they only over-bypass).
        cur = os.environ.get('no_proxy', '')
        if h not in cur.split(','):
            new = (cur + ',' + h) if cur else h
            _apply_to_env('no_proxy', new)
    logger.info('[Proxy] Registered no-proxy host: %s', h)
    return True


def register_no_proxy_url(url: str) -> bool:
    """Convenience wrapper: register the hostname extracted from *url*."""
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ''
    except Exception as e:
        logger.debug('[Proxy] Failed to parse URL %s: %s', url, e)
        return False
    return register_no_proxy_host(host)


def get_bypass_domains() -> list:
    """Return the current *settings-only* bypass domains (for the UI)."""
    return list(_settings_domains)


def set_bypass_domains(domains: list):
    """Hot-reload bypass domains from the Settings UI.

    Updates both the per-request ``proxies_for()`` bypass tuple **and**
    the ``no_proxy`` environment variable (auto-synced).

    Called by ``routes/common.py`` when the user saves settings, and
    once at startup when loading persisted config.

    Args:
        domains: List of domain suffixes (e.g. ``['.corp.net', '.internal.example.com']``).
    """
    global _settings_domains
    with _lock:
        _settings_domains = tuple(
            d.strip() for d in domains if d and d.strip()
        )
        _rebuild()
        _sync_no_proxy()
    if _settings_domains:
        logger.info('[Proxy] Bypass domains updated: %s (no_proxy synced)',
                    ', '.join(_settings_domains))
    else:
        logger.debug('[Proxy] Settings bypass domains cleared')
    _on_proxy_topology_changed()


# ── Initial merge + env sync ──
_rebuild()
_sync_no_proxy()
