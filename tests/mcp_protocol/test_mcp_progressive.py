"""Safety of the retired progressive MCP wrappers' historical search/call helpers."""

from __future__ import annotations

import json

import pytest

from lib.mcp.progressive import (
    MCP_READ_TOOL_NAME,
    MCP_WRITE_TOOL_NAME,
    call_progressive_mcp,
    search_mcp_catalog,
)

pytestmark = pytest.mark.unit


def _tool(name: str, *, description: str = '', read_only: bool = False):
    definition = {
        'type': 'function',
        'function': {
            'name': name,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': {'query': {'type': 'string'}},
            },
        },
    }
    return definition, read_only


class _Bridge:
    def __init__(self, tools):
        self._defs = [row[0] for row in tools]
        self._safety = {
            row[0]['function']['name']: row[1] for row in tools
        }
        self.calls = []

    def get_openai_tool_defs(self):
        return self._defs

    def get_tool_safety(self):
        return self._safety

    def get_tool_info(self, name):
        for definition in self._defs:
            fn = definition['function']
            if fn['name'] == name:
                return {
                    'server_name': name.split('__')[1],
                    'tool_name': name.split('__')[-1],
                    'read_only_hint': self._safety[name],
                }
        return None

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({'ok': True, 'name': name})


def test_search_returns_exact_schema_safety_and_wrapper():
    bridge = _Bridge([
        _tool('mcp__github__search_issues',
              description='Search GitHub issues', read_only=True),
        _tool('mcp__github__create_issue',
              description='Create a GitHub issue', read_only=False),
    ])
    result = json.loads(search_mcp_catalog(bridge, 'search issues'))
    match = result['matches'][0]
    assert match['name'] == 'mcp__github__search_issues'
    assert match['read_only'] is True
    assert match['invoke_with'] == MCP_READ_TOOL_NAME
    assert match['input_schema']['properties']['query']['type'] == 'string'


def test_read_write_wrappers_enforce_mcp_safety_annotation():
    bridge = _Bridge([
        _tool('mcp__docs__read_page', read_only=True),
        _tool('mcp__docs__edit_page', read_only=False),
    ])

    denied_write = call_progressive_mcp(
        bridge, MCP_WRITE_TOOL_NAME, 'mcp__docs__read_page', {})
    denied_read = call_progressive_mcp(
        bridge, MCP_READ_TOOL_NAME, 'mcp__docs__edit_page', {})
    assert 'is read-only' in denied_write
    assert 'not annotated read-only' in denied_read
    assert bridge.calls == []

    call_progressive_mcp(
        bridge, MCP_READ_TOOL_NAME, 'mcp__docs__read_page', {'id': 1})
    call_progressive_mcp(
        bridge, MCP_WRITE_TOOL_NAME, 'mcp__docs__edit_page', {'id': 1})
    assert bridge.calls == [
        ('mcp__docs__read_page', {'id': 1}),
        ('mcp__docs__edit_page', {'id': 1}),
    ]


def test_search_budget_page_keeps_full_candidates_canonical(monkeypatch):
    """The bounded page drops low-ranked matches from the MODEL view, but the
    complete ranked candidate set must be retained canonically so the artifact
    layer can recover omitted candidates without re-running discovery."""
    import lib.mcp.progressive as progressive
    monkeypatch.setattr(progressive, '_SEARCH_MAX_RESULT_CHARS', 400)
    tools = [
        _tool(f'mcp__srv__match_{i}',
              description=('match capability guidance. ' * 8))[0]
        for i in range(12)
    ]
    bridge = _Bridge([(definition, True) for definition in tools])
    out = search_mcp_catalog(bridge, 'match', limit=12)
    page = json.loads(out)
    assert page['truncated_for_budget'] is True
    canonical = getattr(out, 'canonical', None)
    assert canonical is not None, 'budget page lost its canonical candidate set'
    full = json.loads(canonical.text())
    # The canonical copy keeps the WHOLE ranked selection; the bounded page
    # dropped some of those candidates, so it is strictly smaller.
    assert len(full['matches']) > len(page['matches'])
    assert {m['name'] for m in page['matches']} <= {
        m['name'] for m in full['matches']}


def test_search_within_budget_stays_a_plain_json_page():
    bridge = _Bridge([
        _tool('mcp__github__search_issues', read_only=True),
    ])
    out = search_mcp_catalog(bridge, 'search issues')
    assert json.loads(out)['matches'][0]['name'] == 'mcp__github__search_issues'
    assert getattr(out, 'canonical', None) is None
