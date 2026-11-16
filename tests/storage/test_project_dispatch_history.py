"""Bounded passive history across authorities, direction changes and cutover."""
import pytest

from lib.project_dispatch.repository import ProjectDispatchError
from tests.project_mod.test_project_dispatch_repository import context as context
from tests.storage.test_project_dispatch_repository_segments import owned as owned, authorize
from tests.turn_lifecycle.test_task_event_delivery import storage as storage

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_every_assignment_is_reachable_without_retaining_previous_pages(owned):
    c = owned
    expected = [authorize(c, str(index)) for index in range(90)]
    head = c.store.head()
    first = c.repo.history_page()
    second = c.repo.history_page(first['olderCursor'])
    third = c.repo.history_page(second['olderCursor'])
    assert first['newerCursor'] is None and third['olderCursor'] is None
    assert first['rows'] + second['rows'] + third['rows'] == expected[::-1]
    assert len(first['rows']) == len(second['rows']) == 32
    assert c.repo.history_page(third['newerCursor']) == second
    assert c.repo.history_page(second['newerCursor']) == first
    assert c.store.head() == head
    authorize(c, 'new-arrival')
    assert c.repo.history_page(first['olderCursor']) == second
    with pytest.raises(ProjectDispatchError): c.repo.history_page('older/another-project:1')


def test_passive_legacy_pages_keep_their_cursor_after_cutover(owned):
    c = owned; c.repo.storage_version = 1
    expected = [authorize(c, str(index)) for index in range(40)]
    c.repo.storage_version = 2
    first = c.repo.history_page()
    assert c.store.head() is None
    authorize(c, 'after-cutover')
    second = c.repo.history_page(first['olderCursor'])
    assert second['rows'] == expected[:8][::-1] and second['olderCursor'] is None
    previous = c.repo.history_page(second['newerCursor'])
    assert previous['rows'] == first['rows'] and previous['newerCursor']


def test_service_page_preserves_exact_lookup_and_owner_authority(owned):
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.identity import PrincipalContext
    c = owned
    expected = [authorize(c, str(index)) for index in range(35)]
    service = ProjectDispatchService(c.repo, turn_get=c.repo.turn_get, attempt_get=lambda *a, **kw: None)
    first = service.history_page(principal=c.principal)
    assert len(first['assignments']) == 32 and first['olderCursor'] and first['newerCursor'] is None
    head = c.store.head()
    exact = service.history_page(principal=c.principal, assignment_id=expected[0]['id'])
    replay = service.history_page(principal=c.principal, command_id='0')
    assert exact == replay and exact['assignments'][0]['assignmentId'] == expected[0]['id']
    assert exact['olderCursor'] is exact['newerCursor'] is None and c.store.head() == head
    foreign = PrincipalContext.user(subject_id='foreign', owner_user_id=c.repo.owner + 1, scopes=['chat'])
    with pytest.raises(PermissionError): service.history_page(principal=foreign, cursor=first['olderCursor'])


def _turn_read_log(storage, monkeypatch, *, conversation=None):
    """Every Turn read of the page, whether it goes alone or inside a pipelined batch."""
    reads = []

    def record(operation, payload):
        if operation == 'turn.get' and (conversation is None or payload['conversation_id'] == conversation):
            reads.append('preview' if 'content_preview_chars' in payload
                         else 'body' if payload.get('include_projection', True) else 'metadata')

    # ``port.query_many`` routes a single read to ``query`` and a batch to the
    # client's pipelined exchange, so the two wrappers never count the same read.
    real = storage.client.query

    def query(operation, payload, **kwargs):
        record(operation, payload)
        return real(operation, payload, **kwargs)
    monkeypatch.setattr(storage.client, 'query', query)
    from lib.storage import port
    real_many = port.query_many

    def query_many(client, requests, deadline=None):
        if len(requests) > 1:
            for operation, payload in requests:
                record(operation, payload)
        return real_many(client, requests, deadline=deadline)
    monkeypatch.setattr(port, 'query_many', query_many)
    return reads


