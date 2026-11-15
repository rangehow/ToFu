"""router_project: the Global Router's project lifecycle tool (docs/ROUTER.md R12).

Every write acts under the owner's delegation (R9, scope project_lifecycle):
the owner's trigger and one audit record precede the effect, a retry never
applies twice, and the effect is the very operation the UI's folders adapter
calls. Reads need no grant. Projects are never deleted or unregistered.
The trigger alone is stubbed (its own authority specs live in test_owner_delegation);
projects, coordinators and settings run over a real storage authority.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import lifecycle_approval as gate
from lib.lifecycle_approval import DelegationRefused
from lib.router import forwarding
from lib.router.project_lifecycle import DELEGATION_SCOPE, ProjectLifecycle, router_project
from tests.router.test_owner_delegation import human
from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

OWNER_WORDS = '总负责人要可以创建项目，设置它的默认路径啊啥的，并设置项目里的coordinator。'


@pytest.fixture
def r(semantic_authority, monkeypatch, tmp_path):
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path))
    from lib.identity import PrincipalContext
    from lib.router.home import router_home_location
    state = SimpleNamespace(owner=owner, root=tmp_path)
    state.scope = {'user_id': owner, 'project_path': router_home_location(owner),
                   'conversation_id': 'router', 'binding_revision': 1}
    state.task = {'id': 'router-task', 'convId': 'router', '_turnId': 'router-turn',
                  '_attemptId': 'router-attempt', 'config': {},
                  '_principalContext': PrincipalContext.user(subject_id='owner',
                                                             owner_user_id=owner).to_payload()}
    turns = {'owner-turn': {'conversationId': 'router', 'turnId': 'owner-turn', 'actor': 'human',
                            'projectionRevision': 1,
                            'projection': {'content': 'create the project', 'originalContent': OWNER_WORDS}},
             'report-turn': {'conversationId': 'router', 'turnId': 'report-turn', 'actor': 'assistant',
                             'projectionRevision': 1, 'projection': {'content': 'a report'}}}
    monkeypatch.setattr(forwarding, '_router_turn',
                        lambda o, c, t: dict(turns[t]) if t in turns else (_ for _ in ()).throw(
                            ValueError(f'Turn {t} does not exist')))
    monkeypatch.setattr(forwarding, '_current_request_source',
                        lambda task, scope: {'conversationId': 'router', 'turnId': 'owner-turn',
                                             'projectionRevision': 1})
    monkeypatch.setattr('lib.router.owner_delegation._live_router_attempt', lambda task, scope: None)
    from lib.conversations.folders import owner_folders
    state.lifecycle = ProjectLifecycle(owner, folders=owner_folders(str(tmp_path / 'folders.json')))
    monkeypatch.setattr('lib.router.project_lifecycle._lifecycle', lambda o: state.lifecycle)
    state.grant = lambda: gate.set_delegation(owner, True, grantee={
        'kind': 'global_router', 'conversationId': 'router', 'bindingRevision': 1, 'tenant': None},
        provenance=human(owner=owner))
    state.revoke = lambda: gate.set_delegation(owner, False, grantee=None, provenance={
        'kind': 'authenticated-request', 'ownerUserId': owner, 'credential': 'header'})

    def call(action, **arguments):
        return router_project(state.task, state.scope, {'action': action, **arguments})

    def write(action, **arguments):
        arguments.setdefault('reason', 'The owner asked for this project change')
        arguments.setdefault('source_turn_id', 'owner-turn')
        arguments.setdefault('idempotency_key', action + '-1')
        return call(action, **arguments)

    def binding(path):
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        return ProjectCoordinationRepository(owner).read(str(path))

    state.call, state.write, state.binding = call, write, binding
    state.audit = lambda: gate.delegated_actions(owner)
    state.ui_folders = lambda: owner_folders(str(tmp_path / 'folders.json')).list(owner)
    state.read_settings = lambda path: state.lifecycle.read_router(str(path))['settings']
    return state


def test_create_runs_the_same_backend_operation_as_the_ui_and_is_audited(r, monkeypatch):
    calls = []
    original = ProjectLifecycle.save_project

    def observed(self, fields):
        calls.append(dict(fields))
        return original(self, fields)

    monkeypatch.setattr(ProjectLifecycle, 'save_project', observed)
    # The UI's folders adapter constructs this same owner and calls that same method.
    import routes.api_v1.folders as folders_route
    assert isinstance(folders_route._lifecycle(r.owner), ProjectLifecycle)
    r.grant()
    result = r.write('create', parent=str(r.root), folder_name='demo')
    assert len(calls) == 1 and calls[0]['projectPaths'] == [str(r.root / 'demo')]
    assert result['project']['name'] == 'demo'
    assert result['coordinator']['availability'] == 'available'
    assert result['owner_delegation_action_id']
    [record] = r.audit()
    assert record['scope'] == DELEGATION_SCOPE and record['status'] == 'completed'
    assert record['trigger']['kind'] == 'owner_message' and record['trigger']['text'] == OWNER_WORDS
    assert record['target'] == {'kind': 'project', 'action': 'create', 'path': str(r.root / 'demo')}
    # The owner's "Act for me" read renders that record, with its delegation.
    # (active requires the router's actual conversation binding, which is absent
    # in this stubbed fixture; the grant and the audit record are what R12 owns.)
    from lib.router.owner_delegation import owner_delegation_state
    state = owner_delegation_state(r.owner)
    assert state['grant']['state'] == 'granted'
    assert state['actions'][0]['id'] == record['id']
    assert 'project_lifecycle' in state['grant']['scopes']


def test_a_retry_with_the_same_key_never_creates_twice_and_reads_the_state_back(r):
    r.grant()
    first = r.write('create', parent=str(r.root), folder_name='demo')
    second = r.write('create', parent=str(r.root), folder_name='demo')
    assert second['owner_delegation_action_id'] == first['owner_delegation_action_id']
    assert second['replayed'] is True and second['action_status'] == 'completed'
    assert second['interrupted'] is False
    assert second['coordinator']['conversationId'] == first['coordinator']['conversationId']
    assert len(r.ui_folders()) == 1 and len(r.audit()) == 1


def test_without_the_grant_a_write_is_a_typed_refusal_with_no_effects(r):
    with pytest.raises(DelegationRefused) as refused:
        r.write('create', parent=str(r.root), folder_name='demo')
    assert refused.value.code == 'owner_delegation_not_granted'
    assert r.ui_folders() == [] and r.audit() == []


def test_revoking_the_grant_stops_the_next_change(r):
    r.grant()
    r.write('create', parent=str(r.root), folder_name='demo')
    r.revoke()
    folder = r.ui_folders()[0]
    with pytest.raises(DelegationRefused):
        r.write('update_defaults', folder_id=folder['id'], defaults={'name': 'Renamed'},
                idempotency_key='rename-1')
    assert r.ui_folders()[0]['name'] == 'demo'


def test_a_synthetic_turn_without_owner_words_cannot_act(r):
    r.grant()
    with pytest.raises(DelegationRefused) as refused:
        r.write('create', parent=str(r.root), folder_name='demo', source_turn_id='report-turn')
    assert refused.value.code == 'owner_delegation_trigger_invalid'
    assert r.ui_folders() == []
    # The refusal itself is recorded for the owner, with no effect.
    assert [record['status'] for record in r.audit()] == ['refused']


def test_defaults_changed_by_the_router_read_back_identically_in_the_ui_read_path(r):
    r.grant()
    created = r.write('create', parent=str(r.root), folder_name='demo')
    folder = created['project']
    r.write('update_defaults', folder_id=folder['id'], idempotency_key='defaults-1',
            expected_revision=folder['revision'],
            defaults={'name': 'Renamed', 'color': '#a1b2c3', 'collapsed': True,
                      'readOnlyPaths': [str(r.root / 'demo')]})
    [read] = r.ui_folders()
    assert read['name'] == 'Renamed' and read['color'] == '#a1b2c3' and read['collapsed'] is True
    assert read['readOnlyPaths'] == [str(r.root / 'demo')]
    assert read['revision'] == folder['revision'] + 1
    # The dialog's own stale-draft refusal is unchanged.
    from lib.conversations.folders import FolderRevisionConflict
    with pytest.raises(FolderRevisionConflict):
        r.write('update_defaults', folder_id=folder['id'], defaults={'name': 'stale'},
                expected_revision=folder['revision'], idempotency_key='stale-1')


def test_configuring_the_router_goes_through_the_composer_resolver_and_is_audited(r, monkeypatch):
    r.grant()
    created = r.write('create', parent=str(r.root), folder_name='demo')
    calls = []
    import lib.conv_config as conv_config
    original = conv_config.resolve_conv_settings

    def observed(*, conv_settings=None, overrides=None):
        calls.append(dict(conv_settings or {}))
        return original(conv_settings=conv_settings, overrides=overrides)

    monkeypatch.setattr(conv_config, 'resolve_conv_settings', observed)
    (r.root / 'demo').mkdir(exist_ok=True)
    result = r.write('configure_router', project_path=str(r.root / 'demo'), idempotency_key='configure-1',
                     settings={'thinkingDepth': 'high', 'codeExecEnabled': True})
    assert result['coordinator']['settings']['thinkingDepth'] == 'high'
    assert result['coordinator']['settings']['codeExecEnabled'] is True
    assert r.read_settings(r.root / 'demo')['thinkingDepth'] == 'high'
    assert len(calls) == 1
    assert calls[0]['thinkingDepth'] == 'high' and calls[0]['projectPath'] == str(r.root / 'demo')
    assert calls[0]['folderId'] == created['project']['id']
    [record] = [row for row in r.audit() if row['target'].get('action') == 'configure_router']
    assert record['scope'] == DELEGATION_SCOPE and record['status'] == 'completed'


def test_an_unknown_setting_is_refused_before_the_grant(r):
    r.grant()
    (r.root / 'demo').mkdir()
    with pytest.raises(ValueError, match='Not a Project Router setting'):
        r.write('configure_router', project_path=str(r.root / 'demo'),
                settings={'projectPath': '/elsewhere'})
    assert r.audit() == []


def test_reads_are_passive_and_need_no_grant(r):
    assert r.call('list')['projects'] == []
    r.grant()
    created = r.write('create', parent=str(r.root), folder_name='demo')
    listed = r.call('list')['projects']
    assert [row['id'] for row in listed] == [created['project']['id']]
    assert listed[0]['coordinator']['availability'] == 'available'
    read = r.call('get', folder_id=created['project']['id'])
    assert read['coordinator']['conversationId'] == created['coordinator']['conversationId']
    assert len(r.audit()) == 1  # only the create was audited, never a read


def test_projects_are_never_deleted_or_unregistered(r):
    from lib.router.contract import CONTRACT
    actions = CONTRACT['$defs']['routerProject']['properties']['action']['enum']
    assert actions == ['list', 'get', 'create', 'register', 'update_defaults', 'configure_router']
    for action in ('delete', 'unregister', 'remove', 'rmdir'):
        with pytest.raises(ValueError):
            r.write(action, folder_id='f_1')


def test_the_router_home_and_watchdog_are_not_projects(r):
    from lib.router.home import router_home_location
    r.grant()
    for path in (router_home_location(r.owner), str(r.root / 'absent')):
        with pytest.raises(ValueError):
            r.write('register', project_path=path)
    assert r.ui_folders() == []


def test_a_write_requires_a_reason_and_an_idempotency_key(r):
    r.grant()
    for missing in ('reason', 'idempotency_key'):
        arguments = {'action': 'create', 'parent': str(r.root), 'folder_name': 'demo',
                     'reason': 'owner asked', 'source_turn_id': 'owner-turn', 'idempotency_key': 'create-1'}
        arguments.pop(missing)
        with pytest.raises(ValueError, match=missing):
            r.call(**arguments)
    assert r.ui_folders() == []
