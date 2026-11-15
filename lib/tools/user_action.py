"""Tool errors whose typed contract says only the user can unblock them.

Responsibility: read, from one tool result, the declaration that the user must
act before the failed call, or any later call that needs the same authority,
can succeed. A result declares it only in its typed error, never in prose or
by error code: ``user_action_required: true``, or a ``model_action`` that tells
the model to ask the user first. MCP servers return that error as
``structuredContent`` on an ``isError`` result. The shared SSO adapter
(``packages/tofu-mtsso-auth``, "错误形状") sends it for every 学城, Hope and LLM
authentication failure. The task engine reads it only here (policy:
``docs/contracts/TOOL_EXECUTION_POLICY.md``, "Errors that need the user").

Entry points: ``user_action_requirement`` (one result's declaration),
``requirement_scope`` (which calls meet the same requirement) and
``awaiting_user_action_result`` (the typed result of a call that was not sent).
Dependencies: the MCP result carrier and tool namespacing, the typed tool error.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Mapping

from lib.tools.result_envelope import ToolResultEnvelopeV2, typed_tool_error

# ``lib.mcp`` is imported where it is used: its package loads the MCP client,
# which the hot dispatch modules that import this one must not load eagerly.

# The model actions of the MCP authentication contract that make the model ask
# the user before anything else. ``stop_and_report`` and
# ``continue_original_tool`` do not ask; a blocked error that needs the user
# also says ``user_action_required: true``.
MODEL_ACTIONS_THAT_ASK_THE_USER = frozenset({
    'ask_user_then_request_login',
    'ask_user_to_approve_then_retry',
})
# The typed error code of a call that was not sent because of a requirement.
AWAITING_USER_ACTION_CODE = 'awaiting_user_action'


@dataclass(frozen=True)
class UserActionRequirement:
    """One typed error's declaration that the user must act.

    ``scope`` names the calls that meet the same requirement
    (``requirement_scope``); ``tool`` is the call that returned the error.
    ``required_action`` is the error's ``recovery_action``, or its
    ``model_action`` when it names none.
    """

    scope: str
    tool: str
    error_code: str
    required_action: str
    model_action: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            'scope': self.scope,
            'tool': self.tool,
            'errorCode': self.error_code,
            'requiredAction': self.required_action,
            'modelAction': self.model_action,
            'message': self.message,
        }


def called_tool(name: str, arguments: Any) -> str:
    """The tool a call runs: a progressive MCP wrapper runs the one it names."""
    from lib.mcp.progressive import MCP_READ_TOOL_NAME, MCP_WRITE_TOOL_NAME
    from lib.mcp.types import parse_namespaced_name
    if name in (MCP_READ_TOOL_NAME, MCP_WRITE_TOOL_NAME) and isinstance(arguments, Mapping):
        target = arguments.get('name')
        if isinstance(target, str) and parse_namespaced_name(target.strip()):
            return target.strip()
    return name


def requirement_scope(name: str, arguments: Any) -> str:
    """The calls that meet the same requirement as this one.

    An MCP server's typed error belongs to the session and identity that all
    of its tools share, so its requirement covers the whole server. A built-in
    tool's covers that tool.
    """
    from lib.mcp.types import parse_namespaced_name
    tool = called_tool(name, arguments)
    parsed = parse_namespaced_name(tool)
    return f'mcp:{parsed[0]}' if parsed else f'tool:{tool}'


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ''


def user_action_requirement(
        name: str, arguments: Any, content: Any) -> UserActionRequirement | None:
    """The requirement one tool result declares, or None.

    Only a failed MCP result's structured content can declare one; its text
    and successful results (a status probe reporting ``login_required``)
    cannot.
    """
    from lib.mcp.result_content import MCPToolResult
    if not isinstance(content, MCPToolResult) or not content.is_error:
        return None
    if not isinstance(content.structured_json, str):
        return None
    try:
        error = json.loads(content.structured_json)
    except ValueError:
        return None
    if not isinstance(error, dict):
        return None
    model_action = _text(error.get('model_action'), 96)
    if (error.get('user_action_required') is not True
            and model_action not in MODEL_ACTIONS_THAT_ASK_THE_USER):
        return None
    return UserActionRequirement(
        scope=requirement_scope(name, arguments),
        tool=called_tool(name, arguments)[:128],
        error_code=_text(error.get('error_code') or error.get('code'), 96),
        required_action=_text(error.get('recovery_action'), 96) or model_action,
        model_action=model_action,
        message=_text(error.get('error') or error.get('message'), 500),
    )


def awaiting_user_action_result(
        requirement: UserActionRequirement) -> ToolResultEnvelopeV2:
    """The typed result of a call not sent because of ``requirement``."""
    code = requirement.error_code or 'a typed error'
    action = requirement.required_action or 'an action only the user can take'
    failure = typed_tool_error(
        AWAITING_USER_ACTION_CODE, retryable=False,
        message=(f'Not sent: {requirement.tool} returned {code} earlier in this Turn, and '
                 f'that error requires the user ({action}) before calls to '
                 f'{requirement.scope} can succeed.'),
        next_action=('Tell the user what is required. This Turn ends here; new user input '
                     'lifts the wait.'))
    return replace(failure, items=({'userAction': requirement.to_dict()},)).with_visible_bytes()


__all__ = [
    'AWAITING_USER_ACTION_CODE',
    'MODEL_ACTIONS_THAT_ASK_THE_USER',
    'UserActionRequirement',
    'awaiting_user_action_result',
    'called_tool',
    'requirement_scope',
    'user_action_requirement',
]
