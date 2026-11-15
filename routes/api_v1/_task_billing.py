"""Task-backed HTTP billing preparation for chat and agent-run.

request_flow owns money operations; ExecutionSession owns rollback/settlement.
This adapter owns only their pre-dispatch binding and the HTTP refusal; the
task-free stream-direct surface answers a refused reservation with the same
``insufficient_credits_response``.
"""

from __future__ import annotations

from collections.abc import Callable

from lib.agent_core.execution_session import ExecutionSession, bind_billing_reservation
from lib.api_response import api_error
from lib.billing.request_flow import (
    estimate_prompt_tokens, release_reservation, reserve_for_task, settle_task,
)


# The wallet, not the task, is short of credit, so recovery is adding credit;
# a task resource cap (``budget_exceeded``) would send the caller to the wrong fix.
_TOP_UP_HINT = 'Top up credits or redeem a top-up code, then retry; this request was not started.'


def insufficient_credits_response(error):
    """The 402 for a refused credit reservation, kind ``insufficient_funds`` on both majors.

    v1 keeps the legacy string ``error`` beside the branchable ``error_kind``
    (docs/contracts/HEADLESS_API.md 3.8); ``error_code`` makes the same kind
    the v4 problem ``code``.
    """
    return api_error(
        f'Insufficient credits. Estimated cost {error.needed_micro / 1_000_000:.4f} credits, '
        f'balance {error.balance_micro / 1_000_000:.4f}. {_TOP_UP_HINT}',
        status=402, error_kind='insufficient_funds', error_code='insufficient_funds',
        balance_micro=error.balance_micro, needed_micro=error.needed_micro)


def prepare_task_billing(
    task: dict, *, account_user_id: str, execution_session: ExecutionSession,
    conversation_id: str, messages: list, config: dict,
    completion_hint: object, model_for_billing: Callable[[], str],
):
    """Return a 402 refusal or None; propagate faults after rejecting the task."""
    if not account_user_id:
        return None
    from lib.billing import InsufficientFunds
    from lib.tasks_pkg.manager import reject_unstarted_chat_task

    try:
        est_completion = int(config.get('maxTokens') or completion_hint or 1024)
        reservation_micro = reserve_for_task(
            task, user_id=account_user_id, model=model_for_billing(),
            prompt_tokens=estimate_prompt_tokens(messages),
            max_completion_tokens=est_completion)
    except InsufficientFunds as e:
        reject_unstarted_chat_task(
            task, e, cause='billing_reservation_refused', conv_id=conversation_id)
        return insufficient_credits_response(e)
    except Exception as exc:
        reject_unstarted_chat_task(
            task, exc, cause='billing_reservation_failed', conv_id=conversation_id)
        raise

    try:
        bind_billing_reservation(
            execution_session,
            reservation_micro=reservation_micro,
            settle=lambda: settle_task(
                task, user_id=account_user_id, model=model_for_billing(),
                raise_on_error=True,
            ),
            release=lambda: release_reservation(
                task, user_id=account_user_id,
                reservation_micro=reservation_micro, raise_on_error=True,
            ),
        )
    except Exception as exc:
        reject_unstarted_chat_task(
            task, exc, cause='billing_bind_failed', conv_id=conversation_id)
        raise
    return None
