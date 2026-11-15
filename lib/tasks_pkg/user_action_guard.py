"""Honor, for one Turn, a tool error whose contract requires the user.

Responsibility: once a settled tool result declares that the user must act
(``lib.tools.user_action``), later calls of the same Turn that need the same
authority are not sent. Each gets the typed ``awaiting_user_action`` result,
and the round that held one ends the Turn as the user-action guard: a Tofu
protective stop (``lib.turn_verdict.ABORT_CAUSES``), never a person's Stop.
The Turn surfaces the requirement once, in its typed error: to the person in an
interactive Turn, and to the Coordinator of a Worker through the settled error
its ``poll`` reads (``userAction``). No count, interval or threshold is
involved: the error's own declaration is the rule.

New user input lifts a requirement. A new Turn starts without one, and a
person's steer clears it at the steer-drain boundary. The two task keys are
runtime state of the live Turn, never persisted: a resumed attempt learns a
requirement again from its first such result.

Entry points: ``record_tool_result`` (tool settlement), ``blocking_requirement``
and ``block_call`` (worker-side admission, before approval),
``stop_awaiting_user_action`` (root loop, after tools),
``clear_user_action_requirements`` (steer drain) and
``awaiting_user_action_envelope`` (the Turn's error envelope).
Policy: docs/contracts/TOOL_EXECUTION_POLICY.md, "Errors that need the user".
"""

from __future__ import annotations

from typing import Any

from lib.error_envelope import make_envelope
from lib.log import get_logger
from lib.tasks_pkg.failure_attribution import attach_failure_attribution
from lib.tool_results.tool_rejection import stamp_tool_rejection
from lib.tools.result_envelope import ToolResultEnvelopeV2
from lib.tools.user_action import (
    AWAITING_USER_ACTION_CODE,
    UserActionRequirement,
    awaiting_user_action_result,
    called_tool,
    requirement_scope,
    user_action_requirement,
)
from lib.turn_verdict import AWAITING_USER_ACTION

logger = get_logger(__name__)

_REQUIREMENTS_KEY = '_userActionRequirements'
_BLOCKED_KEY = '_userActionBlockedCall'


def record_tool_result(task: dict[str, Any], name: str, arguments: Any, content: Any) -> None:
    """Remember the requirement a settled result declares; the first one per scope stays."""
    requirement = user_action_requirement(name, arguments, content)
    if requirement is None:
        return
    requirements = task.setdefault(_REQUIREMENTS_KEY, {})
    if requirements.setdefault(requirement.scope, requirement) is requirement:
        logger.warning(
            '[Task %s] conv=%s %s returned %s, which requires the user (%s); later calls '
            'to %s wait for user input', task.get('id', ''), task.get('convId', ''),
            requirement.tool, requirement.error_code, requirement.required_action,
            requirement.scope)


def blocking_requirement(
        task: dict[str, Any], name: str, arguments: Any) -> UserActionRequirement | None:
    """The requirement this call would meet, when one holds for this Turn."""
    requirements = task.get(_REQUIREMENTS_KEY)
    if not requirements:
        return None
    return requirements.get(requirement_scope(name, arguments))


def block_call(task: dict[str, Any], name: str, arguments: Any, call_id: str,
               round_entry: dict[str, Any] | None,
               requirement: UserActionRequirement) -> ToolResultEnvelopeV2:
    """Keep one call from its tool: mark its row and return its typed result."""
    result = awaiting_user_action_result(requirement)
    task.setdefault(_BLOCKED_KEY, {
        'tool': called_tool(name, arguments), 'toolCallId': call_id,
        'requirement': requirement})
    stamp_tool_rejection(
        round_entry,
        {'kind': AWAITING_USER_ACTION_CODE, 'tool': name, 'userAction': requirement.to_dict()},
        reason=result.error.message, retryable=False)
    logger.warning('[Task %s] conv=%s %s (%s) not sent: %s waits for the user (%s)',
                   task.get('id', ''), task.get('convId', ''), name, call_id,
                   requirement.scope, requirement.required_action)
    return result


def awaiting_user_action_envelope(
        requirement: UserActionRequirement, *, model: str = '') -> dict[str, Any]:
    """The Turn's one typed error: what the user must do before its calls can succeed."""
    tool, scope = requirement.tool, requirement.scope
    code = requirement.error_code or 'a typed error'
    action = requirement.required_action or 'an action only the user can take'
    detail = (f'{tool} returned {code}, which requires the user ({action}); later calls to '
              f'{scope} were not sent. This was not a manual stop. {requirement.message}')
    hint = (
        f'• {tool} 返回 {code}，该错误要求由你处理（{action}）。本轮已停止调用 {scope} 的工具，'
        '不再重复发送无法成功的请求。\n'
        '• 完成该操作（例如在大象中批准登录，或同意重新发起一次登录）后，在此对话中回复即可继续。\n\n'
        f'• {tool} returned {code}, which requires your action ({action}). This Turn stopped '
        f'calling {scope} tools instead of repeating a request that cannot succeed.\n'
        '• Complete that action (for example approve the 大象 login, or agree to a new login '
        'request), then reply in this conversation to continue.')
    return make_envelope(
        'tool_call_rejected',
        message='⏸️ 需要你的操作后才能继续\nWaiting for your action before this can continue',
        detail=detail, hint=hint, model=model, context='awaiting-user-action',
        source='orchestrator', raw=f'abort_reason={AWAITING_USER_ACTION} scope={scope} code={code}',
        retryable=False, extensions={'userAction': requirement.to_dict()})


def stop_awaiting_user_action(task: dict[str, Any], state: Any, *, round_num: int, tid: str) -> bool:
    """End the Turn after a round that kept a call from its tool; True when it stopped."""
    blocked = task.get(_BLOCKED_KEY)
    if not blocked or task.get('aborted'):
        # Without a blocked call nothing waits; an earlier stop keeps its own cause.
        return False
    requirement = blocked['requirement']
    task['error'] = attach_failure_attribution(
        awaiting_user_action_envelope(requirement, model=getattr(state, 'model', '') or ''),
        task, phase=AWAITING_USER_ACTION, tool_name=blocked['tool'],
        tool_call_id=blocked['toolCallId'])
    task['aborted'] = True
    task['_abort_reason'] = AWAITING_USER_ACTION
    state.exit_reason = AWAITING_USER_ACTION
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event
    append_event(task, build_event(
        EventType.ROUND_END, roundNum=round_num, reason=AWAITING_USER_ACTION))
    logger.warning('[%s] conv=%s USER-ACTION GUARD stop after round %d: %s returned %s; '
                   '%s waits for the user (%s)', tid, task.get('convId', ''), round_num + 1,
                   requirement.tool, requirement.error_code, requirement.scope,
                   requirement.required_action)
    return True


def clear_user_action_requirements(task: dict[str, Any]) -> bool:
    """A person's new input lifts every requirement; True when one was held."""
    return bool(task.pop(_REQUIREMENTS_KEY, None))


__all__ = [
    'awaiting_user_action_envelope',
    'block_call',
    'blocking_requirement',
    'clear_user_action_requirements',
    'record_tool_result',
    'stop_awaiting_user_action',
]
