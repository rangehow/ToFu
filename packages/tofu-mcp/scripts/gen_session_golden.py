"""Record the Python bridge's outcomes on a recorded MCP session as the Rust golden file.

Usage (repository root): ``.venv/bin/python3 packages/tofu-mcp/scripts/gen_session_golden.py``.
Connects ``lib.mcp.client.MCPBridge`` to tests/fixtures/mcp_sessions/replay_server.py
with hope_like.json and writes every call's relay-encoded result (or failure
category) to packages/tofu-mcp/tests/fixtures/hope_like.golden.json, which
``tests/recorded_session.rs`` compares with the Rust client byte for byte.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SESSIONS = ROOT / 'tests' / 'fixtures' / 'mcp_sessions'
OUT = ROOT / 'packages' / 'tofu-mcp' / 'tests' / 'fixtures' / 'hope_like.golden.json'
M17 = 'root.hldy_training_cluster.your-user.h910c_m17_public'

# (tool, arguments, meta); a missing policy file enforces the contract default.
CALLS = [
    ['get_job', {'hope_run_id': 'x', 'limit': '3'}, None],
    ['structured', {}, None],
    ['duplicate_json', {}, None],
    ['media', {}, None],
    ['tool_error', {}, None],
    ['peer_error', {}, None],
    ['catalog_write', {'operation': 'noop'}, None],
    ['sheet_get_range_data', {}, None],
    ['submit_job', {'workdir': '/nonexistent', 'queue': M17}, {'tofu/project': '/p'}],
    ['catalog_write', {'operation': 'codelab_create', 'body': {'queue': M17}}, None],
    ['confirm_rerun', {'hope_conf': ''}, None],
    ['crash', {}, None],
]


def main() -> int:
    from lib.mcp.bridge_relay import _encode_result, _error_kind
    import lib.mcp.client._bridge as bridge_module
    import lib.tools.hope_queue_policy_launcher as launcher

    scratch = Path(tempfile.mkdtemp())
    os.environ['TOFU_MCP_CACHE_DIR'] = str(scratch / 'cache')
    launcher.policy_path = lambda owner: '/nonexistent/HOPE_QUEUE_POLICY.json'
    shim = scratch / 'hope-mcp'
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {SESSIONS / "replay_server.py"} '
                    f'{SESSIONS / "hope_like.json"} "" {scratch / "state"}\n', encoding='utf-8')
    shim.chmod(0o755)
    bridge = bridge_module.MCPBridge(owner_user_id=1)
    bridge._stored_server_config = lambda name, refresh_browser=False: (None, None)
    bridge.connect_server('hope', {'command': str(shim)})
    outcomes = []
    try:
        for tool, arguments, meta in CALLS:
            try:
                result = bridge.call_tool(f'mcp__hope__{tool}', arguments, meta=meta)
                outcome = {'result': json.loads(json.dumps(_encode_result(result)))}
            except Exception as exc:  # noqa: BLE001 - the category is the record
                outcome = {'error': _error_kind(exc)}
            outcomes.append({'tool': tool, 'arguments': arguments, 'meta': meta, **outcome})
        rows = [[name, json.loads(json.dumps(info)), disabled] for name, info, disabled in bridge.tool_index_rows()]
    finally:
        bridge.disconnect_all()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'calls': outcomes, 'tool_index_rows': rows}, ensure_ascii=False, indent=1) + '\n',
                   encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
