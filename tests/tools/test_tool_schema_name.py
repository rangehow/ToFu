"""lib.tools.contracts.tool_schema_name: the one reader of a provider tool schema's name.

Swarm routing, paper/research executors, MCP exposure, direct-call retention
and background guidance all read names here (2026-11-17 audit: six copies
disagreed on dialect fallbacks).
"""
from __future__ import annotations

import pytest

from lib.tools.contracts import tool_schema_name

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('schema,name', [
    ({'type': 'function', 'function': {'name': 'read_files'}}, 'read_files'),
    ({'name': 'read_files', 'input_schema': {}}, 'read_files'),
    ({'function': {}, 'name': 'top_level'}, 'top_level'),
    ({'function': 'not-a-dict', 'name': 'top_level'}, 'top_level'),
    ({'function': {'name': None}}, ''),
    ({}, ''),
    (None, ''),
    ('read_files', ''),
])
def test_both_wire_dialects_and_malformed_schemas(schema, name):
    assert tool_schema_name(schema) == name


def test_catalog_filters_share_the_one_reader():
    from lib.swarm import routing
    from lib.tools import background_guidance, direct_wire, mcp_exposure
    for module in (routing, background_guidance, direct_wire, mcp_exposure):
        assert module.tool_schema_name is tool_schema_name
