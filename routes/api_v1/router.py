"""Global Router HTTP adapters (docs/ROUTER.md), projected to /api/v4/router.

Wire shapes are owned by contracts/router_v1.schema.json. Owner identity comes
from the shared authentication boundary; every handler calls one router owner
(lib.router.binding, lib.router.instructions, ProjectCoordinationRepository,
lib.router.watchdog, the owner delegation in lib.lifecycle_approval) and maps
its typed result; ensuring the main coordinator also ensures its health checks,
which are on by default (R7). POST requires an Idempotency-Key; the operations
themselves are idempotent (deterministic provisioning, set-to-value, pause,
resume, cancel, grant, revoke). Granting the owner delegation is the one
human-UI-only write (auth.human_ui_provenance, docs/ROUTER.md R9).
"""
from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_ok, api_typed_error
from lib.log import get_logger
from lib.openapi import api_meta
from lib.router.contract import parse, query_parameters, request_body, validate
from lib.router.home import RouterGitUnavailable, RouterHomeUnavailable
from lib.storage import StorageError

from .auth import request_principal, request_user_id, require_scope

api_v1_router_bp = Blueprint('api_v1_router', __name__)
logger = get_logger(__name__)


def _owner() -> int:
    return int(request_user_id())


def _idempotency_key() -> str:
    from quart import request
    key = request.headers.get('Idempotency-Key') or ''
    if not 1 <= len(key) <= 256 or not key.strip():
        raise ValueError('Idempotency-Key is required')
    return key


# The typed failures router owners raise; anything else reaches the framework boundary.
_TYPED_FAILURES = (PermissionError, RouterHomeUnavailable, StorageError, ValueError)


def _typed_error(error: Exception):
    from lib.storage import http_status_for_storage_error
    if getattr(error, 'code', None) == 'owner_delegation_unavailable':
        return api_typed_error('storage_unavailable', status=503, detail=str(error),
                               source='api_v1.router.owner_delegation', retryable=True)
    if isinstance(error, PermissionError):
        return api_typed_error('permission', status=403, detail=str(error), source='api_v1.router')
    if isinstance(error, RouterGitUnavailable):
        return api_typed_error(
            'storage_unavailable', status=503,
            detail='Git or its filesystem is temporarily unavailable. Retry opening the main coordinator.',
            source='api_v1.router.home', retryable=True, error_code='router_git_unavailable')
    if isinstance(error, RouterHomeUnavailable):
        return api_typed_error(
            'storage_unavailable', status=503,
            detail='The main coordinator workspace could not be prepared. Check the Request ID in server logs, then retry.',
            source='api_v1.router.home', retryable=True, error_code='router_home_unavailable')
    if isinstance(error, StorageError):
        return api_error(error, status=http_status_for_storage_error(error))
    return api_bad_request(str(error))


def _router_state(owner: int, binding: dict) -> dict:
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    state = {key: binding[key] for key in ('projectPath', 'conversationId', 'revision',
                                           'availability', 'capabilities')}
    state['workerModel'] = ProjectCoordinationRepository(owner).worker_model(binding['projectPath'])
    return validate(state, 'routerState')


@api_v1_router_bp.route('/api/v1/router', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read the owner's Global Router binding without creating it", tags=['router'],
          scope='conversations')
def router_read():
    try:
        from lib.router.binding import global_router
        owner = _owner()
        return api_ok(_router_state(owner, global_router(owner, create=False)))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router', methods=['POST'])
@require_scope('conversations')
@api_meta(summary="Ensure the owner's Global Router home and conversation exist, with health checks on",
          tags=['router'], scope='conversations', request_body=request_body('ensureRequest'))
def router_ensure():
    try:
        _idempotency_key()
        parse('ensureRequest')
        from lib.router.binding import global_router
        owner = _owner()
        router = global_router(owner, create=True)
        _ensure_health_checks(owner)
        return api_ok(_router_state(owner, router))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


def _ensure_health_checks(owner: int) -> None:
    """Health checks come with the main coordinator (docs/ROUTER.md R7). They never
    keep the owner out of it: a failure is logged and the next ensure retries."""
    from lib.router.watchdog.review import ensure_health_checks
    try:
        ensure_health_checks(owner, tenant_id=request_principal().tenant_id)
    except _TYPED_FAILURES:
        logger.warning('[Router] health checks not ensured for owner %s; the next ensure retries',
                       owner, exc_info=True)


