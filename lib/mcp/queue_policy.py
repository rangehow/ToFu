"""Owner off-limits queues shared by MCP and the run_command CLI guard.

This module supplies the tool-declared capability binding: only an MCP tool
that declares ``tofu/queue-policy`` receives the calling project in its request
metadata. It never lets model arguments choose the policy location or project.

``submission_refusal`` is the MCP submission check: ``MCPBridge.call_tool``
runs it before every call, so the task handler, the execution-host relay and
the compute provider all pass it. hope-mcp has no policy reader of its own.
It reads the owner's policy with the CLI shims' loader on every call.
"""
from __future__ import annotations

import os
import shlex
from typing import Any, Optional

from lib.log import get_logger
from lib.tools.hope_queue_policy import (
    CAPABILITY, PROJECT_META, _QUEUE_KEYS, _conf_request, _overrides, load_policy,
    queues_in_conf, refusal,
)

logger = get_logger(__name__)

_HOPE_SERVER_NAME = 'hope'
_HOPE_LAUNCHER = 'hope-mcp'
# hope-mcp job-creating catalog operations that carry a ``hopeConf`` body.
_CATALOG_SUBMISSIONS = frozenset({'job_clone', 'job_rerun'})
# A CodeLab container names its queue directly; without one the platform picks.
_CATALOG_QUEUE_BODIES = frozenset({'codelab_create'})


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


def is_hope_server(server_name: str, config: Optional[dict]) -> bool:
    """Catalog id ``hope``, or any server launched through ``hope-mcp``."""
    if server_name == _HOPE_SERVER_NAME:
        return True
    config = config or {}
    argv = [config.get('command'), *(config.get('args') or [])]
    return any(isinstance(part, str) and os.path.basename(part) == _HOPE_LAUNCHER
               for part in argv)


def _argument_overrides(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    return _overrides(tokens)


def _submit_job_request(arguments: dict) -> tuple[list[str], bool]:
    workdir = arguments.get('workdir')
    conf_name = arguments.get('conf_name')
    queues: list[str] = []
    unknown = True
    if isinstance(workdir, str) and workdir:
        args = ([os.path.join(workdir, conf_name)] if isinstance(conf_name, str) and conf_name
                else ['--workdir', workdir])
        queues, unknown = _conf_request(args, workdir)
    if isinstance(arguments.get('queue'), str):
        queues.append(arguments['queue'])
    queues.extend(_argument_overrides(arguments.get('arguments')))
    extra = arguments.get('extra')
    if isinstance(extra, dict):
        for key, value in extra.items():
            if str(key).strip().lower() in _QUEUE_KEYS and value is not None:
                queues.append(str(value))
    return queues, unknown and not any(str(q).strip() for q in queues)


def _conf_body_request(body: Any) -> tuple[list[str], bool]:
    body = body if isinstance(body, str) else ''
    return queues_in_conf(body), not body.strip()


def _submission_request(tool_name: str, arguments: dict) -> Optional[tuple[list[str], bool]]:
    """(queue candidates, conf_unknown) for a job-creating hope-mcp tool, else None."""
    if tool_name == 'submit_job':
        return _submit_job_request(arguments)
    if tool_name == 'confirm_rerun':
        return _conf_body_request(arguments.get('hope_conf'))
    if tool_name == 'catalog_write':
        operation = arguments.get('operation')
        body = arguments.get('body') if isinstance(arguments.get('body'), dict) else {}
        if operation in _CATALOG_SUBMISSIONS:
            return _conf_body_request(body.get('hopeConf'))
        if operation in _CATALOG_QUEUE_BODIES:
            queue = body.get('queue')
            return ([str(queue)] if queue is not None else []), False
    return None


def submission_refusal(server_name: str, server_config: Optional[dict], tool_name: str,
                       arguments: Any, *, owner_user_id: Optional[int],
                       meta: Optional[dict]) -> Optional[dict[str, Any]]:
    """Typed refusal for a Hope job submission to an off-limits queue, else None."""
    if not is_hope_server(server_name, server_config) or not isinstance(arguments, dict):
        return None
    request = _submission_request(tool_name, arguments)
    if request is None:
        return None
    from lib.tools import hope_queue_policy_launcher
    path = hope_queue_policy_launcher.policy_path(owner_user_id) if owner_user_id else ''
    policy = load_policy(path)
    if policy.default_reason:
        logger.warning('[MCP:QueuePolicy] owner policy %s %s; enforcing the built-in '
                       'default entry m17', policy.path or 'file', policy.default_reason)
    project = (meta or {}).get(PROJECT_META)
    queues, unknown = request
    return refusal(queues, conf_unknown=unknown,
                   project=project if isinstance(project, str) and project else None,
                   policy=policy)
