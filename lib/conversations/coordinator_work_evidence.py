"""Positive request-aware Worker evidence for a current task-board read.

The latest-request page is preserved. One newest order-index page may add
explicitly linked older requests; neither subset proves total coverage.
"""
from lib.log import get_logger

logger = get_logger(__name__)


def unknown_evidence():
    return {'scope': 'latest_request', 'coverage': 'unknown',
            'requestId': None, 'sourceTurnId': None, 'rows': []}


def _phase(view):
    execution, review = view['execution'], view['review']
    if execution is None:
        return 'unknown'
    if review is not None and review['resultRevision'] != execution['resultRevision']:
        return 'unknown'
    if view['revoked']:
        return 'ended'
    status = execution['status']
    if status in ('queued', 'pending'):
        return 'queued'
    if status == 'running':
        return 'active'
    if status == 'held':
        return 'held'
    if status in ('failed', 'cancelled', 'interrupted', 'truncated', 'superseded'):
        return 'ended'
    if status != 'completed':
        return 'unknown'
    if review is None:
        return 'awaiting_review'
    if review['decision'] != 'accept':
        return 'ended'
    receipt = view['delivery'] or {}
    publication = view.get('publication') or {}
    if (publication.get('resultRevision') != execution['resultRevision']
            or publication.get('grantRevision') != view['grantRevision']
            or not publication.get('checkpointSha')
            or publication['checkpointSha'] != receipt.get('checkpointSha')):
        return 'unknown'
    delivery = receipt.get('status')
    if view['canonicalDelivery']:
        return 'delivered'
    if delivery in ('blocked', 'failed', 'preserved'):
        return 'held'
    if delivery in ('working', 'pending', 'queued'):
        return 'awaiting_delivery'
    if delivery == 'closed':
        return 'ended'
    return 'unknown'


def read_evidence(owner, project, scope, head, *, client, bindings, principal):
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.segmented import SegmentedDispatch
    from lib.project_dispatch.service import ProjectDispatchService

    result = unknown_evidence()
    repository = ProjectDispatchRepository(owner, project, client=client, binding_repository=bindings)
    try:
        request, assignments = SegmentedDispatch(repository).latest_request_page()
        if (request is None or request['coordinatorId'] != scope['coordinatorId']
                or request['bindingRevision'] != scope['bindingRevision']
                or request['tenant'] != principal.tenant_id):
            return result
        result['requestId'] = request['id']
        result['sourceTurnId'] = request['authorizationSource']['turnId']
        keys = set(head.get('taskKeys', ()))
        rows = [row for row in assignments
                if row['grant']['owner'] == owner and row['grant']['tenant'] == principal.tenant_id
                and row['request']['projectPath'] == project
                and row['request']['coordinatorId'] == scope['coordinatorId']
                and row['grant'].get('bindingRevision') == scope['bindingRevision']
                and row['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId') == request['id']
                and row['request'].get('profile', {}).get('mode') != 'coordinator']
        service = ProjectDispatchService(repository)
        for row in rows:
            task_keys = list(dict.fromkeys(key for key in row['request'].get('boardTaskKeys', ()) if key in keys))
            if not task_keys:
                continue
            phase = 'unknown'
            try:
                phase = _phase(service._view(row, settlement_only=True, diagnostic_receipts=True))
            except Exception:
                logger.debug('Task-board Worker receipt unavailable', exc_info=True)
            result['rows'].append({'assignmentId': row['id'], 'conversationId': row['request']['recipientId'],
                                   'taskKeys': task_keys, 'phase': phase})
        from .coordinator_parallel_evidence import read_additional
        additional = read_additional(repository, scope, head, request['id'], principal)
        if additional:
            result['additionalRequests'] = additional
        latest = SegmentedDispatch(repository).head()
        if (latest is None or latest['value']['state']['phase'] != 'ready'
                or latest['value']['state'].get('latestBudgetId') != request['id']):
            return unknown_evidence()
        return result
    except Exception:
        logger.debug('Task-board request evidence unavailable', exc_info=True)
        return unknown_evidence()
