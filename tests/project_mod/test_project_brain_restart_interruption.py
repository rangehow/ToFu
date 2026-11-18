"""A restart drain is ``interrupted`` work, never a Stop, and old mislabels repair once."""

from __future__ import annotations

import uuid

import pytest


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def _project(prefix: str) -> str:
    return f'/tmp/{prefix}-{uuid.uuid4().hex}'


def _worker(conv_id: str, project: str, **evidence) -> dict:
    from lib.conversations.project_brain import note_todo_signal

    task = {'id': 'task-' + uuid.uuid4().hex, 'convId': conv_id, '_userId': 1,
            'config': {'projectPath': project}, 'lastUserQuery': 'Work'}
    note_todo_signal(task, project, [{'content': 'Audit', 'status': 'in_progress'}],
                     accepted=True)
    task.update(evidence)
    return task


@pytest.fixture
def lead(monkeypatch):
    from lib.conversations.project_coordination import ProjectCoordinationRepository

    monkeypatch.setattr(ProjectCoordinationRepository, 'read', lambda _repo, project: {
        'projectPath': project, 'conversationId': 'conv-lead', 'availability': 'available'})
    monkeypatch.setattr('lib.conversations.repository.list_conversations',
                        lambda **_kwargs: [])


def test_a_restart_drained_worker_is_interrupted_and_a_stop_stays_cancelled(lead):
    from lib.conversations.project_brain import (
        board_projection, prepare_project_context, settle_work_item,
    )

    project = _project('restart-drain')
    viewer = {'_userId': 1}
    assert prepare_project_context(project, 'conv-lead', user_id=1, task=viewer) == ''

    drained = _worker('conv-drained', project, aborted=True,
                      _abort_reason='server_shutdown', status='aborted')
    stopped = _worker('conv-stopped', project, aborted=True, status='aborted')
    assert settle_work_item(drained, project) == 'interrupted'
    assert settle_work_item(stopped, project) == 'cancelled'

    statuses = {item['conversationId']: item['status']
                for item in board_projection(project, user_id=1)['recentOutcomes']}
    assert statuses == {'conv-drained': 'interrupted', 'conv-stopped': 'cancelled'}
    feed = prepare_project_context(project, 'conv-lead', user_id=1, task=viewer)
    assert '(conv-drained) 已中断·重启后继续' in feed
    assert '(conv-stopped) 已取消' in feed


def test_boot_repair_relabels_only_restart_drained_cancels_once(lead, monkeypatch):
    import lib.conversations.project_brain_startup as startup
    from lib.conversations.project_brain import (
        board_projection, prepare_project_context, settle_work_item,
    )

    project = _project('restart-repair')
    viewer = {'_userId': 1}
    assert prepare_project_context(project, 'conv-lead', user_id=1, task=viewer) == ''
    # Work recorded before settlement was restart-aware: both read 'cancelled'.
    with monkeypatch.context() as legacy:
        legacy.setattr('lib.tasks_pkg.workspace_delivery.workspace_outcome',
                       lambda _task: 'cancelled')
        drained = _worker('conv-drained', project, aborted=True,
                          _abort_reason='server_shutdown', status='aborted')
        stopped = _worker('conv-stopped', project, aborted=True, status='aborted')
        assert settle_work_item(drained, project) == 'cancelled'
        assert settle_work_item(stopped, project) == 'cancelled'

    # Each work item's own task result: stored metadata names the abort cause.
    results = {
        drained['id']: {'status': 'aborted', 'metadata': {
            'aborted': True, '_abort_reason': 'server_shutdown'}},
        stopped['id']: {'status': 'aborted', 'metadata': {'aborted': True}},
    }
    reads = []

    def query_many(_client, requests):
        reads.extend(payload['key'] for _operation, payload in requests)
        return [results.get(payload['key']) for _operation, payload in requests]

    monkeypatch.setattr(startup, 'query_many', query_many)
    assert startup.repair_restart_cancelled_work_items() == 1
    statuses = {item['conversationId']: item['status']
                for item in board_projection(project, user_id=1)['recentOutcomes']}
    assert statuses == {'conv-drained': 'interrupted', 'conv-stopped': 'cancelled'}
    feed = prepare_project_context(project, 'conv-lead', user_id=1, task=viewer)
    assert '(conv-drained) 已中断·重启后继续' in feed
    assert '(conv-stopped) 已取消' in feed
    assert feed.count('conv-drained') == 1

    # The completed walk is recorded: the next boot reads no task results.
    reads.clear()
    assert startup.repair_restart_cancelled_work_items() == 0
    assert reads == []


def test_a_store_that_predates_interrupted_records_the_drain_as_cancelled(lead, monkeypatch):
    """Engines deploy only once certified: until then the drain must still settle."""
    import lib.conversations.project_brain as brain

    project = _project('restart-old-store')
    drained = _worker('conv-drained', project, aborted=True,
                      _abort_reason='server_shutdown', status='aborted')
    real_command = brain._command
    sent = []

    def old_store(operation, project_path, **kwargs):
        sent.append(kwargs.get('status'))
        if operation == 'project_brain.work.finish' and kwargs.get('status') == 'interrupted':
            raise RuntimeError('Invalid Project work terminal status')
        return real_command(operation, project_path, **kwargs)

    monkeypatch.setattr(brain, '_command', old_store)
    assert brain.settle_work_item(drained, project) == 'cancelled'
    assert sent == ['interrupted', 'cancelled']
    statuses = {item['conversationId']: item['status']
                for item in brain.board_projection(project, user_id=1)['recentOutcomes']}
    assert statuses == {'conv-drained': 'cancelled'}
