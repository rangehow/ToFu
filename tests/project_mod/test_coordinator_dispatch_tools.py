"""Independent conversations reuse existing durable grants and Turn admission."""
from copy import deepcopy
from importlib import import_module
import json
from types import SimpleNamespace

import pytest

from lib.conversations.coordinator_tools import execute_coordinator_tool
from lib.project_dispatch import admission
from lib.project_dispatch.service import ProjectDispatchService
from tests.project_mod.test_project_dispatch_repository import context as context
from tests.project_mod.test_coordinator_reports import reports as reports

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(context, monkeypatch):
    c = context
    from lib.conversations import project_coordination, project_messages, repository as conversations
    from lib.project_dispatch import repository, service
    from lib.storage import service as storage_service
    # Lazy consumers capture these factories and Turn readers at import time.
    # Load their real bindings before source modules receive fixture fakes.
    import_module('lib.project_dispatch.recovery')
    import_module('lib.conversation_sync.runtime')
    import_module('lib.conversations.coordinator_progress')
    import_module('lib.conversations.catalog')
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: c.binding)
    monkeypatch.setattr(repository, 'ProjectDispatchRepository', lambda owner, path: c.repository(owner, path))
    monkeypatch.setattr(conversations, 'ConversationRepository', lambda: c.conversations)
    monkeypatch.setattr(project_messages, 'ConversationRepository', lambda: c.conversations)
    monkeypatch.setattr(admission, 'repository_for', lambda binding: c.repo)
    # Births and tool-count flushes run inline: these specifications observe
    # each delegation's outcome deterministically; their threads have their own.
    from lib.conversations import coordinator_births
    from lib.project_dispatch import request_budget
    monkeypatch.setattr(coordinator_births, 'spawn', lambda body, name: body())
    monkeypatch.setattr(request_budget, 'spawn', lambda body, name: body())
    state = SimpleNamespace(c=c, created=[], calls=[], turns={}, attempts={})

    class Storage:
        def command(self, operation, payload, command_id):
            assert operation == 'conversation.create'
            assert payload['user_id'] == 7
            if payload['conv_id'] in c.conversations.rows:
                from lib.storage.errors import StorageError
                raise StorageError('database_conflict', 'Conversation already exists')
            assert 'messages' not in payload and 'config' not in payload
            c.conversations.rows[payload['conv_id']] = {
                'user_id': 7, 'title': payload['title'], 'settings': deepcopy(payload['settings'])}
            state.created.append(deepcopy(payload))
    # Keep the public factory stable: consumers imported during this fixture
    # must follow the restored runtime after teardown, not retain a fake factory.
    monkeypatch.setattr(storage_service, '_runtime', SimpleNamespace(
        client=lambda **kwargs: Storage()))

    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            trusted = kwargs['trusted_dispatch']
            trusted.checked()
            assert kwargs['principal'].kind == 'system' and owner == 7
            assert request['inputActor'] == 'virtual_user'
            if state.calls:
                return SimpleNamespace(value={})
            state.calls.append((conv, deepcopy(request), kwargs['principal']))
            trusted.bind_execution('worker-turn', 'worker-attempt')
            state.turns['worker-turn'] = {'conversationId': conv, 'turnId': 'worker-turn', 'actor': 'assistant',
                'projectionRevision': 1, 'projection': {'content': ''}}
            state.attempts['worker-attempt'] = {'conversationId': conv, 'turnId': 'worker-turn',
                'attemptId': 'worker-attempt', 'taskId': 'worker-task', 'status': 'running'}
            return SimpleNamespace(value={})
    attempt_get = lambda attempt, **kw: deepcopy(state.attempts.get(attempt))
    c.repo.attempt_get = attempt_get
    state.service = ProjectDispatchService(c.repo, commands=Commands(),
        turn_get=lambda conv, turn, **kw: deepcopy(state.turns.get(turn)),
        attempt_get=attempt_get)
    monkeypatch.setattr(service, 'ProjectDispatchService', lambda repo: state.service)
    state.task = {'id': 'coordinator-task', 'convId': 'coordinator', '_userId': 7,
                  '_principalContext': c.principal.to_payload(), 'config': {'projectPath': '/project'}}
    state.call = lambda name, **args: json.loads(execute_coordinator_tool(state.task, name, args))
    return state


def create_worker(r):
    result = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    assert result['created_only'] and result['execution_started'] is False
    return result['conversation_id']


def test_late_storage_consumer_follows_runtime_restoration(runtime, monkeypatch):
    """A consumer imported during this fixture must not retain its fake client."""
    from lib.storage import get_storage_client
    from lib.storage import service as storage_service

    restored_client = object()
    monkeypatch.setattr(storage_service, '_runtime', SimpleNamespace(
        client=lambda **kwargs: restored_client))
    assert get_storage_client() is restored_client


