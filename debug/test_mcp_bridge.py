#!/usr/bin/env python3
"""Test script for MCP bridge functionality.

Tests:
  1. Config CRUD (load/save/upsert/remove)
  2. Type helpers (make_namespaced_name, parse_namespaced_name)
  3. Bridge singleton
  4. Tool translation (MCP → OpenAI format)
  5. Live connection test (optional — requires npx + network)

Usage:
    python debug/test_mcp_bridge.py           # unit tests only
    python debug/test_mcp_bridge.py --live     # also test live MCP server
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_types():
    """Test namespace helpers."""
    from lib.mcp.types import make_namespaced_name, parse_namespaced_name, MCP_TOOL_PREFIX

    # Basic roundtrip
    ns = make_namespaced_name('github', 'list_issues')
    assert ns == 'mcp__github__list_issues', f'Expected mcp__github__list_issues, got {ns}'

    parsed = parse_namespaced_name(ns)
    assert parsed == ('github', 'list_issues'), f'Expected ("github", "list_issues"), got {parsed}'

    # Edge cases
    assert parse_namespaced_name('web_search') is None  # not MCP
    assert parse_namespaced_name('mcp__') is None  # incomplete
    assert parse_namespaced_name('mcp____') is None  # empty parts

    # Tool name with underscores
    ns2 = make_namespaced_name('my_server', 'do_the_thing')
    assert ns2 == 'mcp__my_server__do_the_thing'
    parsed2 = parse_namespaced_name(ns2)
    assert parsed2 == ('my_server', 'do_the_thing')

    print('✅ types tests passed')


def test_config():
    """Test config CRUD operations."""
    from lib.mcp import config as mcp_config

    # Use a temp dir to avoid touching real config
    # Use project-local data/tmp/ (/tmp may not be accessible on all machines)
    _project_tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'tmp')
    os.makedirs(_project_tmp, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=_project_tmp) as tmpdir:
        # Monkey-patch config dir
        original_dir = mcp_config._CONFIG_DIR
        mcp_config._CONFIG_DIR = tmpdir

        try:
            # Empty initial load
            cfg = mcp_config.load_mcp_config()
            assert cfg == {}, f'Expected empty config, got {cfg}'

            # Save
            test_cfg = {
                'test_server': {
                    'command': 'echo',
                    'args': ['hello'],
                    'transport': 'stdio',
                    'enabled': True,
                    'description': 'Test MCP server',
                }
            }
            ok = mcp_config.save_mcp_config(test_cfg)
            assert ok, 'save_mcp_config should return True'

            # Load back
            loaded = mcp_config.load_mcp_config()
            assert 'test_server' in loaded, 'test_server should be in loaded config'
            assert loaded['test_server']['command'] == 'echo'

            # Upsert
            mcp_config.upsert_server('new_server', {'command': 'cat', 'args': []})
            loaded2 = mcp_config.load_mcp_config()
            assert 'new_server' in loaded2
            assert 'test_server' in loaded2

            # Remove
            mcp_config.remove_server('test_server')
            loaded3 = mcp_config.load_mcp_config()
            assert 'test_server' not in loaded3
            assert 'new_server' in loaded3

            print('✅ config tests passed')

        finally:
            mcp_config._CONFIG_DIR = original_dir


def test_bridge_singleton():
    """Test bridge singleton and basic state."""
    from lib.mcp.client import get_bridge, MCPBridge

    bridge = get_bridge()
    assert isinstance(bridge, MCPBridge)

    # Same instance
    bridge2 = get_bridge()
    assert bridge is bridge2, 'get_bridge should return singleton'

    # Initial state
    assert bridge.server_count == 0
    assert bridge.tool_count == 0
    assert not bridge.connected
    assert bridge.get_openai_tool_defs() == []
    assert bridge.list_servers() == []

    print('✅ bridge singleton tests passed')


def test_tool_translation():
    """Test MCP Tool → OpenAI format translation."""
    from lib.mcp.client import MCPBridge
    from unittest.mock import MagicMock

    # Create a mock MCP Tool
    mock_tool = MagicMock()
    mock_tool.name = 'search_repos'
    mock_tool.description = 'Search GitHub repositories'
    mock_tool.inputSchema = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string', 'description': 'Search query'},
            'limit': {'type': 'integer', 'description': 'Max results', 'default': 10},
        },
        'required': ['query'],
    }

    result = MCPBridge._tool_to_openai('github', mock_tool)

    assert result['type'] == 'function'
    assert result['function']['name'] == 'mcp__github__search_repos'
    assert '[MCP:github]' in result['function']['description']
    assert result['function']['parameters']['type'] == 'object'
    assert 'query' in result['function']['parameters']['properties']

    print('✅ tool translation tests passed')


def test_handler_registration():
    """Test that MCP handler is registered as a fallback on ToolRegistry."""
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.handlers.mcp import handle_mcp_tool

    # The lookup should return None for unknown non-MCP tools
    assert tool_registry.lookup('totally_unknown_tool_xyz') is None

    # For MCP-prefixed tools, it depends on whether the bridge has them registered
    # (not connected, so it should return None)
    result = tool_registry.lookup('mcp__fake__tool')
    # Since the bridge has no tools registered, this should be None
    assert result is None, f'Expected None for unregistered MCP tool, got {result}'

    print('✅ handler registration tests passed')


def test_display_handler():
    """Test MCP tool display in tool_display module."""
    from lib.tasks_pkg.tool_display import _tool_display_generic

    display, extra = _tool_display_generic('mcp__github__list_issues', {'repo': 'test'}, 'tc_1', '{}')
    assert '🔌' in display, f'Expected plug icon, got: {display}'
    assert 'github' in display
    assert 'list_issues' in display

    print('✅ display handler tests passed')


def test_meta_builder():
    """Test MCP tool metadata builder."""
    from lib.tools.meta import build_project_tool_meta

    meta = build_project_tool_meta('mcp__tavily__search', {'query': 'test'}, 'Some search results here')
    assert 'MCP' in meta.get('source', ''), f'Expected MCP in source, got: {meta}'
    assert '🔌' in meta.get('title', '') or '🔌' in meta.get('badge', '')

    print('✅ meta builder tests passed')


def test_live_connection():
    """Test actual MCP server connection (requires npx).

    This test is SLOW (downloads npm packages) — only run with --live flag.
    """
    import shutil
    if not shutil.which('npx'):
        print('⏭️  Skipping live test — npx not found')
        return

    from lib.mcp.client import MCPBridge

    bridge = MCPBridge()
    try:
        # Use the filesystem MCP server as a test — no API key needed
        tools = bridge.connect_server('filesystem', {
            'command': 'npx',
            'args': ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
            'transport': 'stdio',
        })

        assert len(tools) > 0, 'Should discover at least one tool'
        print(f'  Discovered {len(tools)} tools: {[t.name for t in tools]}')

        # Check OpenAI format
        openai_defs = bridge.get_openai_tool_defs()
        assert len(openai_defs) > 0
        for td in openai_defs:
            assert td['type'] == 'function'
            assert td['function']['name'].startswith('mcp__filesystem__')

        # Try calling a tool
        ns_name = f'mcp__filesystem__list_directory'
        if bridge.is_mcp_tool(ns_name):
            result = bridge.call_tool(ns_name, {'path': '/tmp'})
            print(f'  list_directory result (first 200 chars): {result[:200]}')
            assert len(result) > 0
        else:
            # Some versions use different names
            print(f'  Tool names: {[t["function"]["name"] for t in openai_defs]}')

        print('✅ live connection test passed')
    finally:
        bridge.disconnect_all()


def main():
    live = '--live' in sys.argv

    print('=== MCP Bridge Tests ===\n')

    test_types()
    test_config()
    test_bridge_singleton()
    test_tool_translation()
    test_handler_registration()
    test_display_handler()
    test_meta_builder()

    if live:
        print('\n--- Live Connection Test ---')
        test_live_connection()
    else:
        print('\n⏭️  Skipping live test (pass --live to enable)')

    print('\n🎉 All tests passed!')


if __name__ == '__main__':
    main()
