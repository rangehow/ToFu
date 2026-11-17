"""Conserved physical occurrence receipts shared by dispatch and its source.

ExecutionBudget alone creates the events. This reducer records each occurrence
exactly once and never refuses work for spend: Coordinator requests have no
token, model-request or tool-call ceiling. A missing provider receipt is kept
as a lower bound (usage_complete=False) and never blocks the next request.
"""
from copy import deepcopy
import hashlib
import json

from lib.llm.llm_errors import AbortedError

# Terminal tool phases (contracts/project_dispatch_v1.schema.json
# terminalToolPhases) that record an unknown effect rather than an executor
# outcome: a continuation settled an earlier attempt's open intent, or the
# Worker acknowledged it through inspect_tool_effect. A later real outcome of
# the same occurrence still replaces either one.
INTERRUPTED_TOOL_PHASE = 'interrupted_effect_unknown'
ACKNOWLEDGED_TOOL_PHASE = 'effect_unknown_acknowledged'
UNKNOWN_TOOL_PHASES = frozenset({INTERRUPTED_TOOL_PHASE, ACKNOWLEDGED_TOOL_PHASE})


def record_model_event(attempts, event, *, live_intents=(), prior_accounts=False):
    key = (event['root_task_id'], event['attempt_ordinal'])
    old = next((row for row in attempts if (row['root_task_id'], row['attempt_ordinal']) == key), None)
    if event['phase'] == 'dispatch_intent':
        if old is not None:
            if old != event:
                raise AbortedError('Physical model occurrence is terminal or belongs to another account')
            return
        same = [row for row in attempts if row['root_task_id'] == event['root_task_id']]
        if (not prior_accounts and len(same) != len(attempts)
                or any(row['account_nonce'] != event['account_nonce'] for row in same)):
            raise AbortedError('Physical model occurrence belongs to another account')
        attempts.append(deepcopy(event))
    elif old is not None:
        if any(old.get(key) != event.get(key) for key in (
                'account_nonce', 'root_task_id', 'attempt_ordinal', 'attempt_task_id', 'reserved_tokens')):
            raise AbortedError('Model settlement belongs to another immutable occurrence')
        if event['phase'] == 'voided' and old['phase'] == 'dispatch_intent':
            raise AbortedError('Cannot void a durable uncertain physical intent')
        if old['phase'] in ('settled', 'voided') and old != event:
            raise AbortedError('Physical model occurrence is already terminal')
        old.clear(); old.update(deepcopy(event))
    elif event['phase'] != 'voided':
        raise AbortedError('Cannot settle an unrecorded model occurrence')


def model_consumption(attempts):
    """Observed tokens and whether every receipt is confirmed; never a refusal."""
    observed = sum(row['reported_tokens'] for row in attempts)
    return observed, all(row['phase'] in ('settled', 'voided') and row['usage_complete'] for row in attempts)


def _retained():
    from .repository import CONTRACT
    return CONTRACT['delegationDefaults']['retainedSettledReceipts']


def fold_settled_models(holder):
    """Fold old terminal model receipts into a counter; ordinals stay monotonic.

    Unbounded requests would otherwise grow the record (and every CAS rewrite)
    with each call. A per-root watermark keeps replayed settlements idempotent:
    an ordinal at or below it is already counted and is ignored, not re-added.
    """
    attempts = holder['modelAttempts']
    terminal = [row for row in attempts if row['phase'] in ('settled', 'voided')]
    excess = len(terminal) - _retained()
    if excess <= 0:
        return
    folded = sorted(terminal, key=lambda row: row['attempt_ordinal'])[:excess]
    summary = holder.setdefault('settledModelUsage', {'requests': 0, 'tokens': 0, 'complete': True, 'watermarks': {}})
    for row in folded:
        summary['requests'] += 1
        summary['tokens'] += row['reported_tokens']
        summary['complete'] = summary['complete'] and row['usage_complete']
        mark = summary['watermarks'].get(row['root_task_id'], 0)
        summary['watermarks'][row['root_task_id']] = max(mark, row['attempt_ordinal'])
        attempts.remove(row)


def folded_model_event(holder, event):
    """True when an occurrence was already folded; replays are acknowledged as no-ops.

    A retained row is never folded, even below the watermark: an older open
    intent must still accept its own settlement.
    """
    if any((row['root_task_id'], row['attempt_ordinal']) == (event['root_task_id'], event['attempt_ordinal'])
           for row in holder['modelAttempts']):
        return False
    summary = holder.get('settledModelUsage') or {}
    return event['attempt_ordinal'] <= summary.get('watermarks', {}).get(event['root_task_id'], 0)


def fold_settled_tools(holder, terminal_phases):
    attempts = holder.get('toolAttempts', [])
    terminal = [row for row in attempts if row['phase'] in terminal_phases]
    excess = len(terminal) - _retained()
    if excess <= 0:
        return
    # Calls settle out of ordinal order (a long run_command beside a swarm's quick
    # calls), and a watermark folds every ordinal at or below it: it must never
    # pass an open row of its root, or that row's completion reads as already
    # folded and its intent fences the Worker for good. Rows above an open one
    # wait until it settles.
    open_floor = {}
    for row in attempts:
        if row['phase'] not in terminal_phases:
            root = row['root_task_id']
            open_floor[root] = min(open_floor.get(root, row['tool_ordinal']), row['tool_ordinal'])
    foldable = [row for row in terminal
                if row['tool_ordinal'] < open_floor.get(row['root_task_id'], float('inf'))]
    folded = sorted(foldable, key=lambda row: row['tool_ordinal'])[:excess]
    summary = holder.setdefault('settledToolUsage', {'calls': 0, 'watermarks': {}})
    for row in folded:
        summary['calls'] += 1
        mark = summary['watermarks'].get(row['root_task_id'], 0)
        summary['watermarks'][row['root_task_id']] = max(mark, row['tool_ordinal'])
        attempts.remove(row)


def folded_tool_event(holder, event):
    summary = holder.get('settledToolUsage') or {}
    return event['tool_ordinal'] <= summary.get('watermarks', {}).get(event['root_task_id'], 0)


def tool_operation_id(row):
    """The inspect_tool_effect operation ID of one immutable ledger occurrence."""
    identity = json.dumps([row['root_task_id'], row['tool_ordinal'], row['tool_call_id'],
                           row['account_nonce']], separators=(',', ':'))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]


def unsettled_tool_effect(row, terminal_phases):
    """An open occurrence of a tool whose repeat is not safe by declaration.

    Read-only and idempotent tools never hold a result or delivery open: their
    open intent changed nothing a repeat would not change again. A phase this
    code does not know is never read as settled, whatever the tool.
    """
    if row['phase'] in terminal_phases:
        return False
    if row['phase'] != 'intent':
        return True
    from lib.tools.registry import declared_effect_recovery
    return declared_effect_recovery(row['tool_name']) == 'reconcile'


def holder_consumption(holder):
    """Tokens, model requests and receipt completeness including folded receipts."""
    tokens, complete = model_consumption(holder['modelAttempts'])
    summary = holder.get('settledModelUsage') or {'requests': 0, 'tokens': 0, 'complete': True}
    return tokens + summary['tokens'], len(holder['modelAttempts']) + summary['requests'], complete and summary['complete']
