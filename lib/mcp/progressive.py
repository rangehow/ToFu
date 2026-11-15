"""Retired progressive MCP wrappers: historical search/read/write calls.

Requests once reached MCP through three wrapper tools:

* ``search_mcp_tools`` discovered enabled tools and their exact schemas.
* ``call_mcp_read_tool`` invoked only tools annotated read-only.
* ``call_mcp_write_tool`` invoked the others under serial dispatch and
  human approval.

MCP tools are now search-only (``lib/tools/mcp_exposure.py``): the shared
``search_tools``/``execute_tools`` gateway reaches them and no assembly
declares these wrappers. The names, catalog search and call helpers remain for
the dispatcher, approval and display of rounds recorded under the old wrappers.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from lib.log import get_logger
from lib.mcp.types import parse_namespaced_name

logger = get_logger(__name__)


MCP_SEARCH_TOOL_NAME = 'search_mcp_tools'
MCP_READ_TOOL_NAME = 'call_mcp_read_tool'
MCP_WRITE_TOOL_NAME = 'call_mcp_write_tool'
MCP_PROGRESSIVE_TOOL_NAMES = frozenset({
    MCP_SEARCH_TOOL_NAME,
    MCP_READ_TOOL_NAME,
    MCP_WRITE_TOOL_NAME,
})

_MAX_SEARCH_RESULTS = 8


def _query_terms(query: str) -> list[str]:
    """Tokenize a multilingual search query without external dependencies."""
    query = (query or '').strip().lower()
    if not query:
        return []
    terms = re.findall(r'[\w.-]+', query, flags=re.UNICODE)
    return list(dict.fromkeys([query, *terms]))


def search_mcp_catalog(bridge, query: str, *, server: str = '',
                       limit: int = 5) -> str:
    """Search enabled MCP definitions and return bounded schema-rich JSON."""
    terms = _query_terms(query)
    if not terms:
        return json.dumps({
            'error': 'query must not be empty',
            'matches': [],
        }, ensure_ascii=False)

    server_filter = (server or '').strip().lower()
    try:
        result_limit = max(1, min(_MAX_SEARCH_RESULTS, int(limit)))
    except (TypeError, ValueError) as e:
        logger.debug('[MCP:Progressive] invalid search limit %r (%s); using 5',
                     limit, e)
        result_limit = 5

    safety = bridge.get_tool_safety()
    ranked: list[tuple[int, str, dict[str, Any], str]] = []
    for tool_def in bridge.get_openai_tool_defs():
        fn = tool_def.get('function') or {}
        name = str(fn.get('name') or '')
        parsed = parse_namespaced_name(name)
        if not name or parsed is None:
            continue
        server_name, short_name = parsed
        if server_filter and server_name.lower() != server_filter:
            continue

        description = str(fn.get('description') or '')
        name_text = f'{name} {short_name}'.lower()
        desc_text = description.lower()
        score = 0
        full_query = terms[0]
        if full_query == name.lower() or full_query == short_name.lower():
            score += 200
        if full_query in name_text:
            score += 80
        elif full_query in desc_text:
            score += 30
        for term in terms[1:]:
            if term in name_text:
                score += 20
            elif term in desc_text:
                score += 6
        if score <= 0:
            continue
        ranked.append((score, name, fn, server_name))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    matches = []
    for score, name, fn, server_name in ranked[:result_limit]:
        read_only = bool(safety.get(name, False))
        matches.append({
            'name': name,
            'server': server_name,
            'description': fn.get('description') or '',
            # Raw MCP schemas arrive with unbounded per-property prose (a
            # realistic GitHub/Notion tool runs 1.5-4 KB); project the same
            # bounded callable shape search_tools uses, keeping bounded
            # descriptions because this response is the only schema source
            # the model gets before invoking.
            'input_schema': _compact_mcp_schema(
                fn.get('parameters') or {'type': 'object', 'properties': {}}),
            'read_only': read_only,
            'invoke_with': (MCP_READ_TOOL_NAME if read_only
                            else MCP_WRITE_TOOL_NAME),
        })

    out = {
        'query': query,
        'server': server or None,
        'matches': matches,
        'instruction': (
            'Call invoke_with using {"name": match.name, "arguments": {...}}. '
            'Arguments must match input_schema exactly.'
        ),
    }
    # Snapshot the FULL ranked candidate set before the bounded page below
    # pops the lowest-ranked tail. The page budget bounds the model view; the
    # complete selection is retained canonically so the artifact layer can
    # recover omitted candidates without re-running discovery.
    full_out = json.dumps(out, ensure_ascii=False, separators=(',', ':'))
    # Hard output budget: drop the lowest-ranked tail rather than return an
    # unbounded page. Argument names/required fields are never truncated.
    while out['matches'] and len(json.dumps(
            out, ensure_ascii=False, separators=(',', ':'))) \
            > _SEARCH_MAX_RESULT_CHARS:
        out['matches'].pop()
        out['truncated_for_budget'] = True
    if out.get('truncated_for_budget'):
        out['instruction'] += (
            ' The page hit its bounded output budget; narrow the query for '
            'the omitted candidates.')
        model_text = json.dumps(out, ensure_ascii=False, separators=(',', ':'))
        from lib.tools.result_value import (CapturedToolResult, ResultPart,
                                            TrustedResultText)
        return TrustedResultText(model_text, source_omitted=True, canonical=CapturedToolResult(
            (ResultPart('text', full_out),),
            source_coverage='complete', retention_coverage='complete'))
    return json.dumps(out, ensure_ascii=False, separators=(',', ':'))


_SEARCH_MAX_RESULT_CHARS = 24_000
_SEARCH_SCHEMA_KEYS = frozenset({
    'type', 'enum', 'const', 'default', 'required', 'additionalProperties',
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'minLength', 'maxLength', 'minItems', 'maxItems', 'uniqueItems',
    'format', 'pattern', 'nullable',
})
_SCHEMA_DESC_MAX = 180


def _compact_mcp_schema(schema: Any, *, depth: int = 0) -> dict[str, Any]:
    """Project a callable JSON schema without unbounded contract prose.

    Mirrors ``lib.tools.gateway._compact_arguments_schema`` (duplicated here
    to keep the MCP package free of a tools-gateway import), except
    descriptions are kept — bounded — because ``search_mcp_tools`` is the
    model's only schema source before it invokes the tool.
    """
    if not isinstance(schema, dict) or depth > 8:
        return {'type': 'object', 'properties': {}} if depth == 0 else {}
    out = {
        key: copy.deepcopy(value)
        for key, value in schema.items()
        if key in _SEARCH_SCHEMA_KEYS
    }
    description = str(schema.get('description') or '')
    if description:
        out['description'] = description[:_SCHEMA_DESC_MAX] + (
            '…' if len(description) > _SCHEMA_DESC_MAX else '')
    properties = schema.get('properties')
    if isinstance(properties, dict):
        out['properties'] = {
            str(name): _compact_mcp_schema(value, depth=depth + 1)
            for name, value in properties.items()
            if isinstance(value, dict)
        }
    items = schema.get('items')
    if isinstance(items, dict):
        out['items'] = _compact_mcp_schema(items, depth=depth + 1)
    for choice_key in ('oneOf', 'anyOf', 'allOf'):
        choices = schema.get(choice_key)
        if isinstance(choices, list):
            out[choice_key] = [
                _compact_mcp_schema(choice, depth=depth + 1)
                for choice in choices[:16]
                if isinstance(choice, dict)
            ]
    if depth == 0:
        out.setdefault('type', 'object')
        out.setdefault('properties', {})
    return out


def call_progressive_mcp(bridge, wrapper_name: str,
                         namespaced_name: str, arguments: Any, *, invoke=None) -> str:
    """Validate a progressive wrapper's safety class, then call the MCP tool."""
    name = str(namespaced_name or '').strip()
    if parse_namespaced_name(name) is None:
        return ('MCP tool error: invalid namespaced name. Run search_mcp_tools '
                'and use the exact mcp__server__tool value it returns.')
    if not isinstance(arguments, dict):
        return 'MCP tool error: arguments must be a JSON object.'

    info = bridge.get_tool_info(name)
    if info is None:
        return ('MCP tool error: tool is unavailable or unknown. Run '
                'search_mcp_tools again to refresh the enabled catalog.')
    read_only = bool(info.get('read_only_hint', False))
    if wrapper_name == MCP_READ_TOOL_NAME and not read_only:
        return (f'MCP tool error: {name} is not annotated read-only; use '
                f'{MCP_WRITE_TOOL_NAME} so serialization and approval apply.')
    if wrapper_name == MCP_WRITE_TOOL_NAME and read_only:
        return (f'MCP tool error: {name} is read-only; use '
                f'{MCP_READ_TOOL_NAME} for the parallel safe path.')
    return (invoke or bridge.call_tool)(name, arguments)
