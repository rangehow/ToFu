"""Unimplemented binding/dispatch transaction boundary on real isolated authorities.

Only ordinary records, Turns and Attempts are prepared; no executor is started.
The strict expected failure is the missing authority fence, not desired behavior.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.support.coordinator_authority import create_bound_coordinator, start_coordinator_turn
from lib.identity import PrincipalContext
from lib.project_dispatch.repository import ProjectDispatchError, ProjectDispatchRepository
from lib.project_dispatch.segmented import SegmentedDispatch
from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


class MissingBindingCommitWitness(AssertionError):
    pass


@pytest.fixture
def binding_boundary(semantic_authority, monkeypatch):
    from lib import turn_lifecycle as turns

    factory, owner, engine = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: factory(**kw))
    prepared = create_bound_coordinator(factory, owner, '/project',
                                        folder_id='fixture', command_id='initial')
    conversations, binding = prepared.conversations, prepared.bindings
    coordinator = prepared.binding['conversationId']
    source = start_coordinator_turn(coordinator, owner, command_id='source',
        task_id='source-task', content='Consult on the registered task')
    source_attempt = source['attempt']['attemptId']
    factory(write=True).command('turn.event.record', {
        'user_id': owner, 'attempt_id': source_attempt, 'task_id': 'source-task',
        'terminal': True, 'status': 'completed', 'projection': {'content': 'Parked'},
        'settlement': {'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []},
    }, 'park-source')
    recipient = turns.create_turn_pair('recipient', command_id='prepared', config={}, user_id=owner,
        input_projection={'content': 'Prepared fixture only'},
        conversation_defaults={'allowCreate': True, 'title': 'Recipient',
                               'settings': {'projectPath': '/project'}})
    human = source['submittedTurn']
    repo = ProjectDispatchRepository(owner, '/project', client=factory, conversations=conversations,
        binding_repository=binding, turn_get=turns.get_turn, clock=lambda: 1000)
    request = {'projectPath': '/project', 'coordinatorId': coordinator, 'recipientId': 'recipient',
        'authorizationSource': {'conversationId': coordinator, 'turnId': human['turnId'],
                                'projectionRevision': human['projectionRevision']},
        'brief': 'Consult only', 'model': 'model-a',
        'budget': {'maxTokens': 100, 'maxModelRequests': 2, 'deadline': 2000}}
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                      tenant_id='fixture', scopes=['chat'])
    assignment = repo.authorize(request, principal, 'registered')

    def reopen():
        if engine is not None:
            engine.restart_cycle()
        else:
            from lib.storage import service
            runtime = service.storage_runtime()
            runtime.stop()
            runtime.start()

    return SimpleNamespace(repo=repo, binding=binding, source=source,
                           recipient=recipient, assignment=assignment, reopen=reopen,
                           turn_get=turns.get_turn, owner=owner)


def test_exact_bind_reopens_and_replay_retains_evidence_without_new_grant(binding_boundary):
    c = binding_boundary
    bound = c.repo.bind_execution(c.assignment['id'], c.recipient['turn']['turnId'],
                                  c.recipient['attempt']['attemptId'])
    assert bound['execution'] is not None
    c.reopen()
    assert c.repo.read(c.assignment['id']) == bound
    c.binding.bind('/project', '', expected_revision=1, command_id='withdraw-after-commit')
    replay = c.repo.bind_execution(c.assignment['id'], c.recipient['turn']['turnId'],
                                   c.recipient['attempt']['attemptId'])
    assert replay == bound
    assert replay['grant'] == c.assignment['grant']
    with pytest.raises(ProjectDispatchError, match='binding'):
        c.repo.validate(c.repo._admission(bound))


def test_binding_withdrawn_before_validation_refuses_without_association(binding_boundary):
    c = binding_boundary
    c.binding.bind('/project', '', expected_revision=1, command_id='withdraw')
    with pytest.raises(ProjectDispatchError, match='binding'):
        c.repo.bind_execution(c.assignment['id'], c.recipient['turn']['turnId'],
                              c.recipient['attempt']['attemptId'])
    c.reopen()
    assert c.repo.read(c.assignment['id']) == c.assignment
    assert c.binding.read('/project')['revision'] == 2


@pytest.mark.parametrize('with_witness', [False, True])
def test_unadmitted_binding_commit_version_refuses_without_writes_or_receipt(
        semantic_authority, with_witness):
    factory, owner, _ = semantic_authority
    scope = {'user_id': owner, 'project_path': '/binding-interface-fixture'}
    payload = {**scope, 'expected_revision': 0, 'head': {'nextSequence': 1},
               'changes': [{'collection': 'assignments', 'id': 'fixture',
                            'expected_version': 0, 'value': {'fixture': True}}]}
    proposed = deepcopy(payload)
    if with_witness:
        proposed['binding_witness'] = {'coordinator_id': 'fixture-coordinator',
                                       'revision': 1}
    command_id = 'unadmitted-binding-version'
    with pytest.raises(StorageError) as refused:
        factory(write=True).command('project.dispatch.commit_bound_v1', proposed, command_id)
    assert refused.value.code in ('database_protocol_error', 'unknown_operation')
    for collection, identity in (('head', 'root'), ('assignments', 'fixture')):
        assert factory().query('project.dispatch.get', {
            **scope, 'collection': collection, 'id': identity}) is None
    receipt = factory(write=True).command('project.dispatch.commit', payload, command_id)
    assert receipt['version'] == 1
    assert factory(write=True).command('project.dispatch.commit', payload, command_id) == receipt
    assert factory().query('project.dispatch.get', {
        **scope, 'collection': 'assignments', 'id': 'fixture'})['version'] == 1


@pytest.mark.xfail(strict=True, raises=MissingBindingCommitWitness,
    reason='Binding owner revision is not joined to dispatch association commit')
def test_binding_withdrawn_after_validation_must_fence_commit_and_reopen(binding_boundary, monkeypatch):
    c = binding_boundary
    original = SegmentedDispatch.document_command
    source_turn = c.turn_get(c.assignment['request']['coordinatorId'],
                            c.source['turn']['turnId'], user_id=c.owner, include_projection=False)
    head = SegmentedDispatch(c.repo).head()
    raced = []

    def withdraw_before_commit(store, changes, command_id=None):
        if not raced:
            raced.append(True)
            c.binding.bind('/project', '', expected_revision=1, command_id='withdraw-at-commit')
        return original(store, changes, command_id=command_id)

    monkeypatch.setattr(SegmentedDispatch, 'document_command', withdraw_before_commit)
    refused = None
    try:
        c.repo.bind_execution(c.assignment['id'], c.recipient['turn']['turnId'],
                              c.recipient['attempt']['attemptId'])
    except (ProjectDispatchError, StorageError) as error:
        refused = error
    assert raced == [True], 'The fault must run at the actual document command boundary'
    committed = deepcopy(c.repo.read(c.assignment['id']))
    c.reopen()
    assert c.repo.read(c.assignment['id']) == committed
    assert c.binding.read('/project')['revision'] == 2
    assert SegmentedDispatch(c.repo).head() == head
    assert c.turn_get(c.assignment['request']['coordinatorId'], c.source['turn']['turnId'],
                      user_id=c.owner, include_projection=False) == source_turn
    with pytest.raises(ProjectDispatchError, match='binding'):
        c.repo.validate(c.repo._admission(c.assignment))
    assert committed['request'] == c.assignment['request']
    assert committed['grant'] == c.assignment['grant']
    if committed['execution'] is not None:
        raise MissingBindingCommitWitness(
            'The parked source and unchanged grant survived reopen, but an association '
            'committed after the binding owner withdrew epoch 1; live validation rejects it')
    assert refused is not None
