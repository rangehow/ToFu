"""Every project create or register binds exactly one Project Router (docs/ROUTER.md R12).

Owner decision 2026-11-09: 「以后我们创建项目之后，就默认创建负责人吧，也不需要后面再创建」.
The backend operation is shared by the folders HTTP adapter (the project settings
dialog), the coordinator create button and the Global Router's router_project
tool. These specs use a real storage authority and isolated folders; no owner's
tree is touched and no project preparation runs here.
"""
from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

OWNER_WORDS = '总负责人要可以创建项目，设置它的默认路径啊啥的，并设置项目里的coordinator。'


@pytest.fixture
def p(semantic_authority, monkeypatch, tmp_path):
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    from lib.conversations.folders import owner_folders
    from lib.router.project_lifecycle import ProjectLifecycle
    lifecycle = ProjectLifecycle(owner, folders=owner_folders(str(tmp_path / 'folders.json')))
    state = SimpleNamespace(owner=owner, root=tmp_path, lifecycle=lifecycle, client=client)

    def binding(path):
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        return ProjectCoordinationRepository(owner).read(str(path))

    def settings(path):
        from lib.conversations.repository import ConversationRepository
        target = ConversationRepository(client).get(binding(path)['conversationId'], user_id=owner,
                                                    include_messages=False)
        return dict(target.metadata['settings'])

    def coordinators(path):
        from lib.conversations.repository import ConversationRepository
        return [row for row in ConversationRepository(client).list(user_id=owner, include_messages=False)
                if (row.metadata.get('settings') or {}).get('projectPath') == str(path)]

    def forward(path):
        from lib.router.forwarding import _ensure_project_router
        return _ensure_project_router(owner, str(path), {'searchMode': 'multi'})

    state.binding, state.settings, state.coordinators, state.forward = binding, settings, coordinators, forward
    state.new_project = lambda name='demo', **kw: lifecycle.create(parent=str(tmp_path), folder_name=name, **kw)
    state.project_path = lambda name='demo': tmp_path / name
    return state


def test_a_created_project_gets_one_bound_router_and_leaves_the_folder_empty(p):
    created = p.new_project()
    path = p.project_path()
    assert created['project']['projectPaths'] == [str(path)]
    assert created['coordinator']['created'] is True
    binding = p.binding(path)
    assert binding['availability'] == 'available'
    assert created['coordinator']['conversationId'] == binding['conversationId']
    stored = p.settings(path)
    assert stored['projectPath'] == str(path) and stored['folderId'] == created['project']['id']
    # Creating a project writes only its folder and Tofu's standard project preparation:
    # this folder is not a repository yet, so nothing at all was written inside it.
    assert list(path.iterdir()) == []
    assert len(p.coordinators(path)) == 1


def test_a_second_create_of_the_same_folder_is_refused_and_creates_no_router(p):
    first = p.new_project()
    with pytest.raises(ValueError, match='Already exists'):
        p.new_project()
    assert len(p.lifecycle.list()) == 1
    assert p.binding(p.project_path())['conversationId'] == first['coordinator']['conversationId']
    assert len(p.coordinators(p.project_path())) == 1


def test_registering_a_folder_leaves_its_contents_alone_and_reuses_one_router(p):
    root = p.root / 'existing'
    root.mkdir()
    (root / 'notes.txt').write_text('keep me')
    first = p.lifecycle.register(path=str(root), name='Existing')
    second = p.lifecycle.register(path=str(root), name='Existing again')
    assert (root / 'notes.txt').read_text() == 'keep me'
    # Two groupings of one root are allowed in the UI, and share its one Router.
    assert first['project']['id'] != second['project']['id']
    assert first['coordinator']['conversationId'] == second['coordinator']['conversationId']
    assert second['coordinator']['created'] is False
    assert len(p.coordinators(root)) == 1


def test_registering_a_missing_directory_is_refused_without_effects(p):
    with pytest.raises(ValueError, match='Directory not found'):
        p.lifecycle.register(path=str(p.root / 'absent'), name='Absent')
    assert p.lifecycle.list() == []


def test_a_forward_and_a_project_create_converge_on_one_router(p):
    created = p.new_project()
    forwarded, created_again = p.forward(p.project_path())
    assert created_again is False
    assert forwarded['conversationId'] == created['coordinator']['conversationId']
    # The other order is just as idempotent: the forward provisions, the create reuses.
    other = p.root / 'forwarded-first'
    other.mkdir()
    binding, was_created = p.forward(other)
    assert was_created is True
    admitted = p.lifecycle.register(path=str(other), name='Forwarded')
    assert admitted['coordinator']['created'] is False
    assert admitted['coordinator']['conversationId'] == binding['conversationId']


def test_a_new_primary_root_gets_its_router_and_the_old_one_keeps_its_own(p):
    created = p.new_project()
    folder = created['project']
    other = p.root / 'other-root'
    other.mkdir()
    updated = p.lifecycle.update_project(folder['id'], {
        'projectPaths': [str(other)], 'readOnlyPaths': [str(other)], 'expectedRevision': folder['revision']})
    assert updated['project']['projectPath'] == str(other)
    assert p.binding(other)['availability'] == 'available'
    assert p.binding(folder['projectPath'])['conversationId'] == created['coordinator']['conversationId']
    assert updated['coordinator']['created'] is True


