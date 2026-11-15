"""The durable portfolio review (docs/ROUTER.md R10, portfolio).

One owner-visible scheduler task of type ``router_portfolio_review``, installed
like the proactive digest. It is on unless the owner paused it in the main
coordinator's header (a keyed ``scheduler.task.ensure`` row keeps his choice):
the owner asked the Global Router to iterate his projects proactively, and his
standing authority covers work that needs no new approval. Each run is
deterministic: no model call, no polling of conversations, no per-minute loop.

A run
1. applies the kill conditions: proposals past their value deadline close as
   dropped; a started initiative with no measured outcome by its promised due
   time closes as an explicit negative result (no open-ended initiative);
2. retries a start whose acceptance was not confirmed (the same forward command
   id, so the Project Router still receives it at most once);
3. ranks the proposals by expected value per unit of cost and, in that order,
   starts every one that (a) passes the authority gate, (b) is neither running
   nor a duplicate of open work, (c) does not conflict with an active
   initiative of the same project, and (d) whose resources are measured
   available now (``capacity``). There is no count of starts: what limits a
   review is authority, conflicts and measured capacity, each recorded.

Starting applies the opportunity's standing instruction through the same
owner-checked path as route_to_project (R4): the Project Router receives the
owner's frozen text as authority and the opportunity as nonhuman evidence, in
one request that queues behind a busy Project Router's live request rather than
steering into it, so each initiative has its own usage account. An opportunity
approved by the owner in his own message starts as a forward of that message
(R2). The promised outcome and the reason it started (rank, authority, measured
resources) are recorded before the forward is sent. A start the Project Router
does not accept (the executor could not launch, Plan mode, a missing
conversation) is not an initiative: it returns to proposed with that reason.
An opportunity needing approval never starts automatically, nor does anything
inside quiet hours or while the owner paused the review.

Entry points: run_portfolio_review (scheduler handler), start_opportunity (the
shared start guard), StartBlocked, review_state, configure_review,
ensure_review, portfolio_principal, REVIEW_TASK_TYPE.
Dependencies: portfolio.registry/ranking/capacity, router.forwarding,
router.proactive.held (quiet hours), scheduler.manager.
"""
from __future__ import annotations

import json

from lib.identity import PrincipalContext, require_user_id
from lib.log import get_logger

from ..contract import CONTRACT
from .ranking import rank, start_blockers
from .registry import RouterPortfolio, digest

logger = get_logger(__name__)

REVIEW_TASK_TYPE = 'router_portfolio_review'
REVIEW_SYSTEM_KEY = 'router-portfolio-review'
DEFAULT_REVIEW_SCHEDULE = CONTRACT['$defs']['portfolioReview']['properties']['schedule']['default']
# The one definition of the built-in row: the scheduler's boot install, the
# registry's first proposal and the owner's switch all write this same task.
REVIEW_TASK = dict(
    system_key=REVIEW_SYSTEM_KEY, name='Portfolio review', schedule=DEFAULT_REVIEW_SCHEDULE,
    command=('Deterministic portfolio review (docs/ROUTER.md R10): closes killed or expired '
             'initiatives and starts every opportunity the owner already authorized whose '
             'resources are available.'),
    task_type=REVIEW_TASK_TYPE,
    description=('Reviews the opportunity registry and starts owner-authorized initiatives whose '
                 'resources are available; no model request. Pausing stops starts, not started work.'),
    max_runtime=300, reconcile_enabled=False)


class StartBlocked(PermissionError):
    """A structural reason (authority, dependency, duplicate, conflict) a proposal waits."""

    def __init__(self, opportunity_id: str, blockers: list[str]):
        super().__init__(f'Opportunity {opportunity_id} cannot start: {", ".join(blockers)}')
        self.blockers = blockers


def portfolio_principal(owner: int, tenant_id: str | None = None) -> PrincipalContext:
    """The owner's least-privilege scheduling identity for the portfolio review."""
    return PrincipalContext.user(subject_id=f'router-portfolio:user:{owner}', owner_user_id=owner,
                                 tenant_id=tenant_id, scopes={'agents:scheduler'})


