"""Read-only portfolio views: the ranked registry, the ledger, the digest section.

The promised -> delivered ledger is derived from the durable records
(initiative.promise vs outcome), never from activity counts: each unit row
reports how many initiatives started, how many delivered and their measured
value against what was promised, and how many ended as an explicit negative
result. The owner sees value, not work done. The digest names every start with
why it started (its rank, the authority it applied, the resources measured
available), what it has cost so far (its Project Router request's usage
account) and its expected outcome, and every proposal a review deferred with
the measured reason.

Entry points: portfolio_view (router_portfolio list), ledger, portfolio_digest
(the digest's portfolio section), initiative_usage, counts.
"""
from __future__ import annotations

from datetime import datetime

from .ranking import VALUE_UNITS, rank, start_blockers
from .registry import TERMINAL, RouterPortfolio


def ledger(owner: int, *, client=None) -> list[dict]:
    """Promised and delivered value per unit over every started initiative."""
    registry = RouterPortfolio(owner, client=client)
    opportunities = registry.open()
    closed = []
    cursor = None
    while True:
        page = registry.page_closed(cursor=cursor)
        closed.extend(page['closed'])
        cursor = page['closed_cursor']
        if cursor is None:
            break
    return _ledger(opportunities + closed)


def _ledger(opportunities: list[dict]) -> list[dict]:
    rows = []
    for unit in VALUE_UNITS:
        started = [value for value in opportunities if value['initiative'] is not None
                   and value['initiative']['promise']['unit'] == unit]
        if not started:
            continue
        delivered = [value for value in started if value['state'] == 'delivered']
        negative = [value for value in started if value['state'] == 'negative']
        rows.append({'unit': unit, 'initiatives': len(started),
                     'running': sum(1 for value in started if value['state'] == 'started'),
                     'delivered': len(delivered), 'negative': len(negative),
                     'promised': sum(value['initiative']['promise']['amount']
                                     * value['initiative']['promise']['probability'] for value in started),
                     'measured': sum(value['outcome']['amount'] or 0.0 for value in delivered)})
    return rows


def counts(opportunities: list[dict]) -> dict:
    result = {state: sum(1 for value in opportunities if value['state'] == state)
              for state in ('proposed', 'started') + TERMINAL}
    result['awaitingOwner'] = sum(1 for value in opportunities
                                  if value['state'] == 'proposed' and value['approval']['required']
                                  and not value['approval']['grantedTurnId'])
    return result


def initiative_usage(owner: int, opportunity: dict) -> dict | None:
    """Usage so far of the Project Router request an initiative opened, or None.

    None while the forward waits in the Project Router's queue (no account yet)
    or when the initiative opened no request of its own.
    """
    request = (opportunity['initiative'] or {}).get('request')
    if request is None:
        return None
    from lib.conversations.coordinator_budget import request_usage
    return request_usage(owner, opportunity['projectPath'], conversation_id=request['conversationId'],
                         binding_revision=request['bindingRevision'], turn_id=request['inputTurnId'])


def portfolio_view(owner: int, arguments: dict, *, client=None) -> dict:
    """router_portfolio(operation=list): ranked open opportunities, a page of closed
    ones, the ledger and the owner's review switch."""
    from .review import review_state
    from ..binding import global_router
    from ..instructions import RouterInstructions
    registry = RouterPortfolio(owner, client=client)
    opportunities = registry.open()
    page = registry.page_closed(cursor=arguments.get('cursor'))
    ranked = rank(opportunities)
    router = global_router(owner, create=False, client=client)
    instructions = RouterInstructions(owner, client=client)
    active = [value for value in opportunities if value['state'] == 'started']
    open_rows = []
    for position, entry in enumerate(ranked):
        opportunity = entry['opportunity']
        instruction = None
        if opportunity['instructionId'] is not None:
            try:
                instruction = instructions.read(opportunity['instructionId'])
            except ValueError:
                instruction = None
        if router['availability'] == 'available':
            blockers = start_blockers(opportunity, instruction=instruction,
                                      router_conversation_id=router['conversationId'],
                                      dependency_state=registry.state_of, active=active,
                                      ahead=[row['opportunity'] for row in ranked[:position]])
        else:
            blockers = ['router_unavailable']
        open_rows.append({'opportunity': opportunity, 'score': entry['score'],
                          'valueShare': entry['valueShare'],
                          'dominantResourceShare': entry['dominantResourceShare'],
                          'startBlockers': blockers, 'deferral': opportunity['deferral']})
    return {'open': open_rows, 'running': active, 'closed': page['closed'],
            'closed_cursor': page['closed_cursor'], 'ledger': _ledger(opportunities + page['closed']),
            'review': review_state(owner), 'counts': counts(opportunities + page['closed'])}


def _at(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000).strftime('%m-%d %H:%M')


