"""Offline pinned review guidance, distinct from checkpointless handoff.

Dispatch/Turn/Attempt ports are synthetic; workspace storage and Git are isolated.
A delivery pin supports file inspection, not an author-submit classification.
"""
from copy import deepcopy
from pathlib import Path

import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.conversations import coordinator_workers, review_loops, session_control
from lib.project_dispatch import admission, publication, scope
from lib.project_dispatch.service import ProjectDispatchService
from lib.project_mod import delivery
from tests.project_mod.test_project_publication import (
    FakeRepository, authority as authority_fixture, repo as repo_fixture, workspace,
)
from tests.project_mod.test_review_loops import Ports, _loop
from tests.storage.test_integration_delivery import git

repo = repo_fixture
authority = authority_fixture
pytestmark = pytest.mark.unit


class AuthorPorts(Ports):
    """Real service projections over explicit synthetic dispatch and execution ports."""

    def __init__(self, root, owner, monkeypatch):
        super().__init__()
        self.root, self.owner, self.monkeypatch = root, owner, monkeypatch
        self.scope = {'user_id': owner, 'project_path': str(root),
                      'conversation_id': 'lead', 'binding_revision': 1}
        self.rows, self.turns, self.attempts, self.workspaces = {}, {}, {}, {}
        self.successors = []
        self.add_author('assignment', 'predecessor', 'turn-1', 'attempt-1', 7)
        self.repository = self
        self.service = ProjectDispatchService(
            self, turn_get=self.turn_get, attempt_get=self.attempt_get,
            workspace_get=state.get_workspace)
        monkeypatch.setattr(coordinator_workers, 'delegate', self.delegate_successor)

    @property
    def project(self):
        return str(self.root)

    def read(self, identity):
        return deepcopy(self.rows[identity])

    read_point = read

    def validate_binding(self, coordinator, revision):
        assert (coordinator, revision) == ('lead', 1)

    def validate_grant_binding(self, row):
        assert row['request']['coordinatorId'] == 'lead'
        assert row['grant']['owner'] == self.owner and not row['grant']['revoked']

    def turn_get(self, conversation, turn, **kwargs):
        row = deepcopy(self.turns[turn])
        assert row['conversationId'] == conversation and kwargs['user_id'] == self.owner
        if kwargs.get('include_projection') is False:
            row.pop('projection', None)
        return row

    def attempt_get(self, attempt, *, user_id):
        assert user_id == self.owner
        return deepcopy(self.attempts[attempt])

    def add_author(self, identity, work_id, turn, attempt, revision):
        source = FakeRepository(self.root)
        row = source.assignment
        row['id'] = identity
        row['request'].update(coordinatorId='lead', title='Synthetic author')
        row['request']['profile'].update(
            mode='project', tools=['read_files', 'write_file', 'integration_checkpoint'],
            readPaths=['.'], writePaths=['.'])
        scope.validate_profile({'profile': row['request']['profile']})
        row['execution'] = {'turnId': turn, 'attemptId': attempt, 'taskId': work_id}
        binding = {**source.binding, 'assignmentId': identity}
        self.rows[identity] = row
        self.turns[turn] = {'conversationId': 'recipient', 'turnId': turn, 'actor': 'assistant',
                            'projectionRevision': revision, 'modelContextRevision': revision,
                            'projection': {'content': 'Synthetic explanation correction only.'}}
        self.attempts[attempt] = {'conversationId': 'recipient', 'turnId': turn,
                                  'attemptId': attempt, 'taskId': 'physical-' + attempt,
                                  'status': 'completed'}
        created = ic.create_task_workspace(
            str(self.root), work_id, user_id=self.owner, conversation_id='recipient',
            task={'config': {admission.KEY: binding}})
        delivery.bind_task_workspace(str(self.root), work_id, user_id=self.owner,
                                     conversation_id='recipient')
        self.workspaces[identity] = created['workspacePath']

    def view(self, identity, *, settlement_only=False):
        if identity not in self.rows:
            return super().view(identity, settlement_only=settlement_only)
        return self.service.view_for_coordinator(identity, scope=self.scope, settlement_only=settlement_only)

    def follow_up(self, view, instruction, key):
        identity = view['assignmentId']
        row = self.read(identity)
        assert not self.service.continues_in_place(row)
        result = session_control._successor(
            {}, self.scope, {'instruction': instruction, 'attempt_id': row['execution']['attemptId'],
                             'expected_revision': view['execution']['resultRevision']},
            row, self.turns[row['execution']['turnId']], self.service)
        assert result['continues_assignment'] == identity
        self.follow_ups.append(result)
        return result['assignment_id']

    def delegate_successor(self, task, scope, arguments, *, create, coverage_from):
        assert scope == self.scope and coverage_from == 'assignment'
        assert create(scope, arguments) == {'conversation_id': 'recipient'}
        assert 'new assignment continuing assignment assignment' in arguments['brief']
        self.successors.append(deepcopy(arguments))
        self.add_author('successor', 'successor-work', 'turn-2', 'attempt-2', 82)
        self.attempts['attempt-2']['status'] = 'running'
        return {'assignment_id': 'successor', 'dispatch': 'accepted', 'status': 'running',
                'task_id': 'physical-attempt-2'}

    def admit_submit(self):
        row = self.read('successor')
        registered = state.get_workspace(str(self.root), 'successor-work', user_id=self.owner)
        binding = registered['origin'][admission.KEY]
        task = {'id': 'physical-attempt-2', 'convId': 'recipient',
                'config': {admission.KEY: binding, 'projectPath': registered['workspace_path']}}

        def validated(candidate, **kwargs):
            assert candidate is task
            assert self.attempts['attempt-2']['status'] == 'running'
            assert binding['assignmentId'] == row['id']
            assert row['execution']['attemptId'] == self.attempts['attempt-2']['attemptId']
            return deepcopy(binding), deepcopy(row)

        self.monkeypatch.setattr(admission, 'validated_dispatch', validated)
        self.monkeypatch.setattr(admission, 'owned_workspace', lambda candidate, bound: registered)
        arguments = {'task_id': 'successor-work', 'summary': 'Synthetic file submission.'}
        assert 'integration_submit' in scope.granted_tools(row['request']['profile'])
        assert scope.admit_call(task, 'integration_submit', arguments, registered['workspace_path'])[1]
        refused = deepcopy(row)
        refused['request']['profile'].update(mode='files', tools=['read_files', 'write_file'])
        decision = admission.DispatchAdmission(task, binding, refused)
        with pytest.raises(scope.ToolAdmissionRefused, match='no authority for this exact tool'):
            scope.admit_call(task, 'integration_submit', arguments, registered['workspace_path'],
                             admission=decision)
        return ic.execute_integration_tool(
            'integration_submit', arguments, project_path=str(self.root),
            user_id=self.owner, conv_id='recipient')


