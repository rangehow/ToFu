"""Delegation cost does not grow with the request; each Worker birth fence validates once.

Real Coordinator tool, segmented dispatch storage and conversation command
service on the selected authority; no provider and no executor host.
"""
import json
import threading
import uuid

import pytest

from tests.storage.test_native_project_dispatch import _git

pytest_plugins = ('tests.support.semantic_authorities',)

from lib.conversations.coordinator_births import spawn as _THREADED_BIRTH  # noqa: E402 - captured before fixtures patch it
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]


@pytest.fixture
def coordinator(semantic_authority, tmp_path, monkeypatch):
    factory, owner, engine = semantic_authority
    from lib import storage, integration_state_repository as state, turn_lifecycle as turns
    from lib.conversations import coordinator_workers
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.identity import PrincipalContext
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.project_dispatch.repository import CONTRACT, ProjectDispatchRepository
    from lib.project_dispatch.request_budget import bind_source_account
    from lib.project_dispatch.service import ProjectDispatchService
    if engine is not None:
        monkeypatch.setattr(storage, 'get_storage_client', factory)
        monkeypatch.setattr(state, 'get_storage_client', factory)

    def forbidden_provider(*args, **kwargs):
        pytest.fail('Delegation never calls a provider')
    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', forbidden_provider)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', forbidden_provider)
    root = tmp_path / 'canonical'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.name', 'Test')
    _git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'base.txt').write_text('base\n')
    _git(root, 'add', 'base.txt')
    _git(root, 'commit', '-m', 'base')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'workspaces'))
    binding = ProjectCoordinationRepository(owner).create(str(root), folder_id='folder',
        expected_revision=0, command_id='coordinator')
    pair = turns.create_turn_pair(binding['conversationId'], user_id=owner, command_id='request',
        input_projection={'content': 'Implement every part and verify it'}, config={})
    task_id = 'native-file-coordinator-' + uuid.uuid4().hex
    turns.bind_task(pair['attempt']['attemptId'], task_id, user_id=owner)
    turns.mark_task_started(pair['attempt']['attemptId'], task_id, user_id=owner)
    task = {'id': task_id, 'convId': binding['conversationId'], '_userId': owner, '_attended': True,
            '_turnId': pair['attempt']['turnId'], '_attemptId': pair['attempt']['attemptId'],
            '_principalContext': PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                tenant_id='delegation-reads', scopes=[]).to_payload(),
            'config': {'projectPath': str(root), 'codeExecEnabled': True, 'autoApply': True,
                       'tools': {'programmaticCalling': 'off'},
                       '_coordinatorSubmittedToolSettings': {'programmaticCalling': 'on'}}}
    names = ['read_files', 'write_file', 'run_command'] + list(CONTRACT['continuationTools'])
    capture_worker_policy(task, task['config'], [{'function': {'name': name}} for name in names],
                          'fixture-model', worker_model='fixture-model')
    repository = ProjectDispatchRepository(owner, str(root))
    bind_source_account(task, repository=repository)
    service = ProjectDispatchService(repository)
    monkeypatch.setattr(coordinator_workers, 'ProjectDispatchService', lambda _: service)
    # Births run inline so these specifications count a whole birth's work.
    from lib.conversations import coordinator_births
    monkeypatch.setattr(coordinator_births, 'spawn', lambda body, name: body())
    return task, type(factory())


def _delegate(task, key):
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    result = json.loads(execute_coordinator_tool(task, 'delegate_project_work', {
        'title': 'Part ' + key, 'brief': 'Implement and verify part ' + key + '.', 'idempotency_key': key}))
    assert result.get('dispatch') == 'accepted', result
    return result


def _storage_calls(client_type, monkeypatch):
    """Operations this thread sends through the authority's client."""
    calls, thread = [], threading.get_ident()
    for kind in ('query', 'command'):
        original = getattr(client_type, kind)

        def counted(self, operation, *args, _original=original, **kwargs):
            if threading.get_ident() == thread:
                calls.append(operation)
            return _original(self, operation, *args, **kwargs)
        monkeypatch.setattr(client_type, kind, counted)
    return calls


def test_a_delegation_costs_the_same_however_many_workers_its_request_has(coordinator, monkeypatch):
    """2026-11-11: each delegation re-read its whole request cohort (six times)
    to find the first Worker's policy, so it grew with every Worker."""
    task, client_type = coordinator
    for index in range(2):
        _delegate(task, f'early-{index}')
    calls = _storage_calls(client_type, monkeypatch)
    _delegate(task, 'third')
    third = list(calls)
    for index in range(3):
        _delegate(task, f'more-{index}')
    calls.clear()
    _delegate(task, 'seventh')
    assert sorted(calls) == sorted(third)


def test_the_coordinator_thread_makes_no_storage_call_to_delegate(coordinator, monkeypatch):
    """Owner decision 2026-11-11: delegating is as immediate as sending a message."""
    from lib.conversations import coordinator_births
    task, client_type = coordinator
    _delegate(task, 'warm-up')
    monkeypatch.setattr(coordinator_births, 'spawn', _THREADED_BIRTH)
    calls = _storage_calls(client_type, monkeypatch)
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    result = json.loads(execute_coordinator_tool(task, 'delegate_project_work', {
        'title': 'Part instant', 'brief': 'Implement and verify part instant.', 'idempotency_key': 'instant'}))
    assert calls == [] and result['assignment_id'] and result['conversation_id'], (calls, result)
    assert not coordinator_births.wait(task, None, deadline=None, interrupted=lambda: False)
    assert not coordinator_births.failures(task)
    monkeypatch.setattr(coordinator_births, 'spawn', lambda body, name: body())
    assert _delegate(task, 'instant')['assignment_id'] == result['assignment_id']


def test_a_worker_birth_validates_its_grant_once_per_fence(coordinator, monkeypatch):
    """Before its Turn exists, where the Turn's association binds and where the
    accepted attempt is admitted to a worker; the principal, birth time and
    work id in between reuse the command's validation (formerly 11 per birth)."""
    from lib.project_dispatch.repository import ProjectDispatchRepository
    task, _ = coordinator
    validations = []
    original = ProjectDispatchRepository.validate_own

    def counted(self, admission, **kwargs):
        validations.append(admission['assignmentId'])
        return original(self, admission, **kwargs)
    monkeypatch.setattr(ProjectDispatchRepository, 'validate_own', counted)
    worker = _delegate(task, 'one')
    assert validations == [worker['assignment_id']] * 3
    validations.clear()
    assert _delegate(task, 'one') == worker
    # A replay reads the existing association and binds nothing again.
    assert validations == [worker['assignment_id']]
