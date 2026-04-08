"""lib/mcp/config.py — Persistent configuration for MCP servers.

Reads/writes ``data/config/mcp_servers.json``.

Config format::

    {
      "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": "YOUR_GITHUB_TOKEN"},
        "transport": "stdio",
        "enabled": true,
        "description": "GitHub PR/Issue management"
      },
      "tavily": {
        "command": "npx",
        "args": ["-y", "@anthropic/mcp-server-tavily"],
        "env": {"TAVILY_API_KEY": "tvly-xxx"},
        "enabled": true
      }
    }
"""

from __future__ import annotations

import json
import os
from typing import Any

from lib.log import get_logger
from lib.mcp.types import MCP_CONFIG_FILENAME, MCPServerConfig

logger = get_logger(__name__)

# ── Locate config dir ──
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG_DIR = os.path.join(_BASE_DIR, 'data', 'config')


def _config_path() -> str:
    return os.path.join(_CONFIG_DIR, MCP_CONFIG_FILENAME)


def load_mcp_config() -> dict[str, MCPServerConfig]:
    """Load MCP server configurations from disk.

    Returns:
        Dict mapping server_name → MCPServerConfig.
        Empty dict if no config file exists.
    """
    path = _config_path()
    if not os.path.isfile(path):
        logger.debug('[MCP:Config] No config file at %s', path)
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning('[MCP:Config] Config file is not a dict, ignoring: %s', path)
            return {}
        logger.info('[MCP:Config] Loaded %d server configs from %s', len(data), path)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('[MCP:Config] Failed to load config: %s', e)
        return {}


def save_mcp_config(config: dict[str, MCPServerConfig]) -> bool:
    """Save MCP server configurations to disk.

    Args:
        config: Dict mapping server_name → MCPServerConfig.

    Returns:
        True on success, False on failure.
    """
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info('[MCP:Config] Saved %d server configs to %s', len(config), path)
        return True
    except OSError as e:
        logger.error('[MCP:Config] Failed to save config: %s', e, exc_info=True)
        return False


def upsert_server(name: str, server_cfg: dict[str, Any]) -> dict[str, MCPServerConfig]:
    """Add or update a single MCP server config.

    Args:
        name: Server name (used as namespace in tool names).
        server_cfg: Server configuration dict.

    Returns:
        The updated full config dict.
    """
    config = load_mcp_config()
    config[name] = server_cfg
    save_mcp_config(config)
    logger.info('[MCP:Config] Upserted server %r', name)
    return config


def remove_server(name: str) -> dict[str, MCPServerConfig]:
    """Remove a MCP server config.

    Returns:
        The updated full config dict.
    """
    config = load_mcp_config()
    if name in config:
        del config[name]
        save_mcp_config(config)
        logger.info('[MCP:Config] Removed server %r', name)
    else:
        logger.warning('[MCP:Config] Server %r not found in config', name)
    return config
