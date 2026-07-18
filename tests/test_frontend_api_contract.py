"""Frontend↔backend API-contract regression guard.

Two invariants that keep the unified API client (``static/js/api.js``) and the
backend route surface from silently drifting apart. Complements
``test_frontend_api_isolation.py`` (which only forbids RAW ``fetch('/api/...')``
outside api.js) — that guard proves nobody bypasses the client; THIS guard
proves the client's own contract is sound in both directions.

Direction 1 — every ``/api/*`` path template in api.js resolves to a REAL
    backend route.
    Authoritative source = the LIVE Quart ``url_map`` (built by the conftest
    ``flask_app`` fixture via ``server.register_all(app)``), NOT a regex scan of
    ``@route`` decorators. The url_map is the single source of truth: it already
    contains entry-point plugin blueprints (e.g. ``/api/v1/trading``),
    ``register_task_routes`` factory poll/abort routes, and ``@websocket``
    endpoints — so this guard is INHERENTLY immune to the false positives a
    source-only scan produces (a source scan flagged ``/api/v1/trading`` as a
    dead call because trading was extracted to a plugin). No hardcoded
    allowlist to drift.
    Fixture-build failure is a HARD FAIL, never a skip: a guard the environment
    can silently skip is no guard, and the whole suite already depends on this
    fixture.

Direction 2 — every ``Api.<domain>.<method>()`` call site (in any JS file other
    than api.js) names a method that is actually DEFINED under that exact domain
    in api.js.
    The check is PAIRED on ``(domain, method)`` — NOT a loose "some domain
    defines this bare method name" test. ``Api.project.redo()`` must fail if the
    ``project`` domain lacks ``redo``, even when another domain happens to
    define a ``redo``. This catches "added a call site but forgot the api.js
    definition" — a live ``TypeError: undefined is not a function`` dead button.
    Pure-static (no app build). Currently 0 violations; it locks that in.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
JS_DIR = os.path.normpath(os.path.join(HERE, '..', 'static', 'js'))
API_JS = os.path.join(JS_DIR, 'api.js')


# ── Shared helpers ────────────────────────────────────────────────────
def _strip_comments(src: str) -> str:
    """Remove /* */ block and // line comments (not a full JS parser — good
    enough to stop a path/call mentioned in a comment from being counted)."""
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.DOTALL)
    src = re.sub(r'//[^\n]*', '', src)
    return src


def _norm_path(p: str) -> str:
    """Normalize a URL path for set comparison: collapse every path parameter
    — Flask ``<int:x>`` / ``<conv_id>`` and JS ``${...}`` template exprs — to a
    single ``<>`` placeholder, drop the querystring, and strip a trailing slash.
    """
    p = p.split('?')[0].split('#')[0]
    p = re.sub(r'\$\{[^}]*\}', '<>', p)   # JS template expression
    p = re.sub(r'<[^>]+>', '<>', p)       # Flask/Quart converter
    p = re.sub(r'<>(?:<>)+', '<>', p)     # coalesce adjacent placeholders
    return p.rstrip('/') or '/'


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


# ── api.js parsing ────────────────────────────────────────────────────
def _api_js_paths() -> set[str]:
    """Every ``/api/...`` string/template literal in api.js (comments stripped),
    normalized, EXCEPT plugin-prefix base constants.

    A literal assigned to a ``_*_BASE`` const (e.g. ``const _TRADING_BASE =
    '/api/v1/trading';``) is a PREFIX that the client only ever uses as
    ``_TRADING_BASE + path`` — the bare prefix is never hit as an endpoint, and
    the concrete routes under it live in an external entry-point plugin
    (``tofu.blueprints``) that mounts only when installed + enabled. Requiring
    such a prefix to match a live route would falsely flag it whenever the
    plugin isn't mounted (exactly the ``/api/v1/trading`` false positive a
    source-only scan produced). Excluding ``_*_BASE`` assignments is the
    principled, plugin-agnostic fix — no hardcoded route name. The endpoints
    that DO get concatenated onto the base still can't be verified statically
    (the plugin owns them), which is correct: core must not assert a plugin's
    internal route shape.
    """
    s = _strip_comments(_read(API_JS))
    # Literals bound to a _*_BASE const → plugin prefixes, excluded.
    base_literals = set(
        re.findall(r"""_[A-Z][A-Z0-9_]*BASE\s*=\s*[`'"](/api/[^`'"]*)[`'"]""", s)
    )
    out: set[str] = set()
    for m in re.finditer(r"""[`'"](/api/[^`'"]*)[`'"]""", s):
        raw = m.group(1)
        if raw in base_literals:
            continue
        out.add(_norm_path(raw))
    return out


