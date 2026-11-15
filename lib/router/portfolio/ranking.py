"""Deterministic opportunity ranking (docs/ROUTER.md R10, portfolio).

Expected value per unit of resource, with no exchange rate between the owner's
value units and no conversion of GPU hours into money (principle 5: a tuned
constant would be a tuning knob, not an optimization):

- within one value unit, a candidate's expected value (amount x probability)
  counts as its share of the open portfolio's expected value in that unit, so
  ¥/day, hours/week, reliability and evidence never need a common currency;
- the cheapest validation's three resources are compared as shares of the open
  portfolio's demand on each; the binding resource is the largest share
  (dominant-resource share), so the ranking asks which candidates get the most
  of what the owner cares about for the least of what is scarce;
- score = value share / dominant resource share. A candidate whose validation
  costs nothing measured ranks ahead of everything priced; ties go to the
  earlier opportunity deadline, then to the older proposal, then to its id.

Dependencies, approval and authority are not ranking inputs: ``start_blockers``
lists the structural reasons a candidate may not start yet, and the review
measures its resources (``capacity``) before it starts. The review starts every
candidate that clears both, in this order; there is no count of starts.
"""
from __future__ import annotations

RESOURCES = ('gpuHours', 'modelCny', 'wallHours')
VALUE_UNITS = ('cny_per_day', 'hours_per_week', 'failures_per_week', 'claims_settled')


def _totals(candidates: list[dict]) -> tuple[dict, dict]:
    value, cost = {unit: 0.0 for unit in VALUE_UNITS}, {resource: 0.0 for resource in RESOURCES}
    for candidate in candidates:
        value[candidate['value']['unit']] += candidate['value']['amount'] * candidate['value']['probability']
        for resource in RESOURCES:
            cost[resource] += candidate['validation']['cost'][resource]
    return value, cost


def rank(candidates: list[dict]) -> list[dict]:
    """Rank proposed candidates by expected value per unit of resource."""
    candidates = [candidate for candidate in candidates if candidate['state'] == 'proposed']
    if not candidates:
        return []
    value_totals, cost_totals = _totals(candidates)
    ranked = []
    for candidate in candidates:
        value = candidate['value']
        share = (value['amount'] * value['probability'] / value_totals[value['unit']]
                 if value_totals[value['unit']] > 0 else 0.0)
        demand = max(candidate['validation']['cost'][resource] / cost_totals[resource]
                     if cost_totals[resource] > 0 else 0.0 for resource in RESOURCES)
        ranked.append((candidate, share, demand))
    free = [(candidate, share) for candidate, share, demand in ranked if demand == 0.0]
    priced = [(candidate, share, demand) for candidate, share, demand in ranked if demand > 0.0]
    free.sort(key=lambda row: (-row[1], _tie(row[0])))
    priced.sort(key=lambda row: (-row[1] / row[2], -row[1], _tie(row[0])))
    result = []
    for candidate, share in free:
        result.append({'opportunity': candidate, 'score': None, 'valueShare': share,
                       'dominantResourceShare': 0.0})
    for candidate, share, demand in priced:
        result.append({'opportunity': candidate, 'score': share / demand, 'valueShare': share,
                       'dominantResourceShare': demand})
    return result


def _tie(candidate: dict) -> tuple:
    return (candidate['deadline'] if candidate['deadline'] is not None else float('inf'),
            candidate['createdAt'], candidate['opportunityId'])


def work_identity(opportunity: dict) -> tuple[str, str]:
    """Two records describe the same work when they name one problem in one project."""
    return opportunity['projectPath'], ' '.join(opportunity['problem'].split()).casefold()


def start_blockers(candidate: dict, *, instruction: dict | None, router_conversation_id: str,
                   dependency_state, active: list[dict], ahead: list[dict]) -> list[str]:
    """Every structural reason a proposal may not start now (R4/R9/R10).

    Empty means it may start once its resources are measured available:
    - authority: approval the owner has not given, or a missing, paused,
      cancelled, replaced or out-of-scope standing instruction;
    - an unfinished dependency (``dependency_state(id)`` is its current state);
    - a duplicate of open work: the same work identity as an ``active``
      initiative or a proposal ranked ``ahead`` of it, so a twin never starts
      while the other runs or waits (for example for the owner's approval);
    - a conflict: an ``active`` initiative of the same project. Each initiative
      is its own request in that project's Project Router; two at once would
      share one Coordinator and their measured outcomes could not be told apart.
    """
    blockers = []
    approval = candidate['approval']
    if approval['required'] and not approval['grantedTurnId']:
        blockers.append('owner_approval')
    # A start applies the cited standing instruction (R4); when the owner
    # approved this opportunity in his own message, that message is its authority
    # instead (R2), including when the instruction lapsed in the meantime.
    if approval['grantedTurnId']:
        pass
    elif candidate['instructionId'] is None:
        blockers.append('no_authority')
    elif instruction is None:
        blockers.append('unknown_authority')
    elif instruction['state'] != 'active':
        blockers.append('authority_paused' if instruction['state'] == 'paused' else 'authority_cancelled')
    elif instruction['routerConversationId'] != router_conversation_id:
        blockers.append('authority_replaced_router')
    elif instruction['projectPath'] not in (None, candidate['projectPath']):
        blockers.append('authority_other_project')
    for dependency in candidate['dependencies']:
        if dependency_state(dependency) != 'delivered':
            blockers.append(f'dependency:{dependency}')
    identity = work_identity(candidate)
    twin = next((other for other in active + ahead if other['opportunityId'] != candidate['opportunityId']
                 and work_identity(other) == identity), None)
    if twin is not None:
        blockers.append(f'duplicate:{twin["opportunityId"]}')
    running = next((other for other in active if other['projectPath'] == candidate['projectPath']), None)
    if running is not None:
        blockers.append(f'project_busy:{running["opportunityId"]}')
    return blockers


__all__ = ['rank', 'start_blockers', 'work_identity', 'VALUE_UNITS', 'RESOURCES']
