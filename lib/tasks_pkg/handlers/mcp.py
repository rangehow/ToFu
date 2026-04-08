# HOT_PATH
"""MCP tool handler — dispatches tool calls to MCP servers via the bridge.

Unlike other handlers that register for specific tool names, MCP tools are
dynamic: their names are only known at runtime after connecting to MCP servers.
The handler is registered as a **fallback** on the ToolRegistry that catches
any ``mcp__*`` prefixed tool name.

Registration pattern:
  - We don't use @tool_registry.handler() because MCP tool names are dynamic.
  - Instead, we extend ToolRegistry.lookup() to fall back to the MCP handler
    for any tool name starting with ``mcp__``.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.mcp.types import MCP_TOOL_PREFIX
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry

logger = get_logger(__name__)


def handle_mcp_tool(
    task: dict[str, Any],
    tc: dict[str, Any],
    fn_name: str,
    tc_id: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    all_tools: list[dict] | None = None,
) -> tuple[str, str, bool]:
    """Handle an MCP tool call by dispatching to the MCP bridge.

    This handler is invoked by the ToolRegistry fallback for any tool name
    that starts with ``mcp__``.
    """
    from lib.mcp import get_bridge

    bridge = get_bridge()
    tid = task.get('id', '?')[:8]

    # Log the call
    _log_args = str(fn_args)[:300]
    logger.info('[Task %s] [MCP] %s called with args=%s', tid, fn_name, _log_args)

    t0 = time.time()
    try:
        tool_content = bridge.call_tool(fn_name, fn_args)
    except Exception as e:
        elapsed = time.time() - t0
        logger.error('[Task %s] [MCP] %s failed after %.1fs: %s',
                     tid, fn_name, elapsed, e, exc_info=True)
        tool_content = f'❌ MCP tool error: {e}'

    elapsed = time.time() - t0

    # Extract server name for display
    info = bridge.get_tool_info(fn_name)
    server_name = info['server_name'] if info else '?'
    tool_name = info['tool_name'] if info else fn_name

    logger.info('[Task %s] [MCP] %s.%s completed in %.1fs (result_len=%d)',
                tid, server_name, tool_name, elapsed, len(tool_content))

    # Build metadata for frontend display
    icon = '🔌'
    is_error = tool_content.startswith('❌')
    badge = f'{icon} {server_name}' if not is_error else f'❌ {server_name}'

    meta = _build_simple_meta(
        fn_name, tool_content, source=f'MCP:{server_name}',
        icon=icon,
        title=f'{icon} {server_name}/{tool_name}',
        snippet=tool_content[:120].replace('\n', ' '),
        badge=badge,
        extra={
            'mcpServer': server_name,
            'mcpTool': tool_name,
            'elapsed': round(elapsed, 2),
        },
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False


# ── Register the MCP fallback on the ToolRegistry ──
# We monkey-patch the lookup method to check for MCP tools before
# returning None.  This is cleaner than modifying ToolRegistry itself,
# as the MCP bridge is an optional feature.

_original_lookup = tool_registry.lookup.__func__


def _lookup_with_mcp_fallback(self, fn_name: str, round_entry=None):
    """Extended lookup: try normal registry first, then MCP fallback."""
    result = _original_lookup(self, fn_name, round_entry)
    if result is not None:
        return result

    # MCP fallback: check if this is an MCP tool
    if fn_name.startswith(MCP_TOOL_PREFIX):
        try:
            from lib.mcp import get_bridge
            bridge = get_bridge()
            if bridge.is_mcp_tool(fn_name):
                return handle_mcp_tool
        except Exception as e:
            logger.warning('[MCP] Fallback lookup failed for %s: %s', fn_name, e)

    return None


# Apply the patched lookup
import types as _types
tool_registry.lookup = _types.MethodType(_lookup_with_mcp_fallback, tool_registry)
logger.debug('[MCP] ToolRegistry.lookup patched with MCP fallback')
