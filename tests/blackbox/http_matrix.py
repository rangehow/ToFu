"""Differential HTTP matrix: one authored, side-effect-free request script.

Replays the same request script against a reference server (the Python
implementation, ``TOFU_BLACKBOX_REFERENCE_URL``) and a target server (the URL
the black-box harness is pointed at, e.g. a future Rust front door) and
compares each answer, so a port cannot silently change wire behaviour. Both
servers are assumed to run the same build and configuration: the reference is
the oracle for bytes, not an independent instalment.

Wire surface compared per step
------------------------------
* HTTP status.
* Every response header except ``EXCLUDED_RESPONSE_HEADERS`` below. That
  includes ``content-type`` and ``cache-control`` explicitly, plus
  ``x-request-id``: each step pins ``X-Request-ID: http-matrix-<step>`` and both
  servers must echo it. ``content-length`` stays compared because it is derived
  from the exact body.
* The response body bytes after the step's declared body projection. Every
  step declares one of ``exact`` (raw bytes), ``v4-envelope`` or
  ``push-session-shell``; the last two canonicalise only the contract-declared
  per-response members described below.

Excluded response headers (each verified against repeated responses from a live
Python server on this checkout, 2026-11-19; not assumed):
* ``date`` -- the response wall clock; differs on every request.
* ``server`` -- transport process identity (``hypercorn-h11``); a Rust front
  door legitimately names itself instead.
* ``connection``, ``keep-alive``, ``transfer-encoding`` -- hop-by-hop framing
  owned by one connection, not by the resource.
* ``set-cookie`` -- ``/`` and ``/admin`` mint a fresh ``tofu_push_session``
  token on every response.
* ``expires`` -- computed from ``cache-control`` max-age and the wall clock.
* ``last-modified`` and ``etag`` -- derived from the serving checkout's file
  mtime and size (or, for v4 successes, from the pre-envelope handler bytes).
  Identical bytes served from a different install cannot reproduce them, and
  no route this matrix exercises pins them as a contract; the required cache
  policy is asserted through ``cache-control``.

Body projections (the only two, both contract-declared per-response values)
---------------------------------------------------------------------------
* ``v4-envelope``: every v4 success carries ``meta.serverTimeMs``, the server
  clock in epoch milliseconds (``contracts/api_v4.yaml``, ``Meta``), so no two
  answers can be byte-identical. The projection parses the JSON, requires
  ``meta.serverTimeMs`` to be a non-negative integer, then replaces only the
  exact envelope tail ``,"meta":{"requestId":"<pinned id>","serverTimeMs":<n>}}``
  with ``0`` and compares every remaining byte. A payload member that happens
  to be named ``serverTimeMs`` is therefore never normalised. Non-JSON refusals
  (RFC 7807 problem bodies) are compared verbatim.
* ``push-session-shell``: the server-rendered index/admin shell embeds a
  per-browser-session ``"pushOriginProof":"<64 hex>"`` boot token. The
  projection requires exactly one such member, replaces the value with a fixed
  token and compares every remaining byte. A non-HTML answer (the 401 refusal
  of a default-deny server) is compared verbatim.

Side-effect freedom
-------------------
The script contains only reads plus refusals that run before any handler:
``POST /api/v4/auth/mode`` is a 405, the cross-site ``POST`` to the scoped
``/api/v4/messages/cost`` route is refused by the credential boundary, and a
cross-site ``POST`` to the retired ``/api/v1`` prefix is the 426 upgrade
refusal before authentication or body parsing. No request mutates state.

Auth gating
-----------
Public reads and refusals run in every mode. Steps that depend on the
credential-free open-mode grant are marked ``credential_free``; the test skips
that group when the two servers do not both grant it. A different reported
auth mode is itself a wire difference and fails in the public group.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from tests.blackbox.test_blackbox_http import CROSS_SITE, Reply, request

REFERENCE_URL_ENV = 'TOFU_BLACKBOX_REFERENCE_URL'
MANIFEST_PATH = '/static/vite/manifest.json'
REQUEST_ID_PREFIX = 'http-matrix-'

EXACT = 'exact'
V4_ENVELOPE = 'v4-envelope'
PUSH_SESSION_SHELL = 'push-session-shell'

#: Response headers deliberately not compared; every entry states why. Keep
#: this list minimal and empirical (see the module docstring).
EXCLUDED_RESPONSE_HEADERS = {
    'date': 'response wall clock',
    'server': 'transport process identity',
    'connection': 'hop-by-hop framing',
    'keep-alive': 'hop-by-hop framing',
    'transfer-encoding': 'hop-by-hop framing',
    'set-cookie': 'per-request push-session token',
    'expires': 'wall-clock expiry computed from cache-control max-age',
    'last-modified': 'serving checkout file mtime',
    'etag': 'representation validator derived from file mtime/size or pre-envelope bytes',
}

PUSH_ORIGIN_PROOF_MEMBER = re.compile(rb'"pushOriginProof":"([0-9a-f]{64})"')


@dataclass(frozen=True)
class Step:
    """One authored request. ``path`` is appended to each server's base URL."""

    name: str
    method: str
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    projection: str = EXACT
    credential_free: bool = False

    @property
    def request_id(self) -> str:
        return REQUEST_ID_PREFIX + self.name


