"""Request-correlation guard — the frontend→backend log join key.

Why this exists
---------------
The backend has always preferred an inbound ``X-Request-ID``
(``server.py::_assign_req_id_and_log``): it adopts the client's id, pushes it
into a contextvar so EVERY log line for that request carries ``[rid]``, and
echoes it back on the response. But the frontend never sent one, so in practice
every rid was server-minted and the client kept no copy. A user reporting a bug
gave us a screenshot and a rough timestamp; joining that to server logs meant
guessing by URL + clock. That is the mechanical reason bugs were hard to trace.

``static/js/api.js::request()`` is the ONE chokepoint every frontend backend
call passes through (``get``/``post``/``put``/``patch``/``del``/``stream`` all
delegate to it, and ``tests/test_frontend_api_isolation.py`` forbids raw
``fetch('/api/...')`` anywhere else). Injecting the header there gives total
coverage from a single edit — and this guard keeps it there.

What is asserted
----------------
1. ``request()`` sets ``X-Request-ID`` on the outbound headers, and does not
   clobber a caller-provided value.
2. The id is minted per request from a per-page prefix (so one page load's
   requests group with one grep) with a monotonic suffix.
3. Errors carry the id back to the user surface, on BOTH the HTTP-error and the
   no-response network path — the quiet ``onError:'null'`` path logs it too,
   since the quietest failure needs the join key most.
4. The backend still prefers the inbound header over minting its own.

The JS assertions are source-scans plus a real execution of ``api.js`` in node
with ``fetch`` intercepted, so they check what actually reaches the wire rather
than what the source appears to say.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
API_JS = os.path.join(ROOT, 'static', 'js', 'api.js')
SERVER_PY = os.path.join(ROOT, 'server.py')

# Executed in node: loads the REAL api.js, intercepts fetch, and reports the
# headers that actually reached the wire for GET / POST / stream.
_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const captured = [];
globalThis.window = globalThis;
globalThis.location = { protocol: 'http:', host: 'h', pathname: '/' };
globalThis.fetch = async (url, init) => {
  captured.push({ url, headers: Object.assign({}, init.headers) });
  return {
    ok: true, status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type'
      ? 'application/json' : null) },
    text: async () => '{"ok":true}',
  };
};
globalThis.AbortController = class { constructor(){ this.signal = {}; } abort(){} };
new Function('window', src)(globalThis);
const Api = globalThis.Api;
(async () => {
  await Api.get('/api/v1/folders');
  await Api.post('/api/v1/folders', { name: 'x' });
  await Api.stream('/api/chat/stream/t1');
  // Caller-supplied id must NOT be overwritten (retry reusing an id).
  await Api.get('/api/v1/folders', { headers: { 'X-Request-ID': 'caller-supplied' } });
  const out = {
    pageId: (typeof Api.pageRequestId === 'function') ? Api.pageRequestId() : null,
    rids: captured.map((c) => c.headers['X-Request-ID'] || null),
  };
  console.log('@@RESULT@@' + JSON.stringify(out));
})().catch((e) => { console.log('@@ERROR@@' + (e && e.message)); process.exit(3); });
"""


def _run_harness(api_js_path: str = API_JS) -> dict:
    with tempfile.TemporaryDirectory() as td:
        hp = os.path.join(td, 'h.js')
        with open(hp, 'w', encoding='utf-8') as fh:
            fh.write(_HARNESS)
        proc = subprocess.run(
            ['node', hp, api_js_path],
            capture_output=True, text=True, timeout=120, cwd=ROOT,
        )
    out = proc.stdout or ''
    if '@@RESULT@@' not in out:
        pytest.fail(
            'api.js harness produced no result.\n'
            f'stdout={out[-2000:]}\nstderr={(proc.stderr or "")[-2000:]}'
        )
    return json.loads(out.split('@@RESULT@@', 1)[1].splitlines()[0])


def _api_src() -> str:
    with open(API_JS, encoding='utf-8') as fh:
        return fh.read()


@pytest.mark.unit
def test_request_chokepoint_sets_request_id_header():
    """Every outbound call from api.js carries X-Request-ID."""
    r = _run_harness()
    rids = r['rids']
    assert len(rids) == 4, f'expected 4 captured requests, got {len(rids)}'
    for i, rid in enumerate(rids[:3]):
        assert rid, (
            f'request #{i} reached the wire with NO X-Request-ID — the backend '
            'will mint its own and the client keeps no join key. '
            'Inject it in api.js::request().'
        )


@pytest.mark.unit
def test_caller_supplied_request_id_is_not_clobbered():
    """An explicit id survives — a retry can deliberately reuse one."""
    r = _run_harness()
    assert r['rids'][3] == 'caller-supplied', (
        f'caller-provided X-Request-ID was overwritten with {r["rids"][3]!r}; '
        'request() must not clobber an explicit value.'
    )


@pytest.mark.unit
def test_request_ids_share_page_prefix_and_are_unique():
    """Ids group by page load (one grep) yet stay individually addressable.

    The shared prefix is what makes a whole page-load's traffic findable with a
    single `grep` in app.log; uniqueness is what makes a MISSING request
    visible as a gap in the sequence.
    """
    r = _run_harness()
    page = r['pageId']
    assert page, 'Api.pageRequestId() must expose the per-page correlation prefix'
    auto = r['rids'][:3]
    assert len(set(auto)) == 3, f'request ids must be unique per request, got {auto}'
    for rid in auto:
        assert rid.startswith(page + '-'), (
            f'rid {rid!r} does not carry the page prefix {page!r} — '
            'a page load must be greppable as one group.'
        )
    # Header-safe + short enough to read aloud / screenshot.
    for rid in auto:
        assert re.fullmatch(r'[a-z0-9]+-\d+', rid), (
            f'rid {rid!r} must stay [a-z0-9]+-<seq>: it lands in an HTTP '
            'header, server log lines, and a user-visible error surface.'
        )


@pytest.mark.unit
def test_errors_carry_the_request_id_to_the_user_surface():
    """Both failure paths attach the id, including the quiet swallow path."""
    src = _api_src()
    assert 'err.clientRequestId = _rid' in src, (
        'the HTTP-error ApiError must carry the id WE sent (clientRequestId)'
    )
    assert "resp.headers.get('X-Request-ID')" in src, (
        'read the server-echoed X-Request-ID back off the response — a '
        'mismatch with what we sent proves a proxy rewrote the header.'
    )
    assert '_netErr.requestId = _rid' in src, (
        'the no-response network path must also carry the id: it separates '
        '"never left the client" from "server logged it, then connection broke".'
    )
    assert 'rid=%s' in src, (
        "the onError:'null' swallow path must log the rid — the quietest "
        'failure mode is the one most in need of a server-side join key.'
    )


@pytest.mark.unit
def test_backend_prefers_inbound_request_id():
    """server.py must adopt the client's id rather than always minting."""
    with open(SERVER_PY, encoding='utf-8') as fh:
        src = fh.read()
    m = re.search(
        r"rid\s*=\s*request\.headers\.get\(\s*['\"]X-Request-ID['\"]\s*\)\s*or\s+",
        src,
    )
    assert m, (
        'server.py must read an inbound X-Request-ID and only fall back to a '
        'minted id — otherwise the client-side header is ignored and the '
        'frontend/backend logs cannot be joined.'
    )
    assert re.search(r"response\.headers\[['\"]X-Request-ID['\"]\]\s*=\s*rid", src), (
        'server.py must echo the resolved rid back on the response so the '
        'client can record the id the server actually used.'
    )