def test_page_reads_a_worker_turn_body_only_when_its_revision_changes(storage, context, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.identity import PrincipalContext
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import HistoryPreviews, ProjectDispatchService
    c, owner = context, 1
    for row in c.conversations.rows.values():
        row['user_id'] = owner
    principal = PrincipalContext.user(subject_id='user:1', owner_user_id=owner, tenant_id='tenant-a', scopes=['chat'])
    repo = ProjectDispatchRepository(owner, '/project', client=lambda write=False: storage.client,
        conversations=c.conversations, turn_get=c.repo.turn_get, binding_repository=c.binding,
        clock=lambda: c.now[0])
    created = turns.create_turn_pair('recipient', command_id='worker-pair', input_projection={'content': 'brief'},
        config={'model': 'model-a'}, user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Worker', 'settings': {}})
    turn_id, attempt_id = created['turn']['turnId'], created['attempt']['attemptId']
    turns.bind_task(attempt_id, 'worker-task', user_id=owner)
    task = {'_attemptId': attempt_id, 'id': 'worker-task', 'status': 'running', '_userId': owner,
            '_principalContext': principal.to_payload(), 'content': 'seed', 'thinking': '', 'toolRounds': [],
            'model': 'model-a', 'config': {'model': 'model-a'}, 'convId': 'recipient', '_turnId': turn_id}
    turns.record_task_event(task, {'type': 'delta', 'content': 'seed'})
    turns.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})

    def publish(report):
        current = turns.get_turn('recipient', turn_id, user_id=owner)
        turns.update_turn_projection('recipient', turn_id, user_id=owner,
            expected_projection_revision=current['projectionRevision'], projection={'content': report,
                'toolRounds': [{'roundNum': 0, 'llmRound': 0, 'toolCallId': 'call', 'toolName': 'read_files',
                                'toolArgs': '{}', 'status': 'done', 'toolContent': 'evidence ' * 20000}]})
        return report
    report = publish('first report ' * 400)
    repo.bind_execution(repo.authorize(c.request, principal, 'worker')['id'], turn_id, attempt_id)
    reads = _turn_read_log(storage, monkeypatch)
    service = ProjectDispatchService(repo, commands=object(), history_previews=HistoryPreviews())

    def build():
        reads.clear()
        execution = service.history_page(principal=principal)['assignments'][0]['execution']
        assert execution['result'] == {'content': report[:2000]}
        return execution, list(reads)
    first, first_reads = build()
    # The authority cuts the preview: no build decodes the Worker's projection body.
    assert first_reads == ['preview']
    assert build() == (first, ['metadata'])
    report = publish('revised report ' * 400)
    revised, revised_reads = build()
    assert revised_reads == ['metadata', 'preview']
    assert revised['resultRevision'] > first['resultRevision']
    assert build() == (revised, ['metadata'])