@dataclass(frozen=True)
class MatrixRun:
    """Both servers' replies for the same step list."""

    reference: str
    target: str
    steps: tuple[Step, ...]
    expected: dict[str, Reply]
    actual: dict[str, Reply]


# ── The authored script ─────────────────────────────────────────────
#
# Public reads and refusals. `/api/v4/auth/mode` reports the configured mode;
# the index declares `public=True` for the capability/pricing/system-prompt/user
# reads and the login/dashboard pages.

PUBLIC_STEPS = (
    Step('favicon-ico', 'GET', '/favicon.ico'),
    Step('favicon-svg', 'GET', '/favicon.svg'),
    Step('login-page', 'GET', '/login'),
    Step('login-slash', 'GET', '/login/'),
    Step('signup-page', 'GET', '/signup'),
    Step('signup-slash', 'GET', '/signup/'),
    Step('dashboard-page', 'GET', '/dashboard'),
    Step('dashboard-slash', 'GET', '/dashboard/'),
    Step('vite-manifest', 'GET', MANIFEST_PATH),
    Step('v4-auth-mode', 'GET', '/api/v4/auth/mode', projection=V4_ENVELOPE),
    Step('v4-capabilities', 'GET', '/api/v4/capabilities', projection=V4_ENVELOPE),
    Step('v4-billing-pricing', 'GET', '/api/v4/billing/pricing', projection=V4_ENVELOPE),
    Step('v4-system-prompt-blocks', 'GET', '/api/v4/system-prompt/blocks',
         projection=V4_ENVELOPE),
    Step('v4-system-prompt-default', 'GET', '/api/v4/system-prompt/default',
         projection=V4_ENVELOPE),
    Step('v4-users-me', 'GET', '/api/v4/users/me', projection=V4_ENVELOPE),
    Step('legacy-v1-upgrade', 'GET', '/api/v1/conversations'),
    Step('legacy-v3-upgrade', 'GET', '/api/v3/conversations'),
    Step('unknown-v2-prefix', 'GET', '/api/v2/conversations'),
    Step('unknown-page', 'GET', '/blackbox-harness-no-such-page'),
    Step('method-not-allowed-v4', 'POST', '/api/v4/auth/mode'),
    Step('cross-site-write-refused', 'POST', '/api/v4/messages/cost',
         headers=(('Content-Type', 'application/json'), *CROSS_SITE.items()), body=b'{}'),
    Step('cross-site-legacy-write-refused', 'POST', '/api/v1/conversations',
         headers=(('Content-Type', 'application/json'), *CROSS_SITE.items()),
         body=b'{not json'),
)

# Reads whose answer depends on the open-mode loopback grant.
CREDENTIAL_FREE_STEPS = (
    Step('index-shell', 'GET', '/', projection=PUSH_SESSION_SHELL, credential_free=True),
    Step('admin-shell', 'GET', '/admin', projection=PUSH_SESSION_SHELL, credential_free=True),
    Step('admin-slash', 'GET', '/admin/', projection=PUSH_SESSION_SHELL, credential_free=True),
    Step('v4-meta', 'GET', '/api/v4/meta', projection=V4_ENVELOPE, credential_free=True),
    Step('head-v4-meta', 'HEAD', '/api/v4/meta', projection=V4_ENVELOPE,
         credential_free=True),
    Step('unknown-v4-resource', 'GET', '/api/v4/blackbox-harness/no-such-resource',
         credential_free=True),
)


def _vite_entry_assets(manifest: dict) -> list[str]:
    """Entry scripts plus their manifest import/CSS closure, in discovery order.

    Fonts, images and other files the manifest lists stay covered by the
    (byte-compared) manifest itself; generated names, not content, identify
    them.
    """
    pending = [key for key, chunk in manifest.items() if isinstance(chunk, dict)
               and chunk.get('isEntry')]
    seen: set[str] = set()
    assets: list[str] = []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        chunk = manifest.get(key)
        if not isinstance(chunk, dict):
            continue
        candidates = [chunk.get('file'), *(chunk.get('css') or ())]
        assets.extend(path for path in candidates if path and path not in assets)
        pending.extend(chunk.get('imports') or ())
    return assets


def asset_steps(reference_base: str) -> list[Step]:
    """Name the hashed assets from the reference's own manifest.

    The manifest bytes are compared first, so a build mismatch fails there;
    naming assets from the reference keeps the script independent of checkout
    timestamps and Vite hashes.
    """
    reply = request(reference_base, MANIFEST_PATH)
    if reply.status != 200:
        return []
    try:
        manifest = reply.json()
    except ValueError:
        return []
    if not isinstance(manifest, dict):
        return []
    return [Step(f'vite-asset-{index:02d}', 'GET', f'/static/vite/{path}')
            for index, path in enumerate(_vite_entry_assets(manifest))]


