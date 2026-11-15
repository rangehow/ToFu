"""MCP exposure policy: an MCP tool is reachable only through Tool Search.

Owner decision 2026-11-13 (docs/modules/tools_execution.md, "MCP tools are
search-only"): no MCP tool name, schema or listing reaches a provider tool
list or an eagerly composed context block, on any surface (browser chat,
scheduled and channel turns, Router/Coordinator, Workers, swarm children,
headless). MCP stays available: every connected tool remains a member of the
task's executable catalog, ``search_tools`` indexes it and ``execute_tools`` /
``execute_program`` run it through the ordinary approval and execution
pipeline. No project, conversation, grant or request setting changes this.

Entry points:

* :func:`is_mcp_tool_name` -- the ``mcp__<server>__<tool>`` identity
  (``lib.mcp.types.MCP_TOOL_PREFIX``) the bridge and dispatcher already use.
* :func:`without_mcp_tools` / :func:`without_mcp_names` -- drop MCP members
  from a direct-visibility candidate (schemas or names). Never apply them to an
  executable catalog: authority keeps MCP tools.
* :func:`withholds_mcp_tools` -- an authority holding an MCP tool its visible
  surface lacks needs the discovery gateway, however small that surface is.

Callers: the MCP registry family (authority only, ``lib.tools.registry``),
the initial tool surface and delegated child policy
(``lib.tasks_pkg.orchestrator._tool_assembly_prep``,
``lib.tasks_pkg.child_invocation``) and the final provider projection
(``lib.llm.tool_wire``). These never promote an MCP name from authority onto
the wire. A caller-declared explicit schema list is the caller's own tools,
never Tofu's MCP bridge, and is shown as declared.

Dependencies: ``lib.mcp.types`` only.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from lib.mcp.types import MCP_TOOL_PREFIX


def is_mcp_tool_name(name: Any) -> bool:
    return isinstance(name, str) and name.startswith(MCP_TOOL_PREFIX)


def _schema_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ''
    function = tool.get('function')
    return str((function.get('name') if isinstance(function, dict) else '')
               or tool.get('name') or '')


def without_mcp_tools(tools: Iterable[Any] | None) -> list[Any]:
    """Return ``tools`` without MCP schemas, preserving order and identity."""
    return [tool for tool in tools or () if not is_mcp_tool_name(_schema_name(tool))]


def without_mcp_names(names: Iterable[Any] | None) -> list[Any]:
    """Return ``names`` without MCP tool names, preserving order."""
    return [name for name in names or () if not is_mcp_tool_name(name)]


def withholds_mcp_tools(authority: Iterable[Any] | None,
                        visible: Iterable[Any] | None = ()) -> bool:
    """Whether ``authority`` holds an MCP tool the ``visible`` schemas lack."""
    shown = {_schema_name(tool) for tool in visible or ()}
    return any(is_mcp_tool_name(name) and name not in shown
               for name in map(_schema_name, authority or ()))


__all__ = ['is_mcp_tool_name', 'withholds_mcp_tools', 'without_mcp_names',
           'without_mcp_tools']
