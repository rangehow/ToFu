"""A task's MCP schemas follow the live server across restarts and upgrades.

Regression (2026-11-17): xuecheng-mcp was upgraded while Worker turns were
running. The bridge reconnected and re-listed the new schemas, but each task
kept the ``read_doc`` schema it snapshotted at turn start, whose ``max_rows``
property had a default. Gateway normalization filled that default, so every
call sent ``max_rows`` and the upgraded server rejected it as an unadvertised
property. The fixture is a local raw stdio peer that, like the real server,
validates arguments against the schema it currently advertises.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time

import pytest


pytestmark = pytest.mark.unit


_SERVER = textwrap.dedent(
    r'''
    import json
    import sys

    with open(sys.argv[1], encoding='utf-8') as version_file:
        version = version_file.read().strip()
    doc_properties = {'doc': {'type': 'string'}}
    if version == 'old':
        doc_properties['max_rows'] = {
            'type': 'integer', 'minimum': 1, 'maximum': 5000, 'default': 2000}
    tools = [
        {'name': 'read_doc', 'description': 'Read a document (' + version + ').',
         'inputSchema': {'type': 'object', 'properties': doc_properties,
                         'required': ['doc'], 'additionalProperties': False}},
        {'name': 'ping', 'description': 'Liveness.',
         'inputSchema': {'type': 'object', 'properties': {}}},
    ]

    def send(request_id, *, result=None, error=None):
        message = {'jsonrpc': '2.0', 'id': request_id}
        if error is not None:
            message['error'] = error
        else:
            message['result'] = result
        sys.stdout.write(json.dumps(message) + '\n')
        sys.stdout.flush()

    def text(value, is_error=False):
        return {'content': [{'type': 'text', 'text': value}], 'isError': is_error}

    for line in sys.stdin:
        request = json.loads(line)
        method = request.get('method', '')
        request_id = request.get('id')
        if request_id is None:
            continue
        if method == 'initialize':
            send(request_id, result={
                'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'fake-docs', 'version': version}})
        elif method == 'tools/list':
            send(request_id, result={'tools': tools})
        elif method == 'tools/call':
            params = request.get('params') or {}
            arguments = params.get('arguments') or {}
            if params.get('name') == 'read_doc':
                extra = sorted(set(arguments) - set(doc_properties))
                if extra:
                    send(request_id, result=text(json.dumps({
                        'ok': False, 'error_code': 'BAD_ARGUMENTS',
                        'error': 'arguments contains an unadvertised property',
                        'extra': extra}), True))
                else:
                    send(request_id, result=text('doc ' + arguments['doc'] + ' @' + version))
            else:
                send(request_id, result=text('pong @' + version))
        else:
            send(request_id, error={'code': -32601, 'message': 'Method not found'})
    '''
)

READ_DOC = 'mcp__docs__read_doc'


@pytest.fixture
def upgraded_server(tmp_path, monkeypatch):
    """Connect to the old server, snapshot a task, then upgrade and restart it."""
    import lib.mcp
    import lib.mcp.client._bridge as bridge_module
    from lib.tools.contracts import compile_execution_contract_documents

    script = tmp_path / 'fake_docs_mcp.py'
    script.write_text(_SERVER, encoding='utf-8')
    version = tmp_path / 'version'
    version.write_text('old', encoding='utf-8')
    bridge = bridge_module.MCPBridge(owner_user_id=7)
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: bridge)
    monkeypatch.setattr(bridge_module, 'MCP_STDIO_IDLE_SECONDS', 1)
    try:
        bridge.connect_server('docs', {
            'transport': 'stdio', 'command': sys.executable,
            'args': [str(script), str(version)], 'enabled': True,
        })
        catalog = bridge.get_openai_tool_defs()
        task = {
            '_userId': 7,
            '_executable_tool_catalog': catalog,
            '_toolContractDocumentsByName': compile_execution_contract_documents(
                catalog, namespace='docs'),
        }
        assert 'max_rows' in _parameters(task, READ_DOC)['properties']

        # The upgrade lands while the parked server keeps its catalog; the next
        # call wakes it on the new code, exactly as the incident's search did.
        version.write_text('new', encoding='utf-8')
        bridge._last_activity['docs'] = time.monotonic() - 2
        assert bridge._park_idle_stdio_server('docs') is True
        assert bridge.call_tool('mcp__docs__ping', {}) == 'pong @new'
        yield bridge, task
    finally:
        bridge.disconnect_all()


def _parameters(task, name):
    for tool in task['_executable_tool_catalog']:
        if tool['function']['name'] == name:
            return tool['function']['parameters']
    raise AssertionError(f'{name} missing from the task catalog')


def test_task_calls_after_a_server_upgrade_use_the_live_schema(upgraded_server):
    from lib.tasks_pkg.handlers.tool_gateway import _executable_catalog
    from lib.tools.gateway import normalize_gateway_call

    bridge, task = upgraded_server
    catalog = _executable_catalog(task)
    call, error = normalize_gateway_call(
        {'name': READ_DOC, 'arguments': {'doc': '42'}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='gw', index=0,
        source='execute_calls',
        contract_documents_by_name=task['_toolContractDocumentsByName'])

    assert error is None
    assert call['_normalized_arguments'] == {'doc': '42'}
    assert bridge.call_tool(READ_DOC, call['_normalized_arguments']) == 'doc 42 @new'


def test_task_search_discloses_the_live_schema_after_a_server_upgrade(upgraded_server):
    from lib.tasks_pkg.handlers.tool_gateway import _executable_catalog
    from lib.tools.gateway import search_executable_catalog

    _bridge, task = upgraded_server
    catalog = _executable_catalog(task)
    result = search_executable_catalog(
        catalog, 'read_doc', namespace='', limit=5, cursor='',
        contract_documents_by_name=task['_toolContractDocumentsByName'])

    item = next(item for item in result['items'] if item['name'] == READ_DOC)
    assert set(item['arguments_schema']['properties']) == {'doc'}
    assert 'Read a document (new).' in json.dumps(item, ensure_ascii=False)
    assert set(_parameters(task, READ_DOC)['properties']) == {'doc'}