@api_v1_router_bp.route('/api/v1/router/worker-model', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read the Worker model of a bound Router (Global or Project) without changing it",
          tags=['router'], scope='conversations', parameters=query_parameters('workerModelQuery'))
def router_worker_model_read():
    from quart import request
    try:
        query = {}
        for name in request.args:
            if len(request.args.getlist(name)) != 1:
                raise ValueError('Repeated query field: ' + name)
            query[name] = request.args[name]
        validate(query, 'workerModelQuery')
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        owner = _owner()
        return api_ok(_router_state(owner, ProjectCoordinationRepository(owner).read(query['path'])))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/worker-model', methods=['POST'])
@require_scope('conversations')
@api_meta(summary="Set or clear the Worker model of a bound Router (Global or Project)", tags=['router'],
          scope='conversations', request_body=request_body('workerModelRequest'))
def router_worker_model():
    try:
        _idempotency_key()
        data = parse('workerModelRequest')
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        owner = _owner()
        result = ProjectCoordinationRepository(owner).set_worker_model(
            data['path'], data['workerModel'], tenant_id=request_principal().tenant_id)
        return api_ok(validate({key: result[key] for key in (
            'projectPath', 'conversationId', 'revision', 'availability', 'capabilities', 'workerModel')},
            'routerState'))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/session-model', methods=['POST'])
@require_scope('conversations')
@api_meta(summary="Change one owned session's model, preserving its context, grant and checkout",
          tags=['router'], scope='conversations', request_body=request_body('sessionModelRequest'))
def router_session_model():
    try:
        key = _idempotency_key()
        data = parse('sessionModelRequest')
        from lib.conversations.session_model import owner_set_session_model
        result = owner_set_session_model(_owner(), request_principal(), {
            'conversation_id': data['conversationId'], 'model': data['model'],
            'reason': data['reason'], 'idempotency_key': key})
        return api_ok(result)
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/instructions', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read a page of standing instructions, or one with its applications", tags=['router'],
          scope='conversations', parameters=query_parameters('instructionListQuery'))
def router_instructions():
    from quart import request
    try:
        query = {}
        for name in request.args:
            if len(request.args.getlist(name)) != 1:
                raise ValueError('Repeated query field: ' + name)
            query[name] = request.args[name]
        validate(query, 'instructionListQuery')
        from lib.router.instructions import RouterInstructions
        instructions = RouterInstructions(_owner())
        if query.get('instructionId'):
            return api_ok(instructions.applications(query['instructionId'], cursor=query.get('cursor')))
        return api_ok(validate(instructions.page(cursor=query.get('cursor'),
                                                 include_cancelled=query.get('includeCancelled') == 'true'),
                               'instructionPage'))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/instructions/cancel', methods=['POST'])
@require_scope('conversations')
@api_meta(summary="Cancel a standing instruction; already forwarded work keeps its authority",
          tags=['router'], scope='conversations', request_body=request_body('instructionCancelRequest'))
def router_instruction_cancel():
    try:
        _idempotency_key()
        data = parse('instructionCancelRequest')
        from lib.router.instructions import RouterInstructions
        return api_ok(RouterInstructions(_owner()).cancel(data['instructionId']))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/instructions/pause', methods=['POST'])
@require_scope('conversations')
@api_meta(summary="Pause a standing instruction; it applies again only after resume",
          tags=['router'], scope='conversations', request_body=request_body('instructionPauseRequest'))
def router_instruction_pause():
    try:
        _idempotency_key()
        data = parse('instructionPauseRequest')
        from lib.router.instructions import RouterInstructions
        return api_ok(RouterInstructions(_owner()).pause(data['instructionId']))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/instructions/resume', methods=['POST'])
@require_scope('conversations')
@api_meta(summary="Resume a paused standing instruction; a cancelled one stays cancelled",
          tags=['router'], scope='conversations', request_body=request_body('instructionResumeRequest'))
def router_instruction_resume():
    try:
        _idempotency_key()
        data = parse('instructionResumeRequest')
        from lib.router.instructions import RouterInstructions
        return api_ok(RouterInstructions(_owner()).resume(data['instructionId']))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


def _watchdog_state(owner: int, binding: dict) -> dict:
    from lib.router.watchdog.review import review_state
    state = {key: binding[key] for key in ('projectPath', 'conversationId', 'revision',
                                           'availability', 'capabilities')}
    state['review'] = review_state(owner)
    return validate(state, 'watchdogState')


@api_v1_router_bp.route('/api/v1/router/watchdog', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read the owner's Router Watchdog binding and review without creating it",
          tags=['router'], scope='conversations')