def _scheduler():
    from lib.scheduler.manager import get_scheduler
    return get_scheduler()


def _definition(owner: int, *, enabled: bool, principal: PrincipalContext) -> dict:
    return dict(principal=principal, enabled=enabled, **REVIEW_TASK)


def ensure_review(owner: int) -> dict:
    """Install the built-in review (on) without changing the owner's pause."""
    _scheduler().ensure_owner_task(**_definition(owner, enabled=True, principal=portfolio_principal(owner)))
    return review_state(owner)


def review_state(owner: int) -> dict:
    """The owner's switch. A review never installed is on: only the owner pauses it."""
    task_id = f'system-{require_user_id(owner, context="portfolio review")}-{REVIEW_SYSTEM_KEY}'
    task = _scheduler().get_task(task_id, user_id=owner)
    if task is None or task.get('system_key') != REVIEW_SYSTEM_KEY:
        return {'enabled': True, 'schedule': DEFAULT_REVIEW_SCHEDULE, 'taskId': None,
                'lastRun': None, 'lastResult': None}
    return {'enabled': bool(task['enabled']), 'schedule': task['schedule'], 'taskId': task['id'],
            'lastRun': task.get('last_run') or None, 'lastResult': task.get('last_result') or None}


def configure_review(owner: int, request: dict, *, principal: PrincipalContext) -> dict:
    """The owner's switch: pause automatic starts, or resume them."""
    from ..contract import validate
    request = validate(dict(request), 'portfolioReviewRequest')
    manager = _scheduler()
    current = review_state(owner)
    if current['taskId'] is None:
        # First use: the built-in definition is created with the owner's choice.
        manager.ensure_owner_task(**_definition(owner, enabled=request['enabled'], principal=principal))
    else:
        # Later changes are target-state writes on that same row: reconcile_enabled
        # stays False so a re-install never re-enables a review the owner paused.
        manager.update_task(current['taskId'], user_id=owner, enabled=request['enabled'])
    return review_state(owner)


def _instruction(owner: int, opportunity: dict, *, client=None):
    if opportunity['instructionId'] is None:
        return None
    from ..instructions import RouterInstructions
    try:
        return RouterInstructions(owner, client=client).read(opportunity['instructionId'])
    except ValueError:
        return None


def _start_note(opportunity: dict) -> str:
    initiative = opportunity['initiative']
    promise, admission = initiative['promise'], initiative['admission']
    return json.dumps({
        'initiative': 'This opportunity was started by the owner-authorized portfolio review '
                      '(docs/ROUTER.md R10); the owner request above is the authority and this note is evidence.',
        'opportunityId': opportunity['opportunityId'],
        'problem': opportunity['problem'],
        'evidence': opportunity['evidence'],
        'whyNow': {'rank': f'{admission["rank"]} of {admission["ranked"]} by expected value per unit of cost',
                   'resources': admission['resources']},
        'cheapestValidation': {'step': opportunity['validation']['step'],
                               'expectedCost': opportunity['validation']['cost']},
        'expectedOutcome': {'amount': promise['amount'], 'unit': promise['unit'],
                            'probability': promise['probability'], 'dueAtMs': promise['dueAt']},
        'killCondition': opportunity['killCondition'],
        'settle': 'When the validation is measured, report the measured amount and the kill-condition '
                  'metric with evidence paths. The Global Router records the outcome with '
                  'router_portfolio(operation=settle, opportunity_id=...). Do not expand scope: a new '
                  'external or irreversible action, a quality or cost trade-off or a production restart '
                  'still needs the owner.',
    }, ensure_ascii=False, indent=2)