def _why(opportunity: dict) -> str:
    admission = opportunity['initiative']['admission']
    authority = admission['authority']
    basis = (f'rank {admission["rank"]}/{admission["ranked"]} by expected value per unit cost'
             + (f' (score {admission["score"]:.3g})' if admission['score'] is not None else ' (no measured cost)'))
    authorized = (f'your standing instruction {authority["id"]}' if authority['kind'] == 'instruction'
                  else f'your approval (Turn {authority["id"]})')
    resources = admission['resources']
    measured = f'{resources["model"]} route available' + (
        f', {resources["gpuQueue"]} had the GPUs' if resources.get('gpuQueue') else '')
    return f'{basis}; authorized by {authorized}; {measured}'


def _cost(usage: dict | None, opportunity: dict) -> str:
    estimate = opportunity['validation']['cost']
    estimated = (f'estimated ¥{estimate["modelCny"]:g} model, {estimate["gpuHours"]:g} GPU h, '
                 f'{estimate["wallHours"]:g} h')
    if usage is None:
        if opportunity['initiative']['disposition'] == 'queued':
            return f'queued behind the Project Router\u2019s current request, no usage yet ({estimated})'
        return f'usage not recorded yet ({estimated})'
    spent = f'{usage["tokens"]:,} tokens over {usage["modelRequests"]} model requests'
    if not usage['usageComplete']:
        spent += ' (at least; a receipt is still open)'
    return f'{spent} ({estimated})'


def portfolio_digest(owner: int, *, client=None, since: str | None = None,
                     usage=initiative_usage) -> tuple[str | None, dict]:
    """One digest section: owner decisions, starts with why and cost, value, deferrals.

    ``since`` is the previous digest run (ISO), so a start or a closed
    initiative is reported once. Nothing is sent when there is nothing to say.
    """
    cutoff = 0
    if since:
        try:
            cutoff = int(datetime.fromisoformat(since).timestamp() * 1000)
        except (TypeError, ValueError):
            cutoff = 0
    view = portfolio_view(owner, {}, client=client)
    lines = []
    awaiting = [row for row in view['open'] if 'owner_approval' in row['startBlockers']]
    if awaiting:
        lines.append(f'**{len(awaiting)} opportunity(ies) waiting for your approval:**')
        for row in awaiting:
            opportunity = row['opportunity']
            value = opportunity['value']
            lines.append(f'- {opportunity["title"]} — expected {value["amount"] * value["probability"]:g} '
                         f'{value["unit"]}; validation {opportunity["validation"]["cost"]["modelCny"]:g} CNY model, '
                         f'{opportunity["validation"]["cost"]["gpuHours"]:g} GPU h, '
                         f'{opportunity["validation"]["cost"]["wallHours"]:g} h')
            lines.append(f'  Why you: {opportunity["approval"]["reason"]}')
    closed = [value for value in view['closed'] if value['outcome'] is not None
              and value['outcome']['closedAt'] >= cutoff]
    delivered = [value for value in closed if value['state'] == 'delivered']
    negative = [value for value in closed if value['state'] == 'negative']
    started = [value for value in view['running'] if value['initiative']['startedAt'] >= cutoff]
    if started:
        lines.append('**Initiatives started:**')
        for value in started:
            promise = value['initiative']['promise']
            lines.append(f'- {value["title"]} ({value["projectPath"]})')
            lines.append(f'  Expected: {promise["amount"]:g} {promise["unit"]} (p={promise["probability"]:g}), '
                         f'measured by {_at(promise["dueAt"])}')
            lines.append(f'  Why: {_why(value)}')
            lines.append(f'  Cost so far: {_cost(usage(owner, value), value)}')
    if delivered or negative:
        lines.append('**Value delivered:**')
        for value in delivered:
            promise = value['initiative']['promise']
            lines.append(f'- {value["title"]}: measured {value["outcome"]["amount"]:g} {promise["unit"]} '
                         f'(promised {promise["amount"]:g})')
        for value in negative:
            lines.append(f'- Negative result (closed): {value["title"]} — {value["outcome"]["reason"]}')
    deferred = [row for row in view['open'] if row['deferral'] is not None
                and row['deferral']['observedAt'] >= cutoff]
    if deferred:
        lines.append('**Authorized but not started (measured):**')
        for row in deferred:
            deferral = row['deferral']
            retry = f'; the provider named {_at(deferral["retryAt"])}' if deferral['retryAt'] else ''
            lines.append(f'- {row["opportunity"]["title"]}: {deferral["reason"]} — {deferral["detail"]}{retry}')
    if not lines:
        return None, {}
    return '\n'.join(lines), {'awaiting_owner': len(awaiting), 'started': len(started),
                              'delivered': len(delivered), 'negative': len(negative),
                              'running': len(view['running']), 'deferred': len(deferred)}


__all__ = ['counts', 'initiative_usage', 'ledger', 'portfolio_digest', 'portfolio_view']
