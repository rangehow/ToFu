"""Shared request-cost snapshots and terminal display totals.

Entries stamp_round_costs, usage_with_round_costs and stamp_terminal_cost.
The existing lib.cost engine owns pricing. Request snapshots use request-bound
routes; terminal folding retains its explicit checkpoint/provider fallback.
Frozen native receipts never acquire Python price-lookup authority.
"""
from lib.log import get_logger

logger = get_logger(__name__)


def stamp_round_costs(task, api_rounds, *, model):
    """Snapshot request-local prices before checkpoint or terminal projection.

    No pricing at replay/projection time, and no repricing mixed-model aggregate
    usage under the terminal model. Missing usage stays unpriced. Compaction
    detail can be truncated; the Flow receipt reports that coverage separately.

    A round priced here never changes again: it is replaced in ``api_rounds``
    by its settled twin, which Turn projection capture shares by reference.
    """
    if task.get('_nativeFrozenCostProjection') is True:
        return

    from lib.conversation_sync.settled_rows import is_settled, settle_or_none
    from lib.cost import compute_cost
    from lib.pricing import declared_pricing
    from lib.request_quotes import merge_usage_observations, receipt_identity
    from math import isfinite

    groups = {}
    for index, row in enumerate(api_rounds):
        identity = receipt_identity(row) if isinstance(row, dict) else None
        if identity is not None:
            groups.setdefault(identity, []).append(index)
    joined = {}
    skip = set()
    for indices in groups.values():
        if len(indices) == 1:
            continue
        if any('cost' in api_rounds[i] for i in indices):
            skip.update(indices)
            continue
        first, *copies = indices
        usage, conflicts = {}, set()
        routes = set()
        dispatch = None
        for i in indices:
            copy = api_rounds[i]
            source = copy.get('usage')
            if isinstance(source, dict):
                merge_usage_observations(usage, source, conflicts)
                if source.get('_dispatch'):
                    if dispatch is not None and dispatch != source['_dispatch']:
                        conflicts.add(('_dispatch',))
                    dispatch = source['_dispatch']
            routes.update((k, copy[k]) for k in ('model', 'provider_id', 'providerId')
                          if isinstance(copy.get(k), str) and copy[k])
        skip.update(copies)
        if conflicts or any(sum(key == k for key, _ in routes) > 1 for k, _ in routes):
            skip.add(first)
        else:
            joined[first] = (usage | ({'_dispatch': dispatch} if dispatch is not None else {}), dict(routes))

    for index, row in enumerate(api_rounds):
        if index in skip or not isinstance(row, dict) or 'cost' in row:
            continue
        usage, route = joined.get(index, (row.get('usage'), {}))
        route = {**row, **route}
        if not isinstance(usage, dict) or not any(
            not isinstance(usage.get(key), bool)
            and isinstance(usage.get(key), (int, float))
            and usage[key] >= 0
            and (not isinstance(usage[key], float) or isfinite(usage[key]))
            for key in ('prompt_tokens', 'input_tokens', 'completion_tokens',
                        'output_tokens', 'cache_read_input_tokens',
                        'cache_read_tokens', 'cache_creation_input_tokens',
                        'cache_write_tokens')
        ):
            continue
        try:
            dispatch = usage.get('_dispatch')
            dispatch = dispatch if isinstance(dispatch, dict) else {}
            started_ns = dispatch.get('attempt_started_at_unix_ns')
            estimated = started_ns is None and dispatch.get('stream_started_at_unix_ns') is not None
            if estimated:
                started_ns = dispatch['stream_started_at_unix_ns']
            request_ms = (started_ns // 1_000_000 if type(started_ns) is int
                          and 0 < started_ns <= 2**63 - 1 else None)
            at = request_ms / 1000 if request_ms is not None else None
            offering = dispatch.get('offering_actual_pricing')
            pricing = declared_pricing(offering) if offering is not None else None
            cost = compute_cost(
                usage, model_id=(dispatch.get('logical_model') or route.get('model')
                                 or dispatch.get('model') or model or ''),
                provider_id=(dispatch.get('provider_id') or route.get('provider_id')
                             or route.get('providerId') or task.get('provider_id') or None),
                at=at, pricing_override=pricing,
                unrounded_cny_components=True,
            )
            if cost is not None:
                facts = {'cost': cost}
                missing = [field for field, value in (('request_clock', request_ms),
                           ('executed_offering_price', pricing)) if value is None]
                if estimated:
                    missing.append('request_clock_estimated')
                if missing:
                    facts['pricingEvidence'] = missing
                if request_ms is not None:
                    facts['requestTimestampMs'] = request_ms
                if is_settled(row):
                    row = {**row, **facts}
                else:
                    row.update(facts)
                api_rounds[index] = settle_or_none(row) or row
        except Exception as exc:
            # Cost display is best-effort, never a reason to lose paid usage or
            # prevent child terminal delivery. Unpriced rows remain explicit.
            logger.warning('[Cost] child round snapshot failed: %s', exc)


def usage_with_round_costs(usage, api_rounds):
    """Fold request-local prices, never reprice cumulative restored tokens."""
    from math import isfinite
    result = dict(usage or {})
    priced = []
    for row in api_rounds:
        if not isinstance(row, dict):
            continue
        reported = (row.get('usage') or {}).get('cost_usd')
        cost = reported if reported is not None else (row.get('cost') or {}).get('costUsd')
        if type(cost) in (int, float) and isfinite(cost) and cost >= 0:
            priced.append(cost)
    if priced:
        result['cost_usd'] = sum(priced)
    return result


def stamp_terminal_cost(task, done_event):
    """Annotate display costs without publishing or settling a financial account."""
    try:
        from lib.cost import compute_cost as _compute_cost
        from lib.request_quotes import fold_stored_quotes
        rounds = done_event.get('apiRounds') or []
        model = done_event.get('model') or task.get('model') or ''
        stamp_round_costs(task, rounds, model=model)
        _msg_cost = fold_stored_quotes(rounds, usage=done_event.get('usage'), model=model, provider=task.get('provider_id'))
        if not rounds and _msg_cost is None and done_event.get('usage') and task.get('_nativeFrozenCostProjection') is not True:
            _msg_cost = _compute_cost(
                done_event['usage'],
                model_id=done_event.get('model') or task.get('model') or '',
                provider_id=task.get('provider_id') or None,
            )
        if _msg_cost:
            done_event['cost'] = _msg_cost
    except Exception as _ce:
        logger.warning('[Cost] done-event stamp failed (non-fatal): %s', _ce)