def _send(registry: RouterPortfolio, router: dict, opportunity: dict, dispatch, now_ms) -> dict:
    """Forward one begun start; record acceptance, or release it when nothing runs."""
    from ..forwarding import ForwardNotAccepted, _router_turn, apply_standing_instruction, deliver_forward
    from ..instructions import authored_text
    from ...turn_initiation import require_human_turn
    owner, opportunity_id = registry.owner, opportunity['opportunityId']
    trigger = opportunity['initiative']['trigger']
    approval = opportunity['approval']
    try:
        if approval['grantedTurnId']:
            source = _router_turn(owner, router['conversationId'], approval['grantedTurnId'])
            require_human_turn(source)
            # The command id names this opportunity's start attempt: one owner
            # message may approve several opportunities, each its own forward.
            result = deliver_forward(
                owner, opportunity['projectPath'], authored_text(source),
                {'conversationId': router['conversationId'], 'turnId': approval['grantedTurnId']},
                command_id='portfolio-forward:' + digest([owner, trigger]), note=_start_note(opportunity),
                trigger={}, router_id=router['conversationId'], instruction_id=None, config={},
                dispatch=dispatch, inject_mode='queue')
        else:
            result = apply_standing_instruction(
                owner, router['conversationId'], opportunity['projectPath'], opportunity['instructionId'],
                {'event_id': trigger}, _start_note(opportunity), dispatch=dispatch, inject_mode='queue')
    except ForwardNotAccepted as refusal:
        released = registry.release(opportunity_id, deferral={
            'reason': 'start_failed', 'detail': str(refusal)[:2000], 'observedAt': now_ms(),
            'retryAt': None, 'observed': {'disposition': refusal.disposition, 'trigger': trigger}})
        return {'opportunity': released, 'dispatch': None, 'deferral': released['deferral']}
    request = None
    if result['input_turn_id']:
        request = {'conversationId': result['coordinator_conversation_id'],
                   'bindingRevision': result['binding_revision'], 'inputTurnId': result['input_turn_id']}
    return {'opportunity': registry.dispatched(opportunity_id, command_id=result['command_id'],
                                              disposition=result['disposition'], request=request),
            'dispatch': result, 'deferral': None}


def start_opportunity(owner: int, opportunity_id: str, *, started_by: str, review_id: str | None,
                      ranking: list[dict] | None = None, dispatch=None, client=None, clock=None,
                      capacity=None) -> dict:
    """Start one opportunity, or report why it does not start now.

    The one guard for the scheduled review and the Router's manual start.
    ``ranking`` is the review's ranked snapshot (a manual start ranks the
    current registry). Raises ``StartBlocked`` for a structural reason; returns
    ``{'opportunity', 'dispatch', 'deferral'}`` where ``deferral`` is the
    recorded resource or acceptance fact when it did not start. An exception
    while sending leaves the start unconfirmed; the next review repeats it with
    the same command id (the owner text is delivered at most once).
    """
    from datetime import datetime
    from ..binding import global_router
    from .capacity import resource_deferral
    registry = RouterPortfolio(owner, client=client, clock=clock)
    opportunity = registry.read(opportunity_id)
    router = global_router(owner, create=False, client=client)
    if router['availability'] != 'available':
        raise StartBlocked(opportunity_id, ['router_unavailable'])

    def now_ms() -> int:
        return int((clock or datetime.now)().timestamp() * 1000)
    if opportunity['state'] == 'started':
        if opportunity['initiative']['disposition'] is not None:
            raise ValueError(f'Opportunity {opportunity_id} is already dispatched; settle it instead')
        return _send(registry, router, opportunity, dispatch, now_ms)
    if opportunity['state'] != 'proposed':
        raise ValueError(f'Opportunity {opportunity_id} is {opportunity["state"]}; closed work is never restarted')
    current = registry.open()
    ranked = ranking if ranking is not None else rank(current)
    position = next((index for index, entry in enumerate(ranked)
                     if entry['opportunity']['opportunityId'] == opportunity_id), None)
    if position is None:
        raise ValueError(f'Opportunity {opportunity_id} is not a ranked proposal')
    blockers = start_blockers(opportunity, instruction=_instruction(owner, opportunity, client=client),
                              router_conversation_id=router['conversationId'],
                              dependency_state=registry.state_of,
                              active=[value for value in current if value['state'] == 'started'],
                              ahead=[entry['opportunity'] for entry in ranked[:position]])
    if blockers:
        raise StartBlocked(opportunity_id, blockers)
    held = opportunity['deferral']
    if held is not None and held['retryAt'] is not None and held['retryAt'] > now_ms():
        # The provider named when its capacity returns; asking earlier measures nothing new.
        return {'opportunity': opportunity, 'dispatch': None, 'deferral': held}
    deferral, resources = resource_deferral(owner, opportunity, ports=capacity)
    if deferral is not None:
        return {'opportunity': registry.defer(opportunity_id, deferral=deferral), 'dispatch': None,
                'deferral': deferral}
    entry = ranked[position]
    authority = ({'kind': 'approval', 'id': opportunity['approval']['grantedTurnId']}
                 if opportunity['approval']['grantedTurnId']
                 else {'kind': 'instruction', 'id': opportunity['instructionId']})
    admission = {'rank': position + 1, 'ranked': len(ranked), 'score': entry['score'],
                 'valueShare': entry['valueShare'], 'dominantResourceShare': entry['dominantResourceShare'],
                 'authority': authority, 'resources': resources}
    opportunity = registry.begin(opportunity_id, started_by=started_by, review_id=review_id, admission=admission)
    return _send(registry, router, opportunity, dispatch, now_ms)