def test_late_recovery_consumer_does_not_retain_fixture_factories(runtime):
    from lib.project_dispatch import recovery

    assert recovery.ProjectDispatchRepository is type(runtime.c.repo)
    assert recovery.ProjectDispatchService is ProjectDispatchService


def test_late_progress_and_catalog_consumers_do_not_retain_fixture_factories(runtime):
    """Imported first under this fixture, they kept its fakes for the rest of the worker's process,
    and later router specs on that worker failed (2026-11-14)."""
    from lib.conversations import catalog, coordinator_progress

    assert isinstance(coordinator_progress.ProjectCoordinationRepository, type)
    assert isinstance(catalog.ConversationRepository, type)


@pytest.mark.parametrize('files', [False, True])
def test_create_human_authorize_dispatch_follow_ordinary_worker(runtime, files):
    r = runtime
    worker = create_worker(r)
    assert worker != r.task['convId'] and len(r.created) == 1
    assert not r.calls and not r.c.repo.list()
    assert create_worker(r) == worker and len(r.created) == 1
    r.c.request['recipientId'] = worker
    if files:
        r.c.request['profile'] = {'mode': 'files', 'tools': ['read_files', 'write_file'],
                                  'readPaths': ['src'],
                                  'writePaths': ['src'], 'maxToolCalls': 4}
    # This is the existing explicit human grant boundary, not the tool handler.
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    before = deepcopy(assignment['grant'])
    result = r.call('dispatch_project_assignment', assignment_id=assignment['id'], expected_revision=1)
    assert result.get('dispatch') == 'accepted', result
    assert r.calls[0][0] == worker
    assert r.calls[0][1]['config'] == {}  # Config authority stays at ordinary Turn admission.
    assert len(r.calls) == 1
    assert r.c.repo.read(assignment['id'])['grant'] == before
    if files:
        assert r.c.repo.read(assignment['id'])['request']['profile'] == r.c.request['profile']
    # Exact retry reuses the ordinary execution association.
    assert r.call('dispatch_project_assignment', assignment_id=assignment['id'],
                  expected_revision=1)['dispatch'] == 'accepted'
    assert len(r.calls) == 1
    writes = r.c.client.puts
    r.attempts['worker-attempt']['status'] = 'completed'
    r.turns['worker-turn'].update(projectionRevision=2, projection={'content': 'Verified worker result'})
    followed = r.call('get_project_assignment', assignment_id=assignment['id'])
    assert followed['execution']['taskId'] == 'worker-task'
    assert followed['execution']['status'] == 'completed'
    assert followed['execution']['resultRevision'] == 2
    assert not followed['canonicalDelivery']
    assert len(r.calls) == 1 and r.c.client.puts == writes


@pytest.mark.parametrize('violation', ['missing', 'stale', 'revoked', 'tenant', 'source', 'binding', 'recipient'])
def test_invalid_authority_never_dispatches_or_mints_grants(runtime, violation):
    r = runtime
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    args = {'assignment_id': assignment['id'], 'expected_revision': 1}
    if violation == 'missing': args['assignment_id'] = 'missing'
    if violation == 'stale': args['expected_revision'] = 2
    if violation == 'revoked': r.c.repo.revoke(assignment['id'], 1)
    if violation == 'tenant': r.task['_principalContext']['tenant_id'] = 'other'
    if violation == 'source': r.c.turn['actor'] = 'virtual_user'
    if violation == 'binding': r.c.binding.value['revision'] = 2
    if violation == 'recipient': r.c.conversations.rows['recipient']['settings']['projectPath'] = '/other'
    writes = r.c.client.puts
    result = r.call('dispatch_project_assignment', **args)
    assert 'error' in result, result
    assert not r.calls and r.c.client.puts == writes
    assert len(r.c.repo.list()) == 1


def test_creation_conflict_and_foreign_coordinator_are_rejected(runtime):
    r = runtime
    create_worker(r)
    assert 'error' in r.call('create_conversation', title='different', idempotency_key='work-one')
    r.c.binding.value['conversationId'] = 'recipient'
    assert 'error' in r.call('create_conversation', title='new', idempotency_key='two')
    assert len(r.created) == 1 and not r.calls


