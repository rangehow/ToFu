"""router_portfolio: the Global Router's tool over its opportunity registry.

List the ranked registry and ledger, propose an opportunity, approve one from
the owner's own message (a genuine human Turn, exactly like creating a standing
instruction), start a startable one now, or settle a started initiative with its
measured outcome. Every call has already passed the coordinator binding check;
only the owner's Global Router reaches this adapter.
"""
from __future__ import annotations

from ..contract import validate
from .registry import RouterPortfolio
from .review import ensure_review, start_opportunity
from .view import portfolio_view


def execute_portfolio_tool(task: dict, scope: dict, arguments: dict) -> dict:
    from ...turn_initiation import require_human_turn
    arguments = validate(dict(arguments), 'routerPortfolio')
    operation = arguments['operation']
    owner = scope['user_id']
    if operation == 'list':
        return portfolio_view(owner, arguments)
    registry = RouterPortfolio(owner)
    if operation == 'propose':
        if arguments.get('opportunity_id'):
            return registry.revise(arguments)
        if not arguments.get('idempotency_key'):
            raise ValueError('A new opportunity needs an idempotency_key')
        proposed = registry.propose(arguments)
        # The review that may start it exists wherever the registry does; it is
        # on unless the owner paused it (a paused row keeps his choice).
        ensure_review(owner)
        return proposed
    opportunity_id = arguments.get('opportunity_id')
    if not opportunity_id:
        raise ValueError('opportunity_id is required for this operation')
    if operation == 'approve':
        from ..tools import _human_source_turn
        turn = _human_source_turn(task, scope, arguments.get('source_turn_id') or '')
        require_human_turn(turn)
        return registry.approve(opportunity_id, source_turn=turn)
    if operation == 'start':
        result = start_opportunity(owner, opportunity_id, started_by='router', review_id=None)
        if result['deferral'] is not None:
            result['next_step'] = ('It did not start: ' + result['deferral']['reason'] + ' \u2014 '
                                   + result['deferral']['detail'] + ' It stays a proposal; the scheduled '
                                   'review measures again. Do not retry it in a loop.')
        else:
            result['next_step'] = ('The Project Router owns the initiative now. Follow its reports and settle '
                                   'this opportunity with the measured outcome; if nothing is measured by the '
                                   'promised due time the review closes it as a negative result.')
        return result
    if operation == 'settle':
        if arguments.get('drop_reason'):
            current = registry.read(opportunity_id)
            if current['state'] == 'proposed':
                return registry.close(opportunity_id, verdict='dropped', reason='dropped',
                                      note=arguments['drop_reason'])
            return registry.close(opportunity_id, verdict='negative', reason='no_value',
                                  note=arguments['drop_reason'])
        if not arguments.get('outcome'):
            raise ValueError('settle needs outcome {amount, evidence} or a drop_reason')
        return registry.settle(opportunity_id, arguments)
    raise ValueError('Unknown portfolio operation')


__all__ = ['execute_portfolio_tool']