def run_portfolio_review(task: dict, *, client=None, clock=None, dispatch=None, capacity=None) -> tuple[bool, str]:
    """Scheduler handler of ``router_portfolio_review`` rows; returns (ok, result)."""
    from ..proactive.held import quiet_hours_active
    from datetime import datetime
    owner = require_user_id(task.get('user_id'), context='portfolio review')
    registry = RouterPortfolio(owner, client=client, clock=clock)
    closed = registry.close_expired()
    if quiet_hours_active((clock or datetime.now)()):
        return True, f'Quiet hours: no initiative starts. Closed {len(closed)} expired initiative(s).'
    review_id = str(task.get('id') or 'portfolio-review')
    options = dict(review_id=review_id, dispatch=dispatch, client=client, clock=clock, capacity=capacity)
    started, retried, deferred, waiting = [], [], [], []
    for value in registry.open():
        if value['state'] == 'started' and value['initiative']['disposition'] is None:
            try:
                start_opportunity(owner, value['opportunityId'], started_by=value['initiative']['startedBy'],
                                  **options)
                retried.append(value['opportunityId'])
            except Exception as error:
                logger.warning('[Portfolio] retry of %s failed: %s', value['opportunityId'], error)
    ranked = rank(registry.open())
    for entry in ranked:
        opportunity = entry['opportunity']
        row = {'opportunityId': opportunity['opportunityId'], 'title': opportunity['title']}
        try:
            result = start_opportunity(owner, opportunity['opportunityId'], started_by='review', ranking=ranked,
                                       **options)
        except StartBlocked as blocked:
            waiting.append({**row, 'blockers': blocked.blockers})
            continue
        except Exception as error:
            # An unconfirmed send stays started and is repeated by the next review.
            logger.warning('[Portfolio] start of %s failed: %s', opportunity['opportunityId'], error)
            waiting.append({**row, 'blockers': [f'start_unconfirmed:{type(error).__name__}']})
            continue
        if result['deferral'] is not None:
            deferred.append({**row, 'reason': result['deferral']['reason'],
                             'retryAt': result['deferral']['retryAt']})
            continue
        initiative = result['opportunity']['initiative']
        started.append({**row, 'projectPath': opportunity['projectPath'],
                        'expectedOutcome': initiative['promise'], 'admission': initiative['admission'],
                        'disposition': initiative['disposition']})
    summary = {'closed': [value['opportunityId'] for value in closed], 'started': started,
               'retried': retried, 'deferred': deferred, 'waiting': waiting}
    return True, json.dumps(summary, ensure_ascii=False)


__all__ = ['DEFAULT_REVIEW_SCHEDULE', 'REVIEW_SYSTEM_KEY', 'REVIEW_TASK', 'REVIEW_TASK_TYPE', 'StartBlocked',
           'configure_review', 'ensure_review', 'portfolio_principal', 'review_state',
           'run_portfolio_review', 'start_opportunity']