def test_dependency_states_read_prerequisite_turns_as_metadata(storage, context, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.identity import PrincipalContext
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import HistoryPreviews, ProjectDispatchService
    c, owner = context, 1
    for row in c.conversations.rows.values():
        row['user_id'] = owner
    principal = PrincipalContext.user(subject_id='user:1', owner_user_id=owner, tenant_id='tenant-a', scopes=['chat'])

    def turn_get(conversation, identity, **kwargs):
        # The human authorization source stays the fixture's; Worker results are real Turns.
        read = c.repo.turn_get if conversation == 'coordinator' else turns.get_turn
        return read(conversation, identity, **kwargs)
    repo = ProjectDispatchRepository(owner, '/project', client=lambda write=False: storage.client,
        conversations=c.conversations, turn_get=turn_get, binding_repository=c.binding,
        clock=lambda: c.now[0])
    created = turns.create_turn_pair('recipient', command_id='prerequisite-pair', input_projection={'content': 'brief'},
        config={'model': 'model-a'}, user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Worker', 'settings': {}})
    turn_id, attempt_id = created['turn']['turnId'], created['attempt']['attemptId']
    turns.bind_task(attempt_id, 'prerequisite-task', user_id=owner)
    task = {'_attemptId': attempt_id, 'id': 'prerequisite-task', 'status': 'running', '_userId': owner,
            '_principalContext': principal.to_payload(), 'content': 'seed', 'thinking': '', 'toolRounds': [],
            'model': 'model-a', 'config': {'model': 'model-a'}, 'convId': 'recipient', '_turnId': turn_id}
    turns.record_task_event(task, {'type': 'delta', 'content': 'seed'})
    turns.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    current = turns.get_turn('recipient', turn_id, user_id=owner)
    turns.update_turn_projection('recipient', turn_id, user_id=owner,
        expected_projection_revision=current['projectionRevision'], projection={'content': 'result',
            'toolRounds': [{'roundNum': 0, 'llmRound': 0, 'toolCallId': 'call', 'toolName': 'read_files',
                            'toolArgs': '{}', 'status': 'done', 'toolContent': 'evidence ' * 20000}]})
    prerequisite = repo.authorize(c.request, principal, 'prerequisite')
    repo.bind_execution(prerequisite['id'], turn_id, attempt_id)
    revision = turns.get_turn('recipient', turn_id, user_id=owner, include_projection=False)['projectionRevision']
    dependent = repo.authorize({**c.request, 'dependencies': [
        {'assignmentId': prerequisite['id'], 'grantRevision': 1, 'resultRevision': revision}]}, principal, 'dependent')
    reads = _turn_read_log(storage, monkeypatch, conversation='recipient')
    states = repo.dependency_states(dependent)
    assert [(state['status'], state['reason']) for state in states] == [('waiting_review', 'review_required')]
    assert reads == ['metadata']
    # The former body read reaches the same verdict from the same public Turn.
    repo.turn_get = lambda conversation, identity, *, user_id, include_projection=True: turn_get(
        conversation, identity, user_id=user_id)
    assert repo.dependency_states(dependent) == states and reads == ['metadata', 'body']
    repo.turn_get = turn_get
    service = ProjectDispatchService(repo, commands=object(), history_previews=HistoryPreviews())
    for expected in (['metadata', 'preview'], ['metadata', 'metadata']):
        reads.clear()
        page = service.history_page(principal=principal)
        assert page['assignments'][0]['dependencyStates'] == states
        # Dependency evidence (metadata) and the prerequisite row's preview; the
        # page reads its rows' evidence before it evaluates any row's dependencies.
        assert sorted(reads) == sorted(expected)


def test_cutover_during_passive_read_never_synthesizes_wrong_order_cursor(owned, monkeypatch):
    from lib.project_dispatch.segmented import SegmentedDispatch
    c = owned
    for number in range(2): authorize(c, str(number))
    request = {name: c.request[name] for name in ('projectPath', 'coordinatorId', 'authorizationSource', 'budget')}
    request.update(objective='One owned batch', workers=[{name: c.request[name] for name in ('recipientId', 'brief', 'model', 'budget')}])
    c.repo.authorize_batch(request, principal=c.principal, command_id='batch')
    for number in range(33): authorize(c, 'later-' + str(number))
    expected = c.repo.history_page()
    real = SegmentedDispatch.head; calls = []
    def head(store):
        calls.append(True)
        return None if len(calls) == 1 else real(store)
    monkeypatch.setattr(SegmentedDispatch, 'head', head)
    assert c.repo.history_page() == expected


def test_page_dependency_rows_add_no_snapshot_reads(owned, monkeypatch):
    """Dependency rows evaluate against the page's loaded snapshot on every authority.

    The build's dispatch-document RPCs equal those of the page load alone; the
    prerequisite's Stop, attempt and Turn evidence remain live per dependency row.
    """
    from copy import deepcopy
    from lib.project_dispatch.service import HistoryPreviews, ProjectDispatchService
    c = owned
    turns = {'turn-prerequisite': {'turnId': 'turn-prerequisite', 'conversationId': 'recipient',
                                   'actor': 'assistant', 'projectionRevision': 5, 'projection': {'content': 'Result'}}}
    attempts = {'attempt-prerequisite': {'attemptId': 'attempt-prerequisite', 'turnId': 'turn-prerequisite',
                                         'conversationId': 'recipient', 'taskId': 'task-prerequisite',
                                         'status': 'completed'}}
    source = c.repo.turn_get
    from tests.support.turn_reads import turn_read
    c.repo.turn_get = lambda conversation, turn, **kwargs: (
        turn_read(turns[turn], **kwargs) if turn in turns else source(conversation, turn, **kwargs))
    c.repo.attempt_get = lambda attempt, **kwargs: deepcopy(attempts.get(attempt))
    prerequisite = authorize(c, 'prerequisite')
    c.repo.bind_execution(prerequisite['id'], 'turn-prerequisite', 'attempt-prerequisite')
    c.repo.record_review(prerequisite['id'], 'accept', 'Checked', 5, 1)
    pin = {'assignmentId': prerequisite['id'], 'grantRevision': 1, 'resultRevision': 5}
    for number in range(3):
        c.repo.authorize({**c.request, 'dependencies': [pin]}, c.principal, f'dependent-{number}')
    operations, factory = [], c.repo.client

    class Counted:
        def __init__(self, client):
            self.client = client

        def query(self, operation, payload, *args, **kwargs):
            operations.append(operation)
            return self.client.query(operation, payload, *args, **kwargs)

        def command(self, *args, **kwargs):
            return self.client.command(*args, **kwargs)
    monkeypatch.setattr(c.repo, 'client', lambda *, write=False: Counted(factory(write=write)))
    service = ProjectDispatchService(c.repo, commands=object(), turn_get=c.repo.turn_get,
                                     attempt_get=c.repo.attempt_get, history_previews=HistoryPreviews())
    c.repo.history_page()
    page_load = [operation for operation in operations if operation.startswith('project.dispatch.')]
    operations.clear()
    page = service.history_page(principal=c.principal)
    assert [operation for operation in operations if operation.startswith('project.dispatch.')] == page_load
    dependency_rows = [view for view in page['assignments'] if 'dependencyStates' in view]
    assert len(dependency_rows) == 3 and all(view['dependencyStates'] == [
        {'reference': pin, 'status': 'ready', 'reason': 'ready'}] for view in dependency_rows)
    # Both Stop watermarks of the prerequisite are read once per dependency row.
    assert operations.count('record.get') == 2 * len(dependency_rows)


def _bound_page(storage, context, rows):
    """``rows`` bound assignments on a real authority; the even (and oldest) ones have a file profile.

    Each Worker Turn is real and settled; file rows register their delivery
    workspace with an exact receipt, except the newest file row (absent workspace).
    Returns ``(repository factory, principal, publish)``; ``publish(index)`` revises
    one Worker Turn's report.
    """
    from copy import deepcopy
    from lib import integration_state_repository as state
    from lib import turn_lifecycle as turns
    from lib.identity import PrincipalContext
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import ProjectDispatchRepository
    c, owner = context, 1
    for row in c.conversations.rows.values():
        row['user_id'] = owner
    principal = PrincipalContext.user(subject_id='user:1', owner_user_id=owner, tenant_id='tenant-a', scopes=['chat'])

    def repository():
        return ProjectDispatchRepository(owner, '/project', client=lambda write=False: storage.client,
            conversations=c.conversations, turn_get=c.repo.turn_get, binding_repository=c.binding,
            clock=lambda: c.now[0])
    repo, bound = repository(), []
    last_file_row = max(number for number in range(rows) if not number % 2)
    for number in range(rows):
        created = turns.create_turn_pair('recipient', command_id=f'worker-pair-{number}',
            input_projection={'content': 'brief'}, config={'model': 'model-a'}, user_id=owner,
            conversation_defaults={'allowCreate': True, 'title': 'Worker', 'settings': {}})
        turn_id, attempt_id = created['turn']['turnId'], created['attempt']['attemptId']
        task_id = f'worker-task-{number:02}'
        turns.bind_task(attempt_id, task_id, user_id=owner)
        task = {'_attemptId': attempt_id, 'id': task_id, 'status': 'running', '_userId': owner,
                '_principalContext': principal.to_payload(), 'content': 'seed', 'thinking': '', 'toolRounds': [],
                'model': 'model-a', 'config': {'model': 'model-a'}, 'convId': 'recipient', '_turnId': turn_id}
        turns.record_task_event(task, {'type': 'delta', 'content': 'seed'})
        turns.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
        request = deepcopy(c.request)
        if not number % 2:
            request['profile'] = {'mode': 'files', 'tools': ['read_files'], 'readPaths': ['.'],
                                  'writePaths': [], 'maxToolCalls': 9}
        assignment = repo.authorize(request, principal, f'command-{number:02}')
        repo.bind_execution(assignment['id'], turn_id, attempt_id)
        bound.append(turn_id)
        if not number % 2 and number != last_file_row:
            binding = {'assignmentId': assignment['id'], 'owner': owner, 'recipientId': 'recipient',
                       'projectPath': '/project'}
            state.register_workspace(user_id=owner, project_root='/project', task_id=task_id,
                title=task_id, workspace_path=f'/work/{task_id}', managed=True, base_sha='a' * 40, now=1.0,
                origin={admission.KEY: binding, 'delivery': {
                    'state': 'pending' if number % 4 else 'blocked', 'reason': f'receipt {number}',
                    'checkpointSha': 'b' * 40, 'targetSha': ''}})

    def publish(index, report):
        current = turns.get_turn('recipient', bound[index], user_id=owner)
        turns.update_turn_projection('recipient', bound[index], user_id=owner,
            expected_projection_revision=current['projectionRevision'], projection={'content': report})
    for index in range(rows):
        publish(index, f'Worker {index} report. ' + 'finding ' * (300 + index))
    return repository, principal, publish


class _RoundTrips:
    """Storage round trips: outermost ``query``/``query_many`` calls on the authority's client."""

    def __init__(self, storage, monkeypatch):
        import threading
        self.count, inside = 0, threading.local()
        for name in ('query', 'query_many'):
            original = getattr(storage.client, name, None)
            if original is None:
                continue

            def counted(*args, _original=original, **kwargs):
                if getattr(inside, 'active', False):
                    return _original(*args, **kwargs)
                self.count += 1
                inside.active = True
                try:
                    return _original(*args, **kwargs)
                finally:
                    inside.active = False
            monkeypatch.setattr(storage.client, name, counted)

    def of(self, build):
        self.count = 0
        result = build()
        return result, self.count


def test_page_round_trips_do_not_grow_with_its_rows(storage, context, monkeypatch):
    """Each kind of row evidence is read in one exchange, so a page of 32 bound rows
    costs the storage round trips of a page of one: the head, the order index, the
    head witness and documents, then Turns, attempts and delivery workspaces."""
    from lib.project_dispatch.service import HistoryPreviews, ProjectDispatchService
    repository, principal, publish = _bound_page(storage, context, 33)
    service = ProjectDispatchService(repository(), commands=object(), history_previews=HistoryPreviews())
    trips = _RoundTrips(storage, monkeypatch)
    full, full_trips = trips.of(lambda: service.history_page(principal=principal))
    assert len(full['assignments']) == 32 and full['olderCursor']
    one, one_trips = trips.of(lambda: service.history_page(principal=principal, cursor=full['olderCursor']))
    assert len(one['assignments']) == 1
    assert full_trips == one_trips == 7
    # Unchanged previews read Turn metadata instead of bodies: the same exchanges.
    assert trips.of(lambda: service.history_page(principal=principal))[1] == 7
    # Changed Turns add one exchange for all of their bodies.
    publish(31, 'Revised report 31')
    publish(32, 'Revised report 32')
    revised, revised_trips = trips.of(lambda: service.history_page(principal=principal))
    assert revised_trips == 8
    assert revised['assignments'][0]['execution']['result'] == {'content': 'Revised report 32'}


def test_page_content_equals_the_row_by_row_reads(storage, context):
    """A page read together equals the same page read row by row, build after build."""
    from lib import integration_state_repository as state
    from lib import turn_lifecycle as turns
    from lib.project_dispatch.service import HistoryPreviews, ProjectDispatchService
    repository, principal, publish = _bound_page(storage, context, 12)
    together = ProjectDispatchService(repository(), commands=object(), history_previews=HistoryPreviews())
    row_by_row = ProjectDispatchService(repository(), commands=object(), turn_get=turns.get_turn,
        attempt_get=turns.get_attempt, workspace_get=state.find_workspace, history_previews=HistoryPreviews())

    def same_page():
        page = together.history_page(principal=principal)
        assert page == row_by_row.history_page(principal=principal)
        return page
    first = same_page()
    deliveries = [view['delivery'] for view in first['assignments'] if view['request'].get('profile')]
    assert {delivery['status'] for delivery in deliveries if delivery} == {'pending', 'blocked'}
    assert None in deliveries and all(view['execution']['status'] == 'completed' for view in first['assignments'])
    assert same_page() == first
    publish(3, 'Revised report 3')
    state.set_workspace_meta(user_id=1, project_root='/project', task_id='worker-task-04', now=2.0,
                             patch={'delivery': {'state': 'delivered', 'reason': '', 'checkpointSha': 'c' * 40,
                                                 'targetSha': 'd' * 40}})
    revised = same_page()
    assert revised != first
    assert same_page() == revised
