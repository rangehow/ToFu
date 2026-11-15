"""Structured failure attribution for turn terminal errors.

One bounded ``failureAttribution`` extension on the error envelope links a
terminal failure to the exact execution context that produced it —
``roundNum`` / ``toolCallId`` / ``toolName`` / ``phase`` — so post-hoc
diagnosis reads the failure record directly instead of re-deriving causality
by joining ``toolRounds`` with the turn trace.

Producers:

- :func:`record_active_round` stamps the loop's current round on the task as
  each dispatch round opens, so even an exception that escapes the loop
  (``handle_task_fatal``) still knows which round it died in.
- :func:`attach_failure_attribution` merges the attribution onto an error
  envelope dict at the failure seam that owns the context (fatal handler,
  circuit-breaker envelopes) — additive, never overriding core fields.
- :func:`ensure_failure_attribution` is the settlement-side backstop: any
  failed terminal settlement whose envelope lacks attribution gets the best
  available one (active round + the newest batch's in-flight or failed call)
  so EVERY durable failure record is traceable.
- :func:`append_turn_retry_receipt` links a discarded auto-retry attempt to
  its successor on the task's bounded ``_turnRetryChain``; the terminal
  settlement then persists the whole chain (see ``turn_lifecycle._settlement``).

All fields are best-effort diagnostics: they never change control flow and
carry no secrets, prompt content, or provider bodies.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from lib.log import get_logger

logger = get_logger(__name__)

#: Cap on the persisted auto-retry chain; a turn's retry budget is smaller
#: than this in every supported configuration.
_MAX_RETRY_CHAIN = 8

#: Envelope extension key (additive — the frontend ErrorEnvelope normalizer
#: passes unknown keys through unchanged).
ATTRIBUTION_KEY = 'failureAttribution'


def record_active_round(task: dict[str, Any], round_num: Any) -> None:
    """Stamp the loop's zero-based dispatch index for internal context consumers."""
    if (isinstance(round_num, int) and not isinstance(round_num, bool)
            and round_num >= 0):
        task['_activeRoundNum'] = round_num


def _newest_batch_tool_round(projection: Mapping[str, Any] | None) -> dict[str, str]:
    """Best-effort tool identity of the terminal's cause: the newest model batch only.

    Its call still unanswered (in flight when the Turn ended) comes first, else
    one of its calls that failed. An older failure is history the model already
    saw and answered, never the cause of a later terminal: on 2026-11-11 a
    storage outage cut a read-only wait, and the settlement blamed a review call
    that had failed explicitly hours earlier.
    """
    from lib.tool_results.tool_round_identity import execution_batch_keys
    from lib.tool_results.tool_round_status import TERMINAL_TOOL_ROUND_STATUSES
    rounds = (projection or {}).get('toolRounds') or []
    if not isinstance(rounds, (list, tuple)):
        return {}
    calls = [row for row in rounds if isinstance(row, Mapping)
             and (row.get('toolName') or row.get('toolCallId'))]
    if not calls:
        return {}
    failed_statuses = {'error', 'failed', 'timeout', 'rejected'}
    keys = execution_batch_keys(calls)
    batch = [row for row, key in zip(calls, keys) if key == keys[-1]]
    in_flight = [row for row in batch if str(row.get('status') or '')
                 not in TERMINAL_TOOL_ROUND_STATUSES | failed_statuses]
    failed = [row for row in batch if str(row.get('status') or '') in failed_statuses]
    selected = in_flight[0] if in_flight else failed[-1] if failed else None
    if selected is None:
        return {}
    found: dict[str, str] = {}
    tool_name = selected.get('toolName')
    tool_call_id = selected.get('toolCallId')
    if isinstance(tool_name, str) and tool_name:
        found['toolName'] = tool_name[:128]
    if isinstance(tool_call_id, str) and tool_call_id:
        found['toolCallId'] = tool_call_id[:128]
    return found