def _match_first_brace_block(s: str, open_idx: int) -> str:
    """Return the substring of the { ... } block whose opening brace is at
    ``open_idx`` (balanced-brace scan, ignoring braces inside strings/templates
    is NOT attempted — api.js domain blocks don't embed literal unbalanced
    braces in strings, and the parser is only used to collect top-level keys)."""
    depth = 0
    for i in range(open_idx, len(s)):
        c = s[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return s[open_idx:i + 1]
    return s[open_idx:]


def _api_js_domain_methods() -> dict[str, set[str]]:
    """Parse api.js into {domain: {method, ...}}.

    Each domain is declared ``const <domain> = { ... }``. Within a domain block
    we collect top-level ``key:`` identifiers (method names). Nested object
    literals are skipped by only taking keys at brace-depth 1 of the block.
    """
    s = _strip_comments(_read(API_JS))
    domains: dict[str, set[str]] = {}
    for m in re.finditer(r'\bconst\s+([a-zA-Z_]\w*)\s*=\s*\{', s):
        name = m.group(1)
        block = _match_first_brace_block(s, m.end() - 1)
        methods: set[str] = set()
        depth = 0
        # Walk the block char by char; record ``ident:`` only at depth 1.
        i = 0
        while i < len(block):
            c = block[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif depth == 1:
                km = re.match(r'\s*([a-zA-Z_]\w*)\s*:', block[i:])
                if km:
                    methods.add(km.group(1))
                    i += km.end() - 1
            i += 1
        if methods:
            domains[name] = methods
    return domains


# ── call-site scan ────────────────────────────────────────────────────
def _api_call_sites() -> set[tuple[str, str, str]]:
    """All ``Api.<domain>.<method>(`` call sites across static/js EXCEPT api.js
    (and generated bundles). Returns {(domain, method, relpath)}."""
    call_re = re.compile(r'\bApi\.([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(')
    out: set[tuple[str, str, str]] = set()
    for root, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js'):
                continue
            if name == 'api.js' or name.startswith('bundle-') or name.startswith('feature-'):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            s = _strip_comments(_read(path))
            rel = os.path.relpath(path, JS_DIR).replace(os.sep, '/')
            for m in call_re.finditer(s):
                out.add((m.group(1), m.group(2), rel))
    return out


# ── Direction 1: FE paths ⊆ live backend routes ───────────────────────
def _live_route_paths(flask_app) -> set[str]:
    """Normalized set of every registered route path from the LIVE url_map."""
    routes: set[str] = set()
    for rule in flask_app.url_map.iter_rules():
        routes.add(_norm_path(str(rule.rule)))
    return routes


def test_api_js_paths_resolve_to_real_routes(flask_app):
    """Every /api/* template in api.js must match a real registered route.

    HARD requirement: this depends on the ``flask_app`` fixture (the live app).
    If the fixture cannot build, the test FAILS — it is never skipped. A guard
    the environment can silently disable is not a guard.
    """
    # Fixture-fail = hard fail: assert we actually got a usable app + url_map.
    assert flask_app is not None, 'flask_app fixture did not build the app'
    assert hasattr(flask_app, 'url_map'), 'flask_app has no url_map — cannot verify routes'

    live = _live_route_paths(flask_app)
    assert live, 'url_map yielded zero routes — app built empty, cannot verify'

    fe_paths = _api_js_paths()
    assert fe_paths, 'api.js yielded zero /api paths — parser broke'

    orphans = sorted(p for p in fe_paths if p not in live)
    if orphans:
        details = '\n'.join(f'  {p}' for p in orphans)
        pytest.fail(
            'api.js references /api paths with NO matching backend route '
            '(dead client calls — 404 at runtime). Either the route was '
            'removed/renamed or the api.js template is wrong:\n' + details
        )


# ── Direction 2: (domain, method) call sites ⊆ api.js definitions ──────
def test_api_call_sites_are_defined_paired():
    """Every Api.<domain>.<method>() call names a method DEFINED under that
    exact domain in api.js. Paired check — a bare method name defined under a
    DIFFERENT domain does not satisfy it."""
    domain_methods = _api_js_domain_methods()
    assert domain_methods, 'failed to parse any domain from api.js'
    # Sanity: the known domains from the Api = {...} assembly must be present.
    for expected in ('conversations', 'project', 'folders', 'chat'):
        assert expected in domain_methods, (
            f'api.js parser missed the {expected!r} domain — parser is broken, '
            'the guard would be a false green'
        )

    violations: dict[tuple[str, str], set[str]] = {}
    for domain, method, rel in _api_call_sites():
        defined = domain_methods.get(domain)
        # Unknown domain (not a const block) → skip: it may be a low-level verb
        # namespace or a false match; the paired guard targets defined domains.
        if defined is None:
            continue
        if method not in defined:
            violations.setdefault((domain, method), set()).add(rel)

    if violations:
        details = '\n'.join(
            f'  Api.{d}.{m}()  <- {sorted(files)}'
            for (d, m), files in sorted(violations.items())
        )
        pytest.fail(
            'Call sites invoke Api.<domain>.<method>() where the method is NOT '
            'defined under that domain in api.js (runtime TypeError — dead '
            'button). Add the method to the domain in api.js or fix the call:\n'
            + details
        )