def _reworking_loop(root, owner, monkeypatch):
    ports = AuthorPorts(root, owner, monkeypatch)
    pending = delivery.record_task_workspace_intent(
        str(root), 'predecessor', user_id=owner, outcome='completed')
    assert pending['state'] == 'pending'
    assert delivery.resume_pending_workspace_deliveries()
    assert ports.view('assignment')['delivery']['status'] == 'delivered'
    loop = _loop(roles=('correctness',))
    loop['authorAssignmentId'] = 'assignment'
    assert review_loops.advance(loop, ports) == (True, ['reviewer-1'])
    ports.settle('reviewer-1', 'Correct the explanation.\nREVIEW_VERDICT: fail')
    assert review_loops.advance(loop, ports) == (True, ['successor'])
    assert len(ports.follow_ups) == len(ports.successors) == 1
    return loop, ports


@pytest.mark.parametrize('authored_file', [
    pytest.param(False, id='automatic-baseline-authorship-unknown'),
    pytest.param(True, id='explicit-file-already-contained-control'),
])
def test_rework_subject_requires_author_provenance_not_delivery_success(repo, monkeypatch, authored_file):
    root, _, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(root.parent / 'managed'))
    loop, ports = _reworking_loop(root, owner, monkeypatch)
    loop['subject'] = ('Audit allowed/result.txt.' if authored_file else
                       'Audit base.txt and the final explanation correction.')
    work = Path(ports.workspaces['successor'])
    before = git(root, 'rev-parse', 'HEAD')
    explicit = None
    if authored_file:
        (work / 'allowed').mkdir()
        (work / 'allowed/result.txt').write_text('synthetic authored file\n')
        assert ports.admit_submit().startswith('Checkpoint saved')
        explicit = state.get_workspace(str(root), 'successor-work', user_id=owner)['checkpoint_sha']
        assert git(root, 'show', explicit + ':allowed/result.txt') == 'synthetic authored file'
        git(root, 'merge', '--ff-only', explicit)
    ports.attempts['attempt-2']['status'] = 'completed'
    pending = delivery.record_task_workspace_intent(
        str(root), 'successor-work', user_id=owner, outcome='completed')
    assert pending['state'] == 'pending' and not pending.get('checkpointSha')
    assert delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(root), 'successor-work', user_id=owner)
    receipt = row['origin']['delivery']
    assert receipt['state'] == 'delivered' and receipt['phase'] == 'no_changes'
    assert delivery.canonical_delivery(row['origin'])
    view = ports.view('successor')
    assert view['publication'] is None
    assert view['execution']['turnId'] == 'turn-2'
    assert view['execution']['attemptId'] == 'attempt-2'
    assert view['execution']['resultRevision'] == 82
    assert view['delivery']['taskId'] == 'successor-work'
    assert view['delivery']['checkpointSha'] == row['checkpoint_sha']
    assert row['origin'][admission.KEY]['assignmentId'] == 'successor'
    if authored_file:
        assert row['checkpoint_sha'] == explicit
        assert git(root, 'show', 'HEAD:allowed/result.txt') == 'synthetic authored file'
    else:
        assert git(root, 'rev-parse', 'HEAD') == before
        assert row['checkpoint_sha'] == row['origin']['sourceSnapshot']
        assert not (root / 'allowed/result.txt').exists()
    assert publication.delivery_view(
        ports.read('successor'), ports.attempt_get('attempt-1', user_id=owner), state.get_workspace) is None
    stale = deepcopy(ports.attempts['attempt-2'])
    ports.attempts['attempt-2'].update(turnId='turn-1')
    with pytest.raises(ValueError, match='association changed'):
        ports.view('successor')
    ports.attempts['attempt-2'] = stale
    checkpoint = row['checkpoint_sha']
    path, source = ('allowed/result.txt', 'synthetic authored file') if authored_file else ('base.txt', 'base')
    (root / path).write_text('later canonical file state\n')
    git(root, 'add', path)
    git(root, 'commit', '-m', 'later canonical state, not the review pin')
    assert git(root, 'rev-parse', 'HEAD') != checkpoint
    assert git(root, 'show', f'HEAD:{path}') != source
    assert review_loops.advance(loop, ports) == (True, ['reviewer-2'])
    author = loop['rounds'][1]['author']
    assert author == {'assignmentId': 'successor', 'attemptId': 'attempt-2',
                      'resultRevision': 82, 'checkpointSha': row['checkpoint_sha']}
    assert ports.accepted == ['reviewer-1'] and len(ports.follow_ups) == 1
    assert loop['outcome'] is None
    brief = ports.delegated[1]['brief']
    assert 're-audit round 2' in brief and 'Correct the explanation' in brief
    assert 'Synthetic explanation correction only.' not in brief
    assert loop['subject'] in brief
    assert f'The pinned inspection source is the exact tree of commit {checkpoint}' in brief
    assert '(delivery checkpoint)' in brief
    assert 'state `authorship unknown`' in brief
    assert 'not proof of an explicit author submission or contribution by this attempt' in brief
    assert 'Automatic no-delta settlement can supply a baseline SHA' in brief
    assert 'Missing submission evidence does not prove there is no code or no submission' in brief
    assert 'Inspect the affected paths named in the subject' in brief
    assert f'`git show {checkpoint}:<path>`' in brief
    assert git(root, 'show', f'{checkpoint}:{path}') == source
    assert 'No verified author baseline is supplied in this brief' in brief
    assert 'state `baseline unknown` and inspect the exact checkpoint' in brief
    assert f'git diff <verified-baseline> {checkpoint} -- <affected-paths>' in brief
    assert 'For non-file or otherwise unknown result material, use only an already authorized review channel' in brief
    assert 'Do not read private Worker sources or substitute arbitrary history' in brief
    assert 'a current answer is not the exact result version' in brief
    assert 'If required material is unavailable, report REVIEW_VERDICT: impasse' in brief
    assert 'do not judge unseen output' in brief
    assert 'Named project files remain independently inspectable at this exact pin' in brief
    assert 'Rules: do not edit, commit or deliver project files' in brief
    assert 'List every defect with its exact location' in brief and 'and the evidence' in brief
    assert 'For pass, record an exact source observation with record_working_fact' in brief
    assert 'REVIEW_FACT: id@revision' in brief
    assert 'no defect in your scope remains' in brief
    assert 'get_conversation(' not in brief and 'get_project_assignment(' not in brief
    assert "(the author's checkpoint)" not in brief


@pytest.mark.parametrize('receipt_checkpoint', ['', '2' * 40])
def test_delivery_projection_checkpoint_selection_has_no_submit_witness(repo, authority, receipt_checkpoint):
    root, _, owner = repo
    workspace(repo, authority)
    original = state.get_workspace(str(root), 'automatic', user_id=owner)
    row = deepcopy(original)
    row['checkpoint_sha'] = '3' * 40
    row['origin']['delivery'].update(state='pending', checkpointSha=receipt_checkpoint)
    before = deepcopy(row)
    reads = []

    def read(project, task, *, user_id):
        reads.append((project, task, user_id))
        return deepcopy(row)

    view = publication.delivery_view(authority.assignment, authority.attempt, read)
    assert view == {'taskId': 'automatic', 'status': 'pending', 'reason': '',
                    'checkpointSha': receipt_checkpoint or row['checkpoint_sha'], 'targetSha': ''}
    assert reads == [(str(root.resolve()), 'automatic', owner)]
    assert row == before and authority.assignment['publication'] is None
    assert state.get_workspace(str(root), 'automatic', user_id=owner) == original