def build_failure_attribution(
    task: dict[str, Any] | None,
    *,
    phase: str = '',
    tool_name: str = '',
    tool_call_id: str = '',
    projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose one bounded attribution dict; empty fields are omitted."""
    attribution: dict[str, Any] = {}
    round_num = (task or {}).get('_activeRoundNum')
    if isinstance(round_num, int) and not isinstance(round_num, bool) \
            and round_num >= 0:
        # Keep the task index zero-based for context composition; public
        # failure receipts use the same one-based ordinal as admission.
        attribution['roundNum'] = round_num + 1
    if phase:
        attribution['phase'] = str(phase)[:64]
    if tool_name:
        attribution['toolName'] = str(tool_name)[:128]
    if tool_call_id:
        attribution['toolCallId'] = str(tool_call_id)[:128]
    if 'toolName' not in attribution and 'toolCallId' not in attribution:
        attribution.update(_newest_batch_tool_round(projection))
    return attribution


def attach_failure_attribution(
    error: Any,
    task: dict[str, Any] | None,
    *,
    phase: str = '',
    tool_name: str = '',
    tool_call_id: str = '',
    projection: Mapping[str, Any] | None = None,
) -> Any:
    """Merge attribution onto an error envelope dict (never overrides).

    Returns the (possibly mutated) error unchanged for non-dict inputs so
    call sites never branch on the envelope's construction path.
    """
    if not isinstance(error, dict) or ATTRIBUTION_KEY in error:
        return error
    attribution = build_failure_attribution(
        task, phase=phase, tool_name=tool_name,
        tool_call_id=tool_call_id, projection=projection)
    if attribution:
        error[ATTRIBUTION_KEY] = attribution
    return error


def ensure_failure_attribution(
    error: Any,
    task: dict[str, Any] | None,
    *,
    projection: Mapping[str, Any] | None = None,
) -> Any:
    """Settlement backstop: a durable failure record always carries
    attribution when any execution context is known."""
    if isinstance(error, dict) and error.get('context') == 'task-start':
        # Replay checkpoints contain earlier tools, not startup failure causes.
        error[ATTRIBUTION_KEY] = {'phase': 'task-start'}
        return error
    return attach_failure_attribution(
        error, task, phase='terminal', projection=projection)


def append_turn_retry_receipt(
    task: dict[str, Any],
    *,
    attempt: int,
    kind: str,
    backoff_s: float,
    error: Any,
) -> None:
    """Link a discarded auto-retry attempt to its successor.

    The receipt is persisted via the terminal settlement's ``retryChain`` —
    including the chain tail when the budget is exhausted — so a retried
    failure is never an orphan: attempt N's envelope and attribution are
    joined to the retry decision that produced attempt N+1.
    """
    chain = task.get('_turnRetryChain')
    if not isinstance(chain, list):
        chain = []
        task['_turnRetryChain'] = chain
    receipt: dict[str, Any] = {
        'attempt': int(attempt),
        'kind': str(kind or 'generic')[:64],
        'backoff_s': round(float(backoff_s or 0), 2),
        'at': round(time.time(), 3),
    }
    if isinstance(error, dict):
        detail = error.get('detail')
        if isinstance(detail, str) and detail:
            receipt['detail'] = detail[:300]
        attribution = error.get(ATTRIBUTION_KEY)
        if isinstance(attribution, dict) and attribution:
            receipt[ATTRIBUTION_KEY] = dict(attribution)
    chain.append(receipt)
    del chain[:max(0, len(chain) - _MAX_RETRY_CHAIN)]


def turn_retry_chain(task: dict[str, Any]) -> list[dict[str, Any]]:
    """The bounded, durable copy of the task's auto-retry receipts."""
    chain = task.get('_turnRetryChain')
    if not isinstance(chain, list):
        return []
    return [dict(entry) for entry in chain if isinstance(entry, dict)]


__all__ = [
    'ATTRIBUTION_KEY',
    'append_turn_retry_receipt',
    'attach_failure_attribution',
    'build_failure_attribution',
    'ensure_failure_attribution',
    'record_active_round',
    'turn_retry_chain',
]
