"""tests/test_mcp_remote_transport.py — remote MCP transport + credential safety.

Two behaviours are pinned here, both asserted on RESULTS rather than on
implementation details (charter: "测试守卫必须断言「结果」而非「实现」"):

1. **A remote MCP server that requires an auth header can actually be
   reached.** Verified against a REAL streamable-http MCP server started on
   localhost which rejects any request without the right bearer token. The
   assertion is "the tool call returned the right answer", so the guard keeps
   biting if the transport is reimplemented.

2. **No credential ever leaves the process through the config surface.** The
   assertion is "the secret string does not appear anywhere in the serialized
   response", not "field X was popped" — so it also catches a *new*
   secret-bearing field being added later.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_mcp_remote_transport.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

SECRET = 'sk-tofu-rollinggo-SUPERSECRET-9f3a'


# ── A real authenticated streamable-http MCP server ──────────────────

_SERVER_SRC = textwrap.dedent('''
    import sys
    from mcp.server.fastmcp import FastMCP
    from starlette.responses import JSONResponse

    PORT = int(sys.argv[1])
    SECRET = sys.argv[2]

    mcp = FastMCP('authcheck', host='127.0.0.1', port=PORT)

    @mcp.tool()
    def whoami() -> str:
        """Return a fixed string proving the authenticated call went through."""
        return 'authenticated-ok'

    app = mcp.streamable_http_app()

    class RequireBearer:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                await self.inner(scope, receive, send)
                return
            hdrs = {k.decode().lower(): v.decode()
                    for k, v in scope.get('headers') or []}
            if hdrs.get('authorization') != f'Bearer {SECRET}':
                resp = JSONResponse({'error': 'unauthorized'}, status_code=401)
                await resp(scope, receive, send)
                return
            await self.inner(scope, receive, send)

    import uvicorn
    uvicorn.run(RequireBearer(app), host='127.0.0.1', port=PORT,
                log_level='error')
''')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def auth_server(tmp_path_factory):
    """Start the bearer-protected MCP server; yield its /mcp URL."""
    port = _free_port()
    script = tmp_path_factory.mktemp('mcpsrv') / 'server.py'
    script.write_text(_SERVER_SRC)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(port), SECRET],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    url = f'http://127.0.0.1:{port}/mcp'
    for _ in range(100):
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.skip(f'MCP test server died: {err.decode()[:400]}')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip('MCP test server did not come up')
    yield url
    proc.kill()
    proc.wait(timeout=10)


# ── 1. Reachability of an authenticated remote MCP server ────────────

def _remote_cfg(url: str) -> dict:
    return {
        'transport': 'streamable-http',
        'url': url,
        'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}'},
        'env': {'ROLLINGGO_API_KEY': SECRET},
        'enabled': True,
    }


def test_authenticated_remote_server_is_reachable(auth_server):
    """The whole point: a Bearer-gated remote MCP server yields its tools.

    Without header pass-through the server answers 401 and this fails — which
    is exactly the NEUTER for the credential-plumbing half of the change.
    """
    from lib.mcp.client import MCPBridge

    bridge = MCPBridge()
    try:
        tools = bridge.connect_server('authcheck', _remote_cfg(auth_server))
        assert [t.name for t in tools] == ['whoami'], \
            f'expected the remote tool to be discovered, got {tools}'

        result = bridge.call_tool('mcp__authcheck__whoami', {})
        assert 'authenticated-ok' in str(result), \
            f'authenticated tool call did not return its answer: {result!r}'
    finally:
        bridge.disconnect_all()


def test_unresolved_header_credential_names_the_missing_key():
    """A blank credential fails with the key name, not an opaque upstream 401."""
    from lib.mcp.transport import resolve_headers

    cfg = {'transport': 'streamable-http', 'url': 'http://x/mcp',
           'headers': {'Authorization': 'Bearer ${NOPE_MISSING_KEY}'}, 'env': {}}
    with pytest.raises(ValueError) as ei:
        resolve_headers(cfg, server_name='rollinggo')
    assert 'NOPE_MISSING_KEY' in str(ei.value)


def test_env_supplies_header_value_not_a_second_secret_store():
    """Credentials resolve from the SAME env block the UI already redacts."""
    from lib.mcp.transport import resolve_headers

    cfg = {'headers': {'Authorization': 'Bearer ${K}', 'X-Trace': 'plain'},
           'env': {'K': SECRET}}
    assert resolve_headers(cfg) == {
        'Authorization': f'Bearer {SECRET}', 'X-Trace': 'plain'}


# ── 2. Credentials never leave through the config surface ────────────

def test_redacted_config_never_carries_a_secret():
    """Result-level assertion: the secret is absent from the whole payload.

    Deliberately not "assert 'headers' not in out" — a future field holding a
    token would slip past that, but not past a substring scan of the JSON.
    """
    from lib.mcp.transport import redact_config

    cfg = _remote_cfg('http://127.0.0.1:1/mcp')
    out = redact_config(cfg)
    assert SECRET not in json.dumps(out, ensure_ascii=False), \
        f'credential leaked through redact_config: {out}'
    # The *shape* stays visible so the UI can show that the server authenticates.
    assert 'Authorization' in out.get('headers', {})


def test_redaction_masks_a_hand_inlined_literal_token():
    """A hand-edited mcp_servers.json that inlines the token is masked too."""
    from lib.mcp.transport import redact_config

    cfg = {'transport': 'streamable-http', 'url': 'http://x/mcp',
           'headers': {'Authorization': f'Bearer {SECRET}'}}
    assert SECRET not in json.dumps(redact_config(cfg))


def test_list_servers_response_contains_no_credential(monkeypatch, tmp_path):
    """End-to-end on the real route: GET /api/v1/mcp/servers leaks nothing.

    The stored config deliberately carries the secret in BOTH shapes:
      - ``env`` (the normal, tool-written shape), and
      - a LITERAL token inlined in ``headers`` (what a hand-edited
        mcp_servers.json looks like).
    The literal is the one that matters here: a template-only config would
    pass this scan even with no redaction at all, so a guard built purely on
    ``${VAR}`` fixtures cannot tell "redaction works" from "there was nothing
    to redact". With the literal present, returning the raw config turns this
    red — which is what makes it a real NEUTER target.
    """
    import lib.mcp.config as mcfg

    stored = _remote_cfg('http://127.0.0.1:1/mcp')
    # Hand-edited shape: token pasted straight into the header block.
    stored['headers'] = {'Authorization': f'Bearer {SECRET}',
                         'X-Api-Key': SECRET}

    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir()
    (cfg_dir / 'mcp_servers.json').write_text(
        json.dumps({'rollinggo': stored}), encoding='utf-8')
    monkeypatch.setattr(mcfg, '_CONFIG_DIR', str(cfg_dir))

    import routes.api_v1.mcp as mcp_routes

    class _FakeBridge:
        def list_servers(self):
            return []

        def get_breaker_state(self, name):
            return None

        def get_cred_health(self, name):
            return None

    captured = {}

    def _fake_ok(payload):
        captured['payload'] = payload
        return payload

    monkeypatch.setattr(mcp_routes, 'api_ok', _fake_ok)
    monkeypatch.setattr('lib.mcp.get_bridge', lambda: _FakeBridge())

    mcp_routes.list_servers_v1.__wrapped__() if hasattr(
        mcp_routes.list_servers_v1, '__wrapped__') else None
    # Call the undecorated body directly (auth decorator needs a request ctx).
    fn = mcp_routes.list_servers_v1
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    fn()

    blob = json.dumps(captured['payload'], ensure_ascii=False, default=str)
    assert SECRET not in blob, f'GET /servers leaked the credential: {blob}'
    assert 'rollinggo' in blob, 'server should still be listed'


# ── 3. Transport classification is explicit, not "not sse" ───────────

@pytest.mark.parametrize('transport,expect_stdio', [
    ('stdio', True),
    (None, True),
    ('sse', False),
    ('streamable-http', False),
    ('http', False),            # Claude CLI spelling
    ('streamable_http', False),  # Codex/docs spelling
])
def test_stdio_classification(transport, expect_stdio):
    """`stdio_command` must be empty for EVERY remote transport.

    The old `transport != 'sse'` idiom returned True for streamable-http,
    which would have sent a remote server down the subprocess-launcher path.
    """
    from lib.mcp.transport import is_stdio, stdio_command

    cfg = {'command': 'npx', 'url': 'http://x/mcp'}
    if transport is not None:
        cfg['transport'] = transport
    assert is_stdio(cfg) is expect_stdio
    assert bool(stdio_command(cfg)) is expect_stdio


def test_catalog_build_never_gives_a_remote_server_a_launcher(monkeypatch):
    """A remote catalog entry must build a URL config, not a subprocess one.

    `build_server_config` classified with `transport == 'sse'`, so a
    streamable-http entry fell into the stdio branch and would have been
    launched as a (nonexistent) local command. Asserted on the built config
    because that is what the bridge actually consumes.
    """
    import lib.mcp.registry as reg

    entry = {
        'id': 'rollinggo-hotel', 'name': 'RollingGo Hotel',
        'description': 'hotels', 'category': reg.CAT_OTHER,
        'transport': 'streamable-http',
        'endpoint': 'https://mcp.rollinggo.cn/mcp',
        'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}'},
        'command': '', 'args': [],
        'env_specs': [{'key': 'ROLLINGGO_API_KEY', 'label': 'API Key',
                       'required': True, 'secret': True}],
    }
    monkeypatch.setattr(reg, 'get_catalog_entry', lambda sid: entry)

    cfg = reg.build_server_config('rollinggo-hotel',
                                  {'ROLLINGGO_API_KEY': SECRET})
    assert cfg['url'] == 'https://mcp.rollinggo.cn/mcp'
    assert 'command' not in cfg, \
        f'remote server was given a subprocess launcher: {cfg}'
    # The credential landed in env (the redacted store), and the header block
    # only references it.
    assert cfg['env']['ROLLINGGO_API_KEY'] == SECRET
    assert cfg['headers']['Authorization'] == 'Bearer ${ROLLINGGO_API_KEY}'

    from lib.mcp.transport import resolve_headers
    assert resolve_headers(cfg) == {'Authorization': f'Bearer {SECRET}'}
