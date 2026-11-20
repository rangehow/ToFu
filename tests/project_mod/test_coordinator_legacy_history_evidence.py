"""Offline evidence limits, not a runtime repair or certification of legacy rows."""
import ast
import hashlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from copy import deepcopy

import pytest

from lib.conversations import coordinator_progress, coordinator_task_history
from lib.conversations.coordinator_progress import CoordinatorTaskProgress, POLICY
from lib.storage.errors import StorageError
from tests.project_mod.test_coordinator_task_history import Store, task

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'coordinator_history'
DIGESTS = {
    'd12cd290': '6106ba7539b0389a5088792f4fea9d6f50995181cd4a333f41b96247e76da410',
    'd1422634': '598f5d14069d67682b1d3814dea134996c7c7c64ad357ca7c98ac677cf9d16a9',
    'prepare_d1422634': 'c6a929b213f40a53a5b3d3a223cc078a7fdada9c490deee3b63dcaa8ccbcccf4',
    'publish_d12cd290': '608a3d8718bebe7811d9ea21e92e71b8cb910fc5b64e59ac889ebf3c46a7f854',
}


def captured(name):
    source = (FIXTURES / f'pre_cas_{name}.txt').read_bytes()
    assert hashlib.sha256(source).hexdigest() == DIGESTS[name]
    return source.decode()


def legacy_writer(monkeypatch, version):
    legacy = ModuleType('captured_legacy_history')
    legacy.__dict__.update(vars(coordinator_task_history))
    tree = ast.parse(captured(version))
    # Execute captured transitions unchanged, not retired query limits or an old HTTP reader.
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 or isinstance(node, ast.ClassDef) and node.name == '_Records']
    exec(compile(tree, f'pre_cas_{version}.txt', 'exec'), legacy.__dict__)

    def no_transcript(scope):
        pytest.fail('The offline head/history proof must not read a conversation')

    monkeypatch.setattr(coordinator_task_history, 'record_board', legacy.record_board, raising=False)
    monkeypatch.setattr(coordinator_task_history, 'transcript_boards', no_transcript)
    namespace = dict(vars(coordinator_progress))
    method = 'publish_d12cd290' if version == 'd12cd290' else 'prepare_d1422634'
    exec(compile(captured(method), f'pre_cas_{method}.txt', 'exec'), namespace)
    override = '_publish' if version == 'd12cd290' else '_prepare_write'
    writer = type('CapturedLegacyProgress', (CoordinatorTaskProgress,), {override: namespace[override]})
    return legacy, writer


def world(writer):
    store = Store()
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    bindings = SimpleNamespace(read=lambda path: deepcopy(binding))
    now = [1.0]
    scope = {'owner': 7, 'projectPath': '/project', 'coordinatorId': 'coordinator', 'bindingRevision': 1}
    progress = writer(7, '/project', client=store, bindings=bindings, notify=lambda: None, clock=lambda: now[0])
    history = coordinator_task_history.CoordinatorTaskHistory(
        7, '/project', client=store, bindings=bindings, clock=lambda: now[0])

    def publish(*tasks, summary='Board'):
        return progress._publish(scope, lambda previous: {'summary': summary, 'tasks': list(tasks)}, None)

    return SimpleNamespace(store=store, scope=scope, progress=progress, history=history, now=now, publish=publish)


def abandon_head(w, monkeypatch, *tasks):
    command = w.store.command
    refused = []

    def refuse(operation, payload, command_id):
        if payload['namespace'] == POLICY['namespace']:
            refused.append(deepcopy(payload['value']))
            raise StorageError('database_conflict', 'Synthetic abandoned head CAS')
        return command(operation, payload, command_id)

    with monkeypatch.context() as patch:
        patch.setattr(w.store, 'command', refuse)
        with pytest.raises(StorageError, match='changed concurrently'):
            w.publish(*tasks)
    assert len(refused) == POLICY['casAttempts']
    return refused[0]