def router_watchdog_read():
    try:
        from lib.router.watchdog.binding import router_watchdog
        owner = _owner()
        return api_ok(_watchdog_state(owner, router_watchdog(owner, create=False)))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/office', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read the owner's office facts; streamId and seq order the office.fact push frames after them",
          tags=['router'], scope='conversations')
def router_office_read():
    try:
        from lib.router.office_facts import OfficeFactsClosed, snapshot
        try:
            return api_ok(validate(snapshot(_owner()), 'officeFacts'))
        except OfficeFactsClosed as error:
            return api_error(str(error), status=503, code='office_facts_stopping')
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/watchdog', methods=['POST'])
@require_scope('conversations')
@api_meta(summary=("Ensure the owner's health checks: the Router Watchdog conversation and, unless the "
                   "owner chose otherwise, its periodic review"), tags=['router'],
          scope='conversations', request_body=request_body('ensureRequest'))
def router_watchdog_ensure():
    try:
        _idempotency_key()
        parse('ensureRequest')
        from lib.router.watchdog.review import ensure_health_checks
        owner = _owner()
        return api_ok(_watchdog_state(owner, ensure_health_checks(
            owner, tenant_id=request_principal().tenant_id)))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/watchdog/review', methods=['POST'])
@require_scope('agents:scheduler')
@api_meta(summary="Enable or disable the Router Watchdog's periodic review and set its cadence",
          tags=['router'], scope='agents:scheduler', request_body=request_body('watchdogReviewRequest'))
def router_watchdog_review():
    try:
        _idempotency_key()
        data = parse('watchdogReviewRequest')
        from lib.router.watchdog.binding import router_watchdog
        from lib.router.watchdog.review import configure_review
        owner = _owner()
        binding = router_watchdog(owner, create=False)
        if binding['availability'] != 'available':
            raise PermissionError('Open the Watchdog before configuring its review')
        configure_review(owner, data, principal=request_principal())
        return api_ok(_watchdog_state(owner, binding))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


def _portfolio_state(owner: int) -> dict:
    from lib.router.portfolio.view import counts, ledger
    from lib.router.portfolio.review import review_state
    from lib.router.portfolio.registry import RouterPortfolio
    registry = RouterPortfolio(owner)
    registry_counts = counts(registry.open() + registry.page_closed()['closed'])
    return validate({'review': review_state(owner), 'counts': registry_counts,
                     'ledger': ledger(owner)}, 'portfolioState')


@api_v1_router_bp.route('/api/v1/router/portfolio', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read the portfolio review switch and the promised/delivered value ledger",
          tags=['router'], scope='conversations')
def router_portfolio_read():
    try:
        return api_ok(_portfolio_state(_owner()))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/portfolio/review', methods=['POST'])
@require_scope('agents:scheduler')
@api_meta(summary="Pause or resume the portfolio review's automatic initiative starts",
          description=('The owner\'s stop/pause switch for proactive portfolio work (docs/ROUTER.md R10). '
                       'Pausing stops new starts; started initiatives keep the authority they were admitted '
                       'with and still settle. The cadence is the documented daily review after quiet hours.'),
          tags=['router'], scope='agents:scheduler', request_body=request_body('portfolioReviewRequest'))
def router_portfolio_review():
    try:
        _idempotency_key()
        data = parse('portfolioReviewRequest')
        from lib.router.portfolio.review import configure_review
        owner = _owner()
        configure_review(owner, data, principal=request_principal())
        return api_ok(_portfolio_state(owner))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/projects', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read a page of the owner's projects with each Project Router's live activity",
          description=('The browser form of get_router_projects (docs/ROUTER.md): per bound project its '
                       'availability, active Workers and requests, latest report, board, commitments and '
                       'open owner questions. Passive; the router home and the Watchdog are omitted. Pass '
                       'nextCursor back as cursor until it is null.'),
          tags=['router'], scope='conversations', parameters=query_parameters('routerProjectsQuery'))
def router_projects_read():
    from quart import request
    try:
        query = {}
        for name in request.args:
            if len(request.args.getlist(name)) != 1:
                raise ValueError('Repeated query field: ' + name)
            query[name] = request.args[name]
        validate(query, 'routerProjectsQuery')
        from lib.router.projects import router_projects, router_projects_wire
        page = router_projects(_owner(), {'after_id': query['cursor']} if 'cursor' in query else {})
        return api_ok(validate(router_projects_wire(page), 'routerProjectsPage'))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/owner-delegation', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Read the owner's delegation to their main coordinator and its audit log",
          tags=['router'], scope='conversations')
