"""Owner off-limits queues shared by MCP and the run_command CLI guard.

This module supplies the tool-declared capability binding: only an MCP tool
that declares ``tofu/queue-policy`` receives the calling project in its request
metadata. It never lets model arguments choose the policy location or project.
The actual submission check is in hope-mcp and in the pinned CLI guard.
"""
from __future__ import annotations

from typing import Any, Optional

from lib.tools.hope_queue_policy import CAPABILITY, PROJECT_META


def _declaration(info: Optional[dict]) -> bool:
    meta = (info or {}).get('meta') or {}
    return meta.get(CAPABILITY) == {'version': 1}


def canonical_project_path(task: dict[str, Any]) -> str:
    """The canonical registered project of a task, not its execution worktree."""
    config = task.get('config') or {}
    return str(config.get('canonicalProjectPath') or config.get('projectPath') or '')


def project_meta_for_tool(info: Optional[dict], task: dict[str, Any]) -> Optional[dict[str, str]]:
    """Return request metadata only for a tool that declared the capability."""
    if not _declaration(info):
        return None
    project = canonical_project_path(task)
    return {PROJECT_META: project} if project else None