@pytest.mark.parametrize('version', ['d12cd290', 'd1422634'])
def test_abandoned_legacy_candidate_matches_advanced_revision_but_not_committed_board(monkeypatch, version):
    legacy, writer = legacy_writer(monkeypatch, version)
    w = world(writer)
    w.publish(task('Confirmed', 'pending'))
    w.now[0] = 2.0
    candidate = abandon_head(w, monkeypatch, task('Confirmed', 'pending'), task('Candidate only', 'completed'))
    w.publish(task('Confirmed', 'pending'), summary='Legitimate summary change')
    _, _, head = w.progress.current()
    rows = legacy._Records(w.store, w.scope).all()
    abandoned = rows[legacy.title_key('Candidate only')]
    assert candidate['board']['revision'] == head['board']['revision'] == abandoned['changedRevision'] == 2
    assert head['board']['tasks'] == [task('Confirmed', 'pending')]
    assert abandoned['key'] not in head['taskKeys']
    before = deepcopy(w.store.rows)
    page = w.history.read()
    assert w.store.rows == before
    entries = {entry['title']: entry for entry in page['tasks']}
    assert entries['Confirmed']['onBoard'] is True
    # This pins the known limitation, not a desired classification: revision fencing accepts the abandoned row.
    assert entries['Candidate only']['status'] == 'completed' and not entries['Candidate only']['onBoard']
    assert page['recorded'] is True and page['counts']['completed'] == 1


def test_successful_and_abandoned_legacy_completion_have_identical_surviving_evidence(monkeypatch):
    legacy, writer = legacy_writer(monkeypatch, 'd12cd290')
    successful, abandoned = world(writer), world(writer)
    for w in (successful, abandoned):
        w.publish(task('X', 'pending'))
        w.now[0] = 2.0
    successful.publish(task('X', 'completed'))
    refused = abandon_head(abandoned, monkeypatch, task('X', 'completed'))
    abandoned.publish(task('X', 'pending'), summary='Legitimate summary change')
    assert successful.progress.current()[2]['board']['tasks'] == refused['board']['tasks'] == [task('X', 'completed')]
    assert abandoned.progress.current()[2]['board']['tasks'] == [task('X', 'pending')]
    for w in (successful, abandoned):
        w.now[0] = 3.0
        w.publish(summary='Same final board')
    # Includes all scoped record values AND CAS versions, not merely a title/status comparison.
    assert successful.store.rows == abandoned.store.rows
    assert successful.progress.current() == abandoned.progress.current()
    rows = legacy._Records(successful.store, successful.scope).all()
    [record] = rows.values()
    assert {field: record[field] for field in ('status', 'completedAt', 'lastSeenRevision', 'departedRevision', 'changedRevision')} == {
        'status': 'completed', 'completedAt': 2000, 'lastSeenRevision': 2, 'departedRevision': 3, 'changedRevision': 3}
    before = deepcopy(abandoned.store.rows)
    assert successful.history.read() == abandoned.history.read()
    assert abandoned.store.rows == before
    [entry] = abandoned.history.read()['tasks']
    assert entry['status'] == 'completed' and not entry['onBoard'] and not entry['dropped']


def test_current_head_does_not_authenticate_inherited_optional_fields(monkeypatch):
    legacy, writer = legacy_writer(monkeypatch, 'd1422634')
    w = world(writer)
    w.publish(task('Confirmed', 'pending'))
    w.now[0] = 2.0
    abandon_head(w, monkeypatch, task('Candidate alias', 'pending', progress='Never committed'))
    w.publish(task('Confirmed', 'pending'), summary='Legitimate summary change')
    assert w.progress.current()[2]['board']['tasks'] == [task('Confirmed', 'pending')]
    [entry] = w.history.read()['tasks']
    assert entry['onBoard'] and entry['title'] == 'Confirmed'
    assert entry['progress'] == 'Never committed'
    assert 'Candidate alias' in entry['formerTitles']
