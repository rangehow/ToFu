"""The UI's project create/settings and coordinator create bind exactly one Project Router (R12).

Every create or register — the project settings dialog (POST/PUT /api/v4/folders),
the sidebar's coordinator create, or the Global Router's router_project — goes
through lib/router/project_lifecycle.py, so a project gets its Router with it
and repeating is a no-op. Replacing an existing coordinator keeps working.
These specs run against a disposable authority and temporary folders.
"""
from __future__ import annotations

import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture
def folders_store(tmp_path, monkeypatch):
    import routes.api_v1.folders as folders_route
    monkeypatch.setattr(folders_route, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    monkeypatch.setattr(folders_route, '_notify_folders_changed', lambda **kwargs: None)
    return folders_route


def _binding(path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    return ProjectCoordinationRepository(1).read(str(path))


def _coordinators(path):
    from lib.conversations.repository import ConversationRepository
    return [row for row in ConversationRepository().list(user_id=1, include_messages=False)
            if (row.metadata.get('settings') or {}).get('projectPath') == str(path)]


def test_the_project_dialog_create_binds_one_router_and_repeating_reuses_it(flask_client, chat_sidecar,
                                                                            folders_store, tmp_path):
    path = tmp_path / 'demo'
    path.mkdir()
    created = flask_client.post('/api/v4/folders', json={'name': 'Demo', 'projectPaths': [str(path)]})
    assert created.status_code == 201, created.get_data(as_text=True)
    folder = created.get_json()
    binding = _binding(path)
    assert binding['availability'] == 'available'
    # The preset carries no new fields: the UI's read path is unchanged.
    assert 'coordinator' not in folder
    # Repeating the same dialog save records the grouping again but never a second Router.
    again = flask_client.post('/api/v4/folders', json={'name': 'Demo', 'projectPaths': [str(path)]})
    assert again.status_code == 201
    assert _binding(path)['conversationId'] == binding['conversationId']
    assert len(_coordinators(path)) == 1


def test_adding_a_root_in_project_settings_binds_its_router(flask_client, chat_sidecar,
                                                            folders_store, tmp_path):
    folder = flask_client.post('/api/v4/folders', json={'name': 'Empty'}).get_json()
    assert folder['projectPaths'] == []
    path = tmp_path / 'later'
    path.mkdir()
    saved = flask_client.put(f"/api/v4/folders/{folder['id']}",
                             json={'projectPaths': [str(path)], 'expectedRevision': folder['revision']})
    assert saved.status_code == 200, saved.get_data(as_text=True)
    assert saved.get_json()['projectPath'] == str(path)
    assert _binding(path)['availability'] == 'available'


def test_a_save_that_leaves_the_roots_alone_never_touches_coordination(flask_client, chat_sidecar,
                                                                       folders_store, tmp_path, monkeypatch):
    from lib.router.project_lifecycle import ProjectLifecycle
    calls = []
    monkeypatch.setattr(ProjectLifecycle, 'ensure_router',
                        lambda self, *args, **kwargs: calls.append(args) or ({}, False))
    folder = flask_client.post('/api/v4/folders', json={'name': 'Work'}).get_json()
    saved = flask_client.put(f"/api/v4/folders/{folder['id']}",
                             json={'collapsed': True, 'expectedRevision': folder['revision']})
    assert saved.status_code == 200 and saved.get_json()['collapsed'] is True
    assert calls == []


def test_a_preset_save_survives_a_router_storage_outage(flask_client, chat_sidecar,
                                                        folders_store, tmp_path, monkeypatch):
    from lib.storage.errors import StorageError
    monkeypatch.setattr('lib.router.forwarding._ensure_project_router',
                        lambda *args, **kwargs: (_ for _ in ()).throw(StorageError('storage_unavailable', 'down')))
    created = flask_client.post('/api/v4/folders', json={'name': 'Offline', 'projectPaths': ['/code']})
    assert created.status_code == 201, created.get_data(as_text=True)
    assert flask_client.get('/api/v4/folders').get_json()['items'][0]['name'] == 'Offline'


def test_a_coordinator_create_without_an_idempotency_key_provisions_nothing(
        flask_client, chat_sidecar, folders_store, tmp_path):
    from lib.conversations.folders import owner_folders
    folder = owner_folders(folders_store._FOLDERS_PATH).create(1, {'name': 'Keyed',
                                                                   'projectPaths': [str(tmp_path)]})
    response = flask_client.post('/api/v4/project/coordination/create',
                                 json={'path': str(tmp_path), 'folderId': folder['id'],
                                       'expectedRevision': 0})
    assert response.status_code == 400
    assert _binding(tmp_path)['availability'] == 'unbound'


def test_the_coordinator_create_provisions_once_then_replaces_only_at_the_current_revision(
        flask_client, chat_sidecar, folders_store, tmp_path):
    path = tmp_path / 'legacy'
    path.mkdir()
    # A project created before R12: its preset exists and it has no Router.
    from lib.conversations.folders import owner_folders
    folder = owner_folders(folders_store._FOLDERS_PATH).create(1, {'name': 'Legacy',
                                                                   'projectPaths': [str(path)]})
    assert _binding(path)['availability'] == 'unbound'
    endpoint = '/api/v4/project/coordination/create'

    def create(revision, key=None):
        return flask_client.post(endpoint, json={'path': str(path), 'folderId': folder['id'],
                                                 'expectedRevision': revision},
                                 headers={'Idempotency-Key': key or 'create-' + uuid.uuid4().hex})

    first = create(0)
    assert first.status_code == 200, first.get_data(as_text=True)
    binding = _binding(path)
    assert binding['availability'] == 'available'
    assert first.get_json()['conversationId'] == binding['conversationId']
    # Repeating it never mints a second coordinator for this project.
    again = create(0)
    assert again.get_json()['conversationId'] == binding['conversationId']
    assert len(_coordinators(path)) == 1
    # Replace coordinator (the workspace's Replace action) still replaces the bound one.
    replaced = create(binding['revision'])
    assert replaced.status_code == 200, replaced.get_data(as_text=True)
    assert replaced.get_json()['conversationId'] != binding['conversationId']
    assert replaced.get_json()['availability'] == 'available'
    assert len(_coordinators(path)) == 2
    current = _binding(path)
    assert current['conversationId'] == replaced.get_json()['conversationId']
    assert current['revision'] == replaced.get_json()['revision']
    # A stale replacement never rebinds: the current coordinator wins and the
    # request is idempotent, so the create returns that same binding.
    stale = create(0)
    assert stale.status_code == 200
    assert stale.get_json()['conversationId'] == current['conversationId']
    assert _binding(path)['conversationId'] == current['conversationId']