def test_a_defaults_change_that_leaves_the_roots_alone_does_not_touch_coordination(p, monkeypatch):
    created = p.new_project()
    calls = []
    monkeypatch.setattr(p.lifecycle, 'ensure_router',
                        lambda *args, **kwargs: calls.append(args) or p.binding(p.project_path()))
    updated = p.lifecycle.update_project(created['project']['id'], {'name': 'Renamed', 'collapsed': True})
    assert updated['project']['name'] == 'Renamed' and updated['project']['collapsed'] is True
    assert calls == []


def test_configuring_the_router_writes_what_the_ui_reads_and_keeps_every_other_setting(p):
    created = p.new_project()
    path = p.project_path()
    from lib.conv_config import resolve_conv_settings
    from lib.conversations.settings_store import update_conversation_settings
    seeded = resolve_conv_settings(conv_settings={'projectPath': str(path), 'folderId': created['project']['id'],
                                                  'searchMode': 'single', 'autoApply': False})
    assert update_conversation_settings(created['coordinator']['conversationId'],
                                        lambda stored: stored.update(seeded), user_id=p.owner) is not None
    result = p.lifecycle.configure_router(str(path), settings={'thinkingDepth': 'high',
                                                               'codeExecEnabled': True, 'modelRef': None})
    assert result['settings']['thinkingDepth'] == 'high' and result['settings']['codeExecEnabled'] is True
    stored = p.settings(path)
    assert stored['thinkingDepth'] == 'high' and stored['codeExecEnabled'] is True
    assert stored['searchMode'] == 'single' and stored['autoApply'] is False
    assert stored['projectPath'] == str(path) and stored['folderId'] == created['project']['id']
    assert p.binding(path)['availability'] == 'available'
    # A later unrelated change keeps the earlier choices.
    p.lifecycle.configure_router(str(path), settings={'fetchEnabled': True})
    assert p.settings(path)['thinkingDepth'] == 'high'
    assert result['created'] is False


def test_a_new_model_name_replaces_the_routed_reference_and_must_be_routable(p, monkeypatch):
    created = p.new_project()
    path = p.project_path()
    from lib.conversations.settings_store import update_conversation_settings
    update_conversation_settings(created['coordinator']['conversationId'],
                                 lambda stored: stored.update({'modelRef': {'creator_id': 'c', 'model_id': 'old'}}),
                                 user_id=p.owner)
    import lib.model_routing as routing
    monkeypatch.setattr(routing, 'ModelRoutingRepository', lambda: SimpleNamespace(
        get=lambda boundary: SimpleNamespace(document={})))
    monkeypatch.setattr(routing, 'resolve_compatible_model', lambda document, model: 'selection')
    result = p.lifecycle.configure_router(str(path), settings={'model': 'known-model'})
    assert result['settings']['model'] == 'known-model'
    assert p.settings(path)['modelRef'] is None
    def refuse(document, model):
        raise routing.ModelRoutingError('unknown model', field='model')
    monkeypatch.setattr(routing, 'resolve_compatible_model', refuse)
    with pytest.raises(ValueError, match='not routable'):
        p.lifecycle.configure_router(str(path), settings={'model': 'made-up-model'})
    assert p.settings(path)['model'] == 'known-model'


def test_the_router_worker_model_uses_the_worker_model_backend(p):
    created = p.new_project()
    path = p.project_path()
    p.lifecycle.configure_router(str(path), worker_model='')
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    assert ProjectCoordinationRepository(p.owner).worker_model(str(path)) == ''
    stored = p.settings(path)
    assert stored['projectPath'] == str(path) and p.binding(path)['availability'] == 'available'


def test_workspace_pins_and_unknown_settings_are_refused_without_writing(p):
    created = p.new_project()
    before = p.settings(p.project_path())
    for settings in ({'projectPath': '/elsewhere'}, {'folderId': 'f_other'}, {'notASetting': True},
                     {'thinkingDepth': 'extreme'}):
        with pytest.raises(ValueError):
            p.lifecycle.configure_router(str(p.project_path()), settings=settings)
    assert p.settings(p.project_path()) == before


def test_a_router_that_cannot_be_provisioned_does_not_undo_the_owner_save(p, monkeypatch):
    from lib.storage.errors import StorageError
    monkeypatch.setattr(p.lifecycle, 'ensure_router',
                        lambda *args, **kwargs: (_ for _ in ()).throw(StorageError('storage_unavailable', 'down')))
    created = p.new_project()
    assert created['project']['name'] == 'demo'
    assert created['coordinator'] is None
    assert 'down' in created['coordinator_deferred']
    assert len(p.lifecycle.list()) == 1


def test_a_project_without_a_root_is_a_grouping_and_reading_it_stays_passive(p):
    created = p.lifecycle.save_project({'name': 'Grouping only'})
    assert created['coordinator'] is None and created['project']['projectPaths'] == []
    read = p.lifecycle.get(created['project']['id'])
    assert read['coordinator'] is None and read['name'] == 'Grouping only'
    assert [row['id'] for row in p.lifecycle.list()] == [created['project']['id']]
    with pytest.raises(ValueError, match='does not exist'):
        p.lifecycle.get('f_' + uuid.uuid4().hex)


def test_reading_the_router_reports_only_the_composer_settable_settings(p):
    created = p.new_project()
    path = p.project_path()
    p.lifecycle.configure_router(str(path), settings={'thinkingDepth': 'medium'})
    view = p.lifecycle.read_router(str(path))
    assert view['availability'] == 'available' and view['workerModel'] == ''
    assert view['settings']['thinkingDepth'] == 'medium'
    assert 'projectPath' not in view['settings'] and 'folderId' not in view['settings']
    assert 'workerModel' not in view['settings']
