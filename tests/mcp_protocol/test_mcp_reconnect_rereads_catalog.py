"""Every MCP reconnect re-reads the server's tool definitions.

Regression (2026-11-17): xuecheng-mcp was restarted on new code several times
while Worker turns were running; its ``read_doc`` had dropped ``max_rows``.
Calls kept carrying the retired default and the server rejected them as an
unadvertised property. A reconnect is not announced by ``tools/list_changed``,
so the definitions must be re-read by the reconnect itself and reach both the
bridge (model-visible definitions, tool index) and every running task's
snapshot. These cases cover the reconnect paths other than an idle wake,
which ``test_mcp_live_schema_refresh`` covers: a crashed server recovered by
the next call, the keepalive/recovery ``_reconnect_server``, and an explicit
``connect_server`` of an already connected server.
"""

from __future__ import annotations

import sys

import pytest

from tests.mcp_protocol.test_mcp_live_schema_refresh import READ_DOC, _SERVER, _parameters


pytestmark = pytest.mark.unit


@pytest.fixture
def old_server(tmp_path, monkeypatch):
    """Connect to the old server and snapshot a task, then upgrade the code."""
    import lib.mcp
    import lib.mcp.client._bridge as bridge_module
    from lib.tools.contracts import compile_execution_contract_documents

    script = tmp_path / 'fake_docs_mcp.py'
    script.write_text(_SERVER, encoding='utf-8')
    version = tmp_path / 'version'
    version.write_text('old', encoding='utf-8')
    config = {'transport': 'stdio', 'command': sys.executable,
              'args': [str(script), str(version)], 'enabled': True}
    bridge = bridge_module.MCPBridge(owner_user_id=7)
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: bridge)
    # The bridge prefers the owner's stored row; there is none in this test.
    monkeypatch.setattr(bridge, '_stored_server_config',
                        lambda name, refresh_browser=False: (None, None))
    try:
        bridge.connect_server('docs', config)
        catalog = bridge.get_openai_tool_defs()
        task = {
            '_userId': 7,
            '_executable_tool_catalog': catalog,
            '_toolContractDocumentsByName': compile_execution_contract_documents(
                catalog, namespace='docs'),
        }
        assert 'max_rows' in _parameters(task, READ_DOC)['properties']
        version.write_text('new', encoding='utf-8')
        yield bridge, task, config
    finally:
        bridge.disconnect_all()


def _live_properties(bridge):
    info = bridge.get_tool_info(READ_DOC)
    return set(info['openai_def']['function']['parameters']['properties'])


def _model_visible_properties(bridge):
    for tool in bridge.get_openai_tool_defs():
        if tool['function']['name'] == READ_DOC:
            return set(tool['function']['parameters']['properties'])
    raise AssertionError('read_doc missing from the model-visible definitions')


def _assert_live_everywhere(bridge, task):
    from lib.tasks_pkg.handlers.tool_gateway import _executable_catalog
    from lib.tools.gateway import normalize_gateway_call

    assert _live_properties(bridge) == {'doc'}
    assert _model_visible_properties(bridge) == {'doc'}
    catalog = _executable_catalog(task)
    assert set(_parameters(task, READ_DOC)['properties']) == {'doc'}
    assert task['_toolContractDocumentsByName'][READ_DOC]['arguments_schema'][
        'properties'].keys() == {'doc'}
    call, error = normalize_gateway_call(
        {'name': READ_DOC, 'arguments': {'doc': '7'}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='gw', index=0,
        source='execute_calls',
        contract_documents_by_name=task['_toolContractDocumentsByName'])
    assert error is None
    assert call['_normalized_arguments'] == {'doc': '7'}
    assert bridge.call_tool(READ_DOC, call['_normalized_arguments']) == 'doc 7 @new'


def test_recovery_reconnect_rereads_tool_definitions(old_server):
    bridge, task, _config = old_server
    handle = bridge._reconnect_server('docs')
    assert handle.session is not None
    _assert_live_everywhere(bridge, task)


def test_crashed_server_recovered_by_the_next_call_rereads_tool_definitions(old_server):
    import os
    import signal
    import time

    bridge, task, _config = old_server
    pid = bridge._servers['docs']._process_pid if hasattr(
        bridge._servers['docs'], '_process_pid') else None
    if pid is None:
        import psutil
        me = psutil.Process()
        script_children = [p for p in me.children(recursive=True)
                           if any('fake_docs_mcp.py' in part for part in p.cmdline())]
        assert script_children, 'fake server subprocess not found'
        pid = script_children[0].pid
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.05)

    # ping is not a declared read, so the call that meets the dead transport
    # is not replayed; the bridge still reconnects so the next call is
    # validated against the new process's definitions.
    with pytest.raises(Exception):
        bridge.call_tool('mcp__docs__ping', {})
    _assert_live_everywhere(bridge, task)


def test_explicit_connect_of_a_connected_server_rereads_tool_definitions(old_server):
    bridge, task, config = old_server
    bridge.connect_server('docs', config)
    _assert_live_everywhere(bridge, task)
