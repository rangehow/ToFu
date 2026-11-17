"""Project task-owned MCP declarations onto the live server schemas.

Responsibility: a task snapshots its executable catalog (OpenAI definitions)
and contract documents when its tools are assembled. A server's advertised
schemas can change during that task: a restart, an upgrade, a parked server
woken on new code, or ``tools/list_changed``. The bridge's tool index always
holds the schemas of the current connection; search and argument
normalization must use those, never the snapshot, or Tofu fills defaults and
validates properties the live server no longer advertises (2026-11-17:
``read_doc`` calls carried a retired ``max_rows`` default and were rejected).

Only the schema and description of names already in the task catalog are
refreshed. Authority (which names a task may call) and per-tool policy
(permission, idempotency, recovery) stay as assembled. A name the bridge does
not currently index (disconnected or mid-restart) is left as is: the bridge
refuses such a call without sending it to any server.

Entry point: ``project_live_mcp_schemas``. Depends on: the owner bridge's
``get_tool_info`` and ``lib.mcp.types.MCP_TOOL_PREFIX``.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from lib.mcp.types import MCP_TOOL_PREFIX


def _function(tool: Any) -> Mapping[str, Any] | None:
    function = tool.get('function') if isinstance(tool, Mapping) else None
    return function if isinstance(function, Mapping) else None


def project_live_mcp_schemas(
    bridge: Any,
    catalog: list[dict[str, Any]],
    documents: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Return ``(catalog, documents)`` on the live schemas, or None if current."""
    projected: list[dict[str, Any]] | None = None
    refreshed_documents: dict[str, Any] | None = None
    for index, tool in enumerate(catalog):
        function = _function(tool)
        name = str((function or {}).get('name') or '')
        if not name.startswith(MCP_TOOL_PREFIX):
            continue
        info = bridge.get_tool_info(name)
        live = _function((info or {}).get('openai_def'))
        if live is None:
            continue
        parameters = live.get('parameters')
        description = str(live.get('description') or '')
        if (function.get('parameters') == parameters
                and function.get('description') == description):
            continue
        if projected is None:
            projected = list(catalog)
            refreshed_documents = dict(documents)
        projected[index] = {**tool, 'function': {
            **function, 'description': description,
            'parameters': copy.deepcopy(parameters)}}
        document = documents.get(name)
        if isinstance(document, Mapping):
            refreshed_documents[name] = {
                **document,
                'summary': description.strip(),
                'help': description.strip(),
                'arguments_schema': copy.deepcopy(parameters),
            }
    if projected is None:
        return None
    return projected, refreshed_documents
