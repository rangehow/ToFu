"""Positive older-request receipts from one existing newest admission-index page.

The subset is never a current-request registry. Point documents and source gates
are rechecked after hydration; no dependency/cohort/history expansion is used.
"""
from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC

logger = get_logger(__name__)


def _records(documents):
    from lib.project_dispatch.repository import _validate_stored_row
    from lib.project_dispatch.segmented import identity
    definitions = {'assignments': 'assignment', 'requestBudgets': 'coordinatorRequestBudget',
                   'coordinatorSettlements': 'coordinatorSettlement'}
    records = {}
    for (field, ident), document in documents.items():
        if isinstance(document, StorageError):
            raise document
        if document is None:
            records[field, ident] = None
            continue
        envelope = document['value']
        record = envelope['record']
        if (identity(field, record) != ident or type(envelope['sequence']) is not int
                or envelope['sequence'] < 1 or type(document['version']) is not int
                or document['version'] < 1):
            raise ValueError('Changed parallel-request document identity')
        _validate_stored_row(record, definitions[field], ident, decoded=True)
        records[field, ident] = record
    return records


def _candidates(store, scope, head, latest_id, principal):
    repository = store.repo
    page = store.query('list', collection='indexes', prefix='assignments:order:',
                       after_key='', reverse=True, limit=SPEC['limits']['pageRows'])
    if not isinstance(page, list) or len(page) > SPEC['limits']['pageRows']:
        raise ValueError('Invalid parallel-request page')
    seen, previous = set(), None
    for entry in page:
        item = entry['value']
        expected = repository.key + ':indexes:assignments:order:' + f"{item['sequence']:020d}"
        if (type(item['sequence']) is not int or item['sequence'] < 1
                or entry['key'] != expected or previous is not None and expected >= previous
                or item['id'] in seen):
            raise ValueError('Invalid parallel-request index identity/order')
        seen.add(item['id'])
        previous = expected
    documents = store.get_many(('assignments', entry['value']['id']) for entry in page)
    records = _records(documents)
    candidates = []
    keys = set(head.get('taskKeys', ()))
    for entry in page:
        ident = entry['value']['id']
        row = records['assignments', ident]
        if row is None or documents['assignments', ident]['value']['sequence'] != entry['value']['sequence']:
            raise ValueError('Parallel-request index lost assignment evidence')
        request, grant = row['request'], row['grant']
        delegation = request.get('delegation') or {}
        budget = delegation.get('policy', {}).get('requestBudgetId')
        if (not budget or budget == latest_id or grant['owner'] != repository.owner
                or grant['tenant'] != principal.tenant_id or request['projectPath'] != repository.project
                or request['coordinatorId'] != scope['coordinatorId']
                or grant.get('bindingRevision') != scope['bindingRevision']
                or request.get('profile', {}).get('mode') == 'coordinator'
                or not keys.intersection(request.get('boardTaskKeys', ()))):
            continue
        candidates.append((row, budget, delegation['attemptId']))
    return documents, records, candidates


def _hydrate(store, scope, head, principal, documents, records, candidates):
    from lib.project_dispatch.service import ProjectDispatchService
    from .coordinator_work_evidence import _phase
    repository = store.repo
    keys = set(head.get('taskKeys', ()))
    points = store.get_many(point for row, budget, source in candidates for point in (
        ('requestBudgets', budget), ('coordinatorSettlements', source)))
    records.update(_records(points))
    documents.update(points)
    service = ProjectDispatchService(repository)
    groups, witnesses = {}, []
    for row, budget_id, source in candidates:
        budget = records['requestBudgets', budget_id]
        settlement = records['coordinatorSettlements', source]
        if (budget is None or settlement is not None or budget['tenant'] != principal.tenant_id
                or budget['coordinatorId'] != scope['coordinatorId']
                or budget['bindingRevision'] != scope['bindingRevision']
                or budget['authorizationSource'] != row['request']['authorizationSource']):
            continue
        value = {'binding': repository.binding, 'assignments': [row],
                 'requestBudgets': [budget], 'coordinatorSettlements': []}
        try:
            admission = repository._admission(row)
            repository.validate_own(admission, value=value)
            view = service._view(row, settlement_only=True, diagnostic_receipts=True)
            phase = _phase(view)
            repository.validate_own(admission, value=value)
        except Exception:
            logger.debug('Older-request source or receipt unavailable', exc_info=True)
            continue
        if phase in ('ended', 'delivered'):
            continue
        witnesses.append((row, value, view))
        group = groups.setdefault(budget_id, {'requestId': budget_id,
            'sourceTurnId': budget['authorizationSource']['turnId'], 'rows': []})
        group['rows'].append({'assignmentId': row['id'],
            'conversationId': row['request']['recipientId'],
            'taskKeys': list(dict.fromkeys(k for k in row['request']['boardTaskKeys'] if k in keys)),
            'phase': phase})
    return groups, witnesses


def read_additional(repository, scope, head, latest_id, principal):
    from lib.project_dispatch.segmented import SegmentedDispatch
    store = SegmentedDispatch(repository)
    try:
        documents, records, candidates = _candidates(store, scope, head, latest_id, principal)
        if not candidates:
            return []
        from lib.project_dispatch.service import ProjectDispatchService
        groups, witnesses = _hydrate(store, scope, head, principal, documents, records, candidates)
        service = ProjectDispatchService(repository)
        for row, value, view in witnesses:
            repository.validate_own(repository._admission(row), value=value)
            if service._view(row, settlement_only=True, diagnostic_receipts=True) != view:
                return []
        # Include absent settlement documents: a concurrent needs_user settlement
        # can leave every Worker running and must still retire the positive set.
        if store.get_many(documents) != documents:
            return []
        return list(groups.values())
    except Exception:
        logger.debug('Older-request subset unavailable', exc_info=True)
        return []