def test_registered_tools_create_and_dispatch_through_real_pipeline(runtime):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.coordinator import COORDINATOR_TOOLS
    r = runtime
    r.task.update({key: value for key, value in _mk_task().items() if key not in r.task})
    r.task['config'].update(autoApply=True)
    messages = []
    def run(name, args):
        parsed = _mk_tc(name, name, len(messages), args=args)
        r.task['toolRounds'] = [parsed[5]]
        execute_tool_pipeline(r.task, [parsed], r.task['config'], '/project', True,
                              COORDINATOR_TOOLS, messages, [], 0, 'test-model')
        assert parsed[5]['status'] == 'done', messages[-1]
    run('create_conversation', {'title': 'Independent worker', 'idempotency_key': 'work-one'})
    assert len(r.created) == 1 and not r.calls
    r.c.request['recipientId'] = r.created[0]['conv_id']
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    run('dispatch_project_assignment', {'assignment_id': assignment['id'], 'expected_revision': 1})
    assert len(r.calls) == 1 and r.calls[0][0] == r.created[0]['conv_id']
    run('get_project_assignment', {'assignment_id': assignment['id']})
    assert len(r.calls) == 1


def test_report_tool_reaches_canonical_publication_through_real_pipeline(runtime, reports, monkeypatch):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.coordinator import COORDINATOR_TOOLS
    from lib.conversations import coordinator_reports
    r = runtime
    monkeypatch.setattr(coordinator_reports, 'CoordinatorReports', reports.factory)
    r.task.update({key: value for key, value in _mk_task().items() if key not in r.task})
    r.task['config'].update(autoApply=True)
    for index in range(2):
        parsed = _mk_tc('report-' + str(index), 'report_to_user', index, args=reports.request)
        r.task['toolRounds'] = [parsed[5]]
        messages = []
        execute_tool_pipeline(r.task, [parsed], r.task['config'], '/project', True,
                              COORDINATOR_TOOLS, messages, [], 0, 'test-model')
        assert parsed[5]['status'] == 'done', messages
        assert json.loads(messages[-1]['content'])['published']
    assert len(reports.turns) == 1 and reports.repo.read()['unread']
    assert not r.created and not r.calls  # Reporting starts no worker/model.


def test_report_files_are_published_with_the_report_and_named_by_its_receipt(runtime, reports, monkeypatch):
    from lib.artifacts import events
    from lib.conversations import coordinator_reports
    from lib.tools import rich_response
    r = runtime
    monkeypatch.setattr(coordinator_reports, 'CoordinatorReports', reports.factory)
    published, emitted = [], []

    def publish(task, path, title, project_path):
        assert task is r.task and project_path == '/project'
        if path.endswith('.exe'):
            raise ValueError('Unsupported or empty response file')
        published.append(path)
        name = path.rsplit('/', 1)[-1]
        return {'artifactId': 'art-' + name, 'filename': name, 'sizeBytes': 3,
                'format': 'file' if name.endswith(('.pdf', '.pptx')) else 'markdown', 'artifact': {'id': name}}
    monkeypatch.setattr(rich_response, 'publish_file_for_task', publish)
    monkeypatch.setattr(events, 'emit_artifact_event', lambda task, artifact: emitted.append(artifact['id']))

    refused = r.call('report_to_user', **{**reports.request, 'idempotency_key': 'bad',
                                          'files': ['out/cv.pdf', 'out/tool.exe']})
    assert 'tool.exe' in json.dumps(refused) and not reports.turns and not emitted

    published.clear()
    result = r.call('report_to_user', **{**reports.request, 'kind': 'delivery',
                                         'files': ['out/cv.pdf', 'out/talk.pptx', 'out/notes.md', 'out/cv.pdf']})
    assert published == ['out/cv.pdf', 'out/talk.pptx', 'out/notes.md']
    assert [(entry['filename'], entry['kind'], entry['path']) for entry in result['files']] == [
        ('cv.pdf', 'file', 'out/cv.pdf'), ('talk.pptx', 'deck', 'out/talk.pptx'),
        ('notes.md', 'document', 'out/notes.md')]
    assert result['published'] and emitted == ['cv.pdf', 'talk.pptx', 'notes.md']
    [turn] = reports.turns.values()
    assert turn['kind'] == 'coordinator_report.delivery'
    assert 'files' not in turn['projection']  # The receipt names the files; the Turn stays the report text.


def test_dispatch_error_does_not_claim_execution_never_started(runtime, monkeypatch):
    from lib.storage.errors import StorageError
    r = runtime
    assignment = r.c.repo.authorize(r.c.request, r.c.principal, 'approved-work')
    monkeypatch.setattr(r.service, 'dispatch_authorized',
                        lambda binding: (_ for _ in ()).throw(StorageError('storage_unavailable', 'lost response')))
    result = r.call('dispatch_project_assignment', assignment_id=assignment['id'], expected_revision=1)
    assert result['execution_started'] is None
    assert 'get_project_assignment' in result['recovery']


