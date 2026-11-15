"""Passive original-request usage projection for the human interface.

Coordinator requests have no spending ceiling, so there are no defaults to
configure. This module reads the latest request account of the current
Coordinator binding and projects combined Coordinator + Worker usage. It never
spends, resumes or cancels work. HTTP uses read_state; request_usage reads the
account of one request named by its source Turn (the portfolio digest's cost
of an initiative, docs/ROUTER.md R10).
"""
from lib.project_dispatch.repository import _validate_schema
from .project_identity import normalize_project_path


def read_state(owner, project, *, repository=None, bindings=None):
    project = normalize_project_path(project)
    result = {'projectPath': project, 'request': None}
    if repository is None:
        from lib.project_dispatch.repository import ProjectDispatchRepository
        repository = ProjectDispatchRepository(owner, project)
    if bindings is None:
        from .project_coordination import ProjectCoordinationRepository
        bindings = ProjectCoordinationRepository(owner, client=repository.client)
    value, _ = repository._load(repository._selection(latest_budget=True))
    binding = bindings.read(repository.project)
    candidates = [row for row in value.get('requestBudgets', []) if row['coordinatorId'] == binding.get('conversationId')
                  and row['bindingRevision'] == binding.get('revision')]
    if candidates:
        from lib.project_dispatch.request_budget import request_view
        latest = max(candidates, key=lambda row: row['createdAt'])
        result['request'] = request_view(repository, value, latest['id'])
    _validate_schema(result, 'coordinatorUsageState')
    return result


def request_usage(owner, project, *, conversation_id, binding_revision, turn_id, repository=None):
    """Combined Coordinator + Worker usage of the request that ``turn_id`` opened.

    None while that request has no account yet: its input still waits in the
    Coordinator's queue, or the request has not reached its first model call.
    """
    from lib.project_dispatch.request_budget import account_identity, request_view
    from lib.turn_lifecycle import LifecycleNotFound
    if repository is None:
        from lib.project_dispatch.repository import ProjectDispatchRepository
        repository = ProjectDispatchRepository(owner, normalize_project_path(project))
    try:
        turn = repository.turn_get(conversation_id, turn_id, user_id=repository.owner, include_projection=False)
    except LifecycleNotFound:
        return None
    if not isinstance(turn, dict) or turn.get('turnId') != turn_id:
        return None
    identity = account_identity(binding_revision, {'conversationId': conversation_id, 'turnId': turn_id,
                                                   'projectionRevision': turn.get('projectionRevision')})
    value, _ = repository._load(repository._selection(budgets=[identity]))
    if not any(row['id'] == identity for row in value.get('requestBudgets', [])):
        return None
    return request_view(repository, value, identity)['usage']
