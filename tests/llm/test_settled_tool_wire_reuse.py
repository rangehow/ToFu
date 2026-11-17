"""Settled wire tool rows keep one identity through provider translation.

Contract: lib/llm/anthropic_outbound/_to_anthropic.py ``_convert_tools`` and
lib/llm/cache.py ``_cache_marked_tool``. The settled wire surface keeps its
row identity across rounds (docs/HARNESS_PERFORMANCE.md, "Settled wire tool
surface"); the translated and cache-marked rows must too, or every per-row
memo after translation (sealed encode, byte fingerprints) misses each round.
"""
from __future__ import annotations

import json

import pytest

from lib.conversation_sync.settled_rows import SettledRowMutation, is_settled, settle

pytestmark = pytest.mark.unit


def _tools():
    return settle([
        {'type': 'function', 'function': {
            'name': f'tool_{index}', 'description': 'd',
            'parameters': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}}
        for index in range(3)])


def test_settled_rows_translate_once_to_the_same_settled_anthropic_rows():
    from lib.llm.anthropic_outbound._to_anthropic import _convert_tools
    tools = list(_tools())
    first, second = _convert_tools(tools), _convert_tools(list(tools))
    assert all(a is b and is_settled(a) for a, b in zip(first, second))
    plain = _convert_tools(json.loads(json.dumps(tools)))
    assert json.dumps(plain) == json.dumps(first), 'bytes are those of a fresh translation'
    assert not any(is_settled(row) for row in plain)
    with pytest.raises(SettledRowMutation):
        first[0]['name'] = 'renamed'


def test_cache_marked_last_tool_is_one_settled_row_per_marker():
    from lib.llm.cache import _cache_marked_tool
    tool = _tools()[-1]
    marker = {'type': 'ephemeral', 'ttl': '1h'}
    marked = _cache_marked_tool(tool, tool['function'], marker)
    assert marked is _cache_marked_tool(tool, tool['function'], dict(marker))
    assert is_settled(marked) and marked['function']['cache_control'] == marker
    other = _cache_marked_tool(tool, tool['function'], {'type': 'ephemeral'})
    assert other is not marked and other['function']['cache_control'] == {'type': 'ephemeral'}
    live = {'type': 'function', 'function': {'name': 'x'}}
    assert not is_settled(_cache_marked_tool(live, live['function'], marker))


def test_claude_cloak_renames_translated_rows_copy_on_write():
    from lib.llm.anthropic_outbound._to_anthropic import _convert_tools
    from lib.oauth.outbound import apply_claude_cloak
    tools = _convert_tools(list(_tools()))
    body = {'model': 'claude-sonnet-4-5', 'max_tokens': 16, 'tools': list(tools),
            'messages': [{'role': 'user', 'content': 'hi'}]}
    cloaked, _reverse = apply_claude_cloak(body)
    assert [row['name'] for row in tools] == ['tool_0', 'tool_1', 'tool_2']
    assert all(isinstance(row, dict) for row in cloaked['tools'])


def test_static_prefix_hash_is_kept_per_settled_floor_and_matches_a_fresh_hash():
    import copy
    from lib.tasks_pkg.wire_fingerprint import static_prefix_hash
    messages = [{'role': 'system', 'content': 'S' * 1000}, {'role': 'user', 'content': 'first'},
                {'role': 'assistant', 'content': 'a'}]
    plain = static_prefix_hash(copy.deepcopy(messages))
    settled = list(settle(messages))
    assert static_prefix_hash(settled) == plain == static_prefix_hash(settled + [settled[-1]])
    changed = list(settled)
    changed[0] = settle({'role': 'system', 'content': 'T' * 1000})
    assert static_prefix_hash(changed) != plain, 'another system row is another floor'
    assert static_prefix_hash(settled) == plain
