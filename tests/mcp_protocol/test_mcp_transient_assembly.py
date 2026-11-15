"""Headless and durable roots publish MCP authority without any conversation read.

MCP tools are search-only (``lib/tools/mcp_exposure.py``): no root declares an
MCP wire, so neither a client-supplied conversation label nor a missing durable
conversation reaches the declaration store, and every request assembles its
own live authority.
"""
import pytest

from lib.mcp import tool_search
from lib.storage.mcp_declarations import MCPDeclarationError, MCPDeclarationStore
from lib.tasks_pkg.model_config import _assemble_tool_list
from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
from tests.mcp_protocol.test_mcp_declaration_lifecycle import bridge
from tests.mcp_protocol.test_mcp_tool_search import _large_snapshot
from tests.goals.test_goal_invocation_integration import runtime as _invocation_runtime_fixture
invocation_runtime = _invocation_runtime_fixture

pytestmark = pytest.mark.unit


@pytest.fixture
def assembly(monkeypatch):
    tool_search.clear_tool_search_caches()
    bridge(monkeypatch, _large_snapshot()[:2])
    accesses = []
    class MissingConversation:
        def query(self, operation, payload):
            accesses.append((operation, payload))
            assert operation == 'conversation.get'
            return None
    monkeypatch.setattr(MCPDeclarationStore, '_storage', lambda self: MissingConversation())
    cfg = {'userId': 7, 'model': 'test', 'memoryEnabled': False, 'schedulerEnabled': False,
           'preferencesEnabled': False, 'plugins': []}
    task = {'id': 'transient-mcp-test', 'convId': 'client-supplied-job-label', '_userId': 7,
            'messages': [{'role': 'user', 'content': 'hope search'}], 'config': cfg}
    resolved = {'project_path': '', 'project_enabled': False, 'search_mode': 'off',
                'search_enabled': False, 'fetch_enabled': False, 'code_exec_enabled': False,
                'browser_enabled': False, 'desktop_enabled': False, 'image_gen_enabled': False}
    yield cfg, task, resolved, accesses
    tool_search.clear_tool_search_caches()


def _mcp_names(tools):
    return [name for name in ((tool.get('function') or {}).get('name', '') for tool in tools or ())
            if name.startswith('mcp__')]


@pytest.mark.parametrize('runtime_flag', ['_inline_messages', '_transientRuntime', None])
def test_roots_publish_mcp_authority_without_reading_a_conversation(assembly, runtime_flag):
    """Headless and durable roots alike: authority, no wire, no declaration read."""
    cfg, task, resolved, accesses = assembly
    if runtime_flag:
        task[runtime_flag] = True
    wire, _ = assemble_round_tools(cfg, task, resolved)
    assert not accesses
    assert _mcp_names(cfg['_mcpAllowedToolCatalog'])
    assert _mcp_names(task['_executable_tool_catalog'])
    assert _mcp_names(wire) == []


def test_storage_free_direct_assembly_ignores_a_supplied_conversation_label(assembly):
    cfg, task, resolved, accesses = assembly
    cfg['_storageFreeRuntime'] = True
    tools, _ = _assemble_tool_list(cfg, '', False, task['id'], 'off', False,
                                   False, False, False, False,
                                   messages=task['messages'], conv_id=task['convId'])
    assert not accesses
    assert cfg['_mcpAllowedToolCatalog']
    assert _mcp_names(tools) == []


def test_each_headless_request_assembles_its_own_live_authority(assembly, monkeypatch):
    cfg, task, resolved, accesses = assembly
    task['_inline_messages'] = True
    assemble_round_tools(cfg, task, resolved)
    first = _mcp_names(task['_executable_tool_catalog'])
    bridge(monkeypatch, _large_snapshot()[2:4])
    task['id'] += '-next-request'
    assemble_round_tools(cfg, task, resolved)
    assert not accesses
    assert _mcp_names(task['_executable_tool_catalog']) == [
        row['namespaced_name'] for row in _large_snapshot()[2:4]] != first


def test_headless_root_reaches_the_provider_and_terminal_delivery_with_mcp_enabled(assembly, invocation_runtime, monkeypatch):
    from lib.tasks_pkg.orchestrator._run import run_task
    from lib.tasks_pkg.manager import _stream
    from lib.llm.stream_result import ProviderStreamResult
    from lib.turn_verdict import derive_task_verdict, TurnStatus
    _, _, _, accesses = assembly
    parent = invocation_runtime.parent
    parent['convId'] = 'client-supplied-job-label'
    parent['config'].update(mcpEnabled=True, memoryEnabled=False, preferencesEnabled=False,
                            schedulerEnabled=False, fetchEnabled=False, browserEnabled=False,
                            desktopEnabled=False, codeExecEnabled=False, imageGenEnabled=False)
    requests = []
    def provider(body, **kwargs):
        requests.append(body)
        assert kwargs['owner_user_id'] == parent['_userId']
        kwargs['on_content']('Completed the caller-owned headless request.')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Completed the caller-owned headless request.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr(_stream, 'dispatch_stream', provider)
    run_task(parent)
    assert derive_task_verdict(parent).status is TurnStatus.COMPLETED
    assert len(requests) == 1 and not accesses
    assert _mcp_names(requests[0].get('tools')) == []
    assert _mcp_names(parent['_executable_tool_catalog'])
    assert parent['usage']['prompt_tokens'] == 20


def test_transient_wire_validation_never_imports_storage(monkeypatch):
    import builtins
    original = builtins.__import__
    def imported(name, *args, **kwargs):
        if name == 'lib.storage' or name.startswith('lib.storage.'):
            raise AssertionError('transient declaration validation imported storage')
        return original(name, *args, **kwargs)
    tool_search.clear_tool_search_caches()
    monkeypatch.setattr(builtins, '__import__', imported)
    try:
        assert tool_search.freeze_wire_definitions('task:empty-wire', []) == []
        value = [{'type': 'function', 'function': {'name': 'mcp__sample__read'}}]
        frozen = tool_search.freeze_wire_definitions('task:nonempty-wire', value)
        value[0]['function']['name'] = 'changed'
        assert frozen[0]['function']['name'] == 'mcp__sample__read'
        with pytest.raises(MCPDeclarationError, match='Malformed'):
            tool_search.freeze_wire_definitions('task:malformed-wire', value)
        with pytest.raises(MCPDeclarationError, match='Malformed MCP conversation scope'):
            tool_search.frozen_wire_definitions('conversation:invalid')
    finally:
        tool_search.clear_tool_search_caches()