def test_an_acknowledged_creation_is_not_read_back_and_replays_its_exact_command(runtime, monkeypatch):
    """conversation.create refuses an existing ID, so its ACK proves this command
    created the header; a lost ACK replays the identical command."""
    import lib.storage
    r = runtime
    attempts = []
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: SimpleNamespace(
        command=lambda operation, payload, command_id: attempts.append((operation, deepcopy(payload), command_id))))
    r.c.conversations.calls.clear()
    first = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    second = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    assert first['created_only'] and second == first
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert first['conversation_id'] not in {call[0] for call in r.c.conversations.calls}
    assert not r.calls


def test_creation_replay_uses_original_request_after_ordinary_metadata_edits(runtime):
    r = runtime
    worker = create_worker(r)
    row = r.c.conversations.rows[worker]
    row['title'] = 'Human renamed this worker'
    row['settings'].update(model='different-model', temperature=0.5)
    assert create_worker(r) == worker
    assert 'error' in r.call('create_conversation', title=row['title'], idempotency_key='work-one')
    assert len(r.created) == 1 and not r.calls


@pytest.mark.parametrize('collision', ['request', 'project', 'binding', 'missing_marker'])
def test_creation_race_validates_committed_identity(runtime, monkeypatch, collision):
    import lib.storage
    r = runtime
    def command(operation, payload, command_id):
        # Another creator committed this ID first: storage refuses ours.
        settings = deepcopy(payload['settings'])
        if collision == 'request': settings['_coordinatorCreationRequest'] = 'another-request'
        if collision == 'project': settings['projectPath'] = '/other'
        if collision == 'binding': settings['_coordinatorCreationKey'] = 'another-binding'
        if collision == 'missing_marker': settings.pop('_coordinatorCreationRequest')
        r.c.conversations.rows[payload['conv_id']] = {
            'user_id': 7, 'title': payload['title'], 'settings': settings}
        from lib.storage.errors import StorageError
        raise StorageError('database_conflict', 'Conversation already exists')
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: SimpleNamespace(command=command))
    result = r.call('create_conversation', title='Independent worker', idempotency_key='work-one')
    assert 'conflicts' in result['error'] and result['execution_started'] is False
    assert not r.calls


def test_pinned_coordinator_turn_cannot_create_after_binding_epoch_changes(runtime):
    r = runtime
    create_worker(r)
    r.c.binding.value['revision'] += 1
    assert 'error' in r.call('create_conversation', title='another', idempotency_key='new')
    assert len(r.created) == 1


def test_create_only_receipt_points_to_current_request_delegation(runtime):
    r = runtime
    result = r.call('create_conversation', title='Empty workspace discussion', idempotency_key='create-only')
    assert result['created_only'] and result['execution_started'] is False
    assert 'delegate_project_work' in result['next_step']
    assert 'creates and starts its own Worker' in result['next_step']
    assert 'without a separate per-worker authorization' in result['next_step']
    assert 'grants no execution rights' in result['next_step']
    assert not r.calls and not r.c.repo.list()


def test_a_report_names_its_board_task_or_is_refused_before_anything_is_published(runtime, reports, monkeypatch):
    """Owner request 2026-11-17: the board expands each task to the reports that name it."""
    from lib.artifacts import events
    from lib.conversations import coordinator_progress, coordinator_reports
    from lib.tools import rich_response
    r = runtime
    monkeypatch.setattr(coordinator_reports, 'CoordinatorReports', reports.factory)
    published = []
    monkeypatch.setattr(rich_response, 'publish_file_for_task', lambda *args: published.append(args) or {})
    monkeypatch.setattr(events, 'emit_artifact_event', lambda task, artifact: None)
    head = {'board': {'tasks': [{'title': 'Resume', 'status': 'in_progress'},
                                {'title': 'Deck', 'status': 'pending'}]}, 'taskKeys': ['a' * 32, 'b' * 32]}
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}

    class Progress:
        def __init__(self, owner, project):
            assert (owner, project) == (7, '/project')

        def current(self):
            return scope, {}, head
    monkeypatch.setattr(coordinator_progress, 'CoordinatorTaskProgress', Progress)

    refused = r.call('report_to_user', **{**reports.request, 'idempotency_key': 'off-board',
                                          'task': 'Interview', 'files': ['out/cv.pdf']})
    assert 'Resume; Deck' in json.dumps(refused) and not reports.turns and not published

    result = r.call('report_to_user', **{**reports.request, 'task': 'Deck'})
    assert result['task'] == {'key': 'b' * 32, 'title': 'Deck'} and result['published']
    [turn] = reports.turns.values()
    assert 'task' not in turn['projection']  # The receipt names the task; the Turn stays the report text.

    # A head written before task history keys each title by its first identity.
    from lib.conversations.coordinator_task_history import title_key
    head.pop('taskKeys')
    assert r.call('report_to_user', **{**reports.request, 'idempotency_key': 'legacy', 'task': 'Resume'}
                  )['task'] == {'key': title_key('Resume'), 'title': 'Resume'}