def router_owner_delegation_read():
    try:
        from lib.router.owner_delegation import owner_delegation_state
        return api_ok(owner_delegation_state(_owner()))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/owner-delegation/history', methods=['GET'])
@require_scope('conversations')
@api_meta(summary="Query every delegated action ever recorded for the owner",
          description=('The durable audit history of actions the owner\'s leads took or tried under the '
                       'delegation, newest first, filtered by acting lead (requester), scope, status and '
                       'since (docs/ROUTER.md R9, Audit). Unlike owner-delegation, which shows the newest '
                       'retained records, nothing is dropped.'),
          tags=['router'], scope='conversations',
          parameters=query_parameters('ownerDelegationHistoryQuery'))
def router_owner_delegation_history():
    from quart import request
    try:
        query = {}
        for name in request.args:
            if len(request.args.getlist(name)) != 1:
                raise ValueError('Repeated query field: ' + name)
            query[name] = request.args[name]
        validate(query, 'ownerDelegationHistoryQuery')
        from lib import lifecycle_approval
        actions = lifecycle_approval.delegated_action_history(
            _owner(), requester=query.get('requester'), scope=query.get('scope'),
            status=query.get('status'), since=float(query['since']) if 'since' in query else None)
        return api_ok(validate({'actions': actions}, 'ownerDelegationHistory'))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


@api_v1_router_bp.route('/api/v1/router/owner-delegation', methods=['POST'])
@require_scope('admin')
@api_meta(summary="Grant or revoke the main coordinator's delegation to act for the owner",
          description=('Granting needs the owner acting in the Tofu UI (the UI session, or the loopback '
                       'open-mode UI, with same-origin browser evidence); API keys, scripts and agents get '
                       '403. The grant names the current main coordinator and is the full delegation: '
                       'every scope it can cover (restarts, Continue/Stop of sessions, reversible settings '
                       'and channel toggles, project setup and repair, pausing or resuming scheduled '
                       'tasks), a scope added later included (docs/ROUTER.md R9). Any '
                       'authenticated owner request revokes it at once.'),
          tags=['router'], scope='admin', request_body=request_body('ownerDelegationRequest'))
def router_owner_delegation():
    try:
        _idempotency_key()
        data = parse('ownerDelegationRequest')
        owner = _owner()
        from lib import lifecycle_approval
        from lib.router.binding import global_router
        from lib.router.owner_delegation import owner_delegation_state
        from .auth import human_ui_provenance, request_provenance
        if data['granted']:
            provenance = human_ui_provenance()
            router = global_router(owner, create=False)
            if router['availability'] != 'available':
                raise PermissionError('Open the main coordinator before granting it your delegation')
            lifecycle_approval.set_delegation(owner, True, provenance=provenance, grantee={
                'kind': 'global_router', 'conversationId': router['conversationId'],
                'bindingRevision': router['revision'], 'tenant': request_principal().tenant_id})
        else:
            lifecycle_approval.set_delegation(owner, False, grantee=None, provenance=request_provenance())
        return api_ok(owner_delegation_state(owner))
    except _TYPED_FAILURES as error:
        return _typed_error(error)




@api_v1_router_bp.route('/api/v1/router/questions/confirm-default', methods=['POST'])
@require_scope('conversations')
@api_meta(summary=("Settle an open non-blocking owner question on its stated default; nothing is sent to "
                   "the lead"), tags=['router'], scope='conversations',
          request_body=request_body('ownerQuestionConfirmRequest'))
def router_question_confirm_default():
    try:
        _idempotency_key()
        data = parse('ownerQuestionConfirmRequest')
        from lib.router.proactive.questions import QuestionNotConfirmable, confirm_default
        try:
            record, card = confirm_default(_owner(), data['coordinatorId'], data['guidanceId'])
        except QuestionNotConfirmable as error:
            return api_error(str(error), status=409, code='owner_question_not_confirmable')
        return api_ok(validate({
            'coordinatorId': record['coordinatorId'], 'guidanceId': record['guidanceId'],
            'defaultAcceptedAt': record['defaultAcceptedAt'], 'defaultAcceptedBy': record['defaultAcceptedBy'],
            'card': card}, 'ownerQuestionConfirmation'))
    except _TYPED_FAILURES as error:
        return _typed_error(error)


__all__ = ['api_v1_router_bp']