def build_steps(reference_base: str) -> list[Step]:
    """The whole script: authored steps, with assets after their manifest."""
    steps: list[Step] = []
    for step in PUBLIC_STEPS:
        steps.append(step)
        if step.name == 'vite-manifest':
            steps.extend(asset_steps(reference_base))
    steps.extend(CREDENTIAL_FREE_STEPS)
    return steps


def record(base: str, steps: list[Step] | tuple[Step, ...]) -> dict[str, Reply]:
    """Replay every step against one server; reads only."""
    replies: dict[str, Reply] = {}
    for step in steps:
        headers = dict(step.headers)
        headers['X-Request-ID'] = step.request_id
        replies[step.name] = request(
            base, step.path, method=step.method, headers=headers, body=step.body)
    return replies


def play_matrix(reference: str, target: str) -> MatrixRun:
    steps = tuple(build_steps(reference))
    return MatrixRun(reference, target, steps, record(reference, steps), record(target, steps))


# ── Body projections ────────────────────────────────────────────────


class ProjectionError(ValueError):
    """A reply does not have the shape its step's projection declares."""


def _without_server_time(step: Step, reply: Reply) -> bytes:
    if not reply.body:
        return reply.body
    media_type = reply.headers.get('content-type', '').split(';', 1)[0].strip()
    if media_type != 'application/json':
        return reply.body
    try:
        document = json.loads(reply.body)
    except ValueError as error:
        raise ProjectionError(f'not JSON: {error}') from error
    meta = document.get('meta') if isinstance(document, dict) else None
    if not isinstance(meta, dict) or 'serverTimeMs' not in meta:
        raise ProjectionError('a v4 success envelope has no meta.serverTimeMs')
    value = meta['serverTimeMs']
    if type(value) is not int or value < 0:
        raise ProjectionError(f'meta.serverTimeMs is not a non-negative integer: {value!r}')
    request_id = step.request_id.encode('ascii')
    envelope_tail = re.compile(
        rb',"meta":\{"requestId":"' + re.escape(request_id)
        + rb'","serverTimeMs":\d+\}\}$')
    if envelope_tail.search(reply.body) is None:
        raise ProjectionError(
            f'no v4 envelope tail carrying requestId {step.request_id!r}')
    return envelope_tail.sub(
        b',"meta":{"requestId":"' + request_id + b'","serverTimeMs":0}}', reply.body)


def _without_push_proof(reply: Reply) -> bytes:
    if b'"pushOriginProof"' not in reply.body:
        return reply.body
    matches = PUSH_ORIGIN_PROOF_MEMBER.findall(reply.body)
    if len(matches) != 1:
        raise ProjectionError(
            f'expected one 64-hex "pushOriginProof" member, found {len(matches)}')
    return PUSH_ORIGIN_PROOF_MEMBER.sub(b'"pushOriginProof":"<per-request>"', reply.body)


def project_body(step: Step, reply: Reply) -> bytes:
    """Return the bytes this step compares after its declared projection."""
    if step.projection == EXACT:
        return reply.body
    if step.projection == V4_ENVELOPE:
        return _without_server_time(step, reply)
    if step.projection == PUSH_SESSION_SHELL:
        return _without_push_proof(reply)
    raise ProjectionError(f'unknown projection {step.projection!r}')


# ── Comparison and failure text ─────────────────────────────────────


def first_difference(expected: bytes, actual: bytes) -> str:
    """First differing offset with 80 bytes of context on each side."""
    offset = next((index for index, (left, right) in enumerate(zip(expected, actual))
                   if left != right), min(len(expected), len(actual)))
    window = slice(max(0, offset - 80), offset + 80)
    return f'offset {offset}: expected {expected[window]!r} actual {actual[window]!r}'


def compare_step(step: Step, expected: Reply, actual: Reply) -> list[str]:
    """Every mismatch for one step, each line naming the step and route."""
    label = f'step {step.name!r} ({step.method} {step.path})'
    problems: list[str] = []
    if expected.status != actual.status:
        problems.append(f'{label}: status {expected.status} != {actual.status}')
    expected_headers = {name: value for name, value in expected.headers.items()
                        if name not in EXCLUDED_RESPONSE_HEADERS}
    actual_headers = {name: value for name, value in actual.headers.items()
                      if name not in EXCLUDED_RESPONSE_HEADERS}
    for name in sorted(set(expected_headers) | set(actual_headers)):
        want = expected_headers.get(name)
        got = actual_headers.get(name)
        if want != got:
            problems.append(f'{label}: header {name!r} expected {want!r} actual {got!r}')
    try:
        want_body = project_body(step, expected)
        got_body = project_body(step, actual)
    except ProjectionError as error:
        problems.append(f'{label}: body projection failed: {error}')
        return problems
    if want_body != got_body:
        problems.append(
            f'{label}: body differs at {first_difference(want_body, got_body)} '
            f'(lengths {len(want_body)} != {len(got_body)})')
    return problems


def compare_steps(run: MatrixRun, steps: list[Step] | tuple[Step, ...]) -> list[str]:
    """All mismatch lines for ``steps``, in script order."""
    return [problem for step in steps
            for problem in compare_step(step, run.expected[step.name], run.actual[step.name])]
