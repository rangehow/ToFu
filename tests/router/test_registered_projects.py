"""lib/router/registered_projects: the one answer to "is this a registered project root?".

Folder roots (primary or not) and coordination bindings register a project;
the router home, the Watchdog and the recent-project navigation history do not.
Every admitting surface (channel attachments, the Feishu bridge) asks here.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from lib.conversations.project_coordination import NAMESPACE
from lib.router.registered_projects import registered_project, registered_project_roots

pytestmark = pytest.mark.unit

OWNER = 7


class _Bindings:
    """``record.get``/``record.list`` over coordination bindings only."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def __call__(self, write=False):
        return self

    def bind(self, path: str, conversation_id: str = 'coordinator-x', owner: int = OWNER):
        key = f'{owner}:{hashlib.sha256(path.encode()).hexdigest()}'
        self.rows[key] = {'value': {'user_id': owner, 'projectPath': path,
                                    'conversationId': conversation_id}, 'version': 1}

    def query(self, operation, payload, deadline=None):
        assert payload['namespace'] == NAMESPACE
        if operation == 'record.get':
            return self.rows.get(payload['key'])
        assert operation == 'record.list'
        keys = sorted(k for k in self.rows if k.startswith(payload['prefix'])
                      and (not payload['after_key'] or k > payload['after_key']))
        return [{'key': k, **self.rows[k]} for k in keys[:payload['limit']]]


@pytest.fixture
def owner_state(monkeypatch, tmp_path):
    folders = tmp_path / 'folders.json'
    bindings = _Bindings()
    home, watchdog = tmp_path / 'home', tmp_path / 'watchdog'
    monkeypatch.setattr('lib.conversations.folders.folder_store_path', lambda: str(folders))
    monkeypatch.setattr('lib.storage.get_storage_client', bindings)
    monkeypatch.setattr('lib.router.home.router_home_location', lambda user_id: str(home))
    monkeypatch.setattr('lib.router.watchdog.binding.watchdog_location', lambda user_id: str(watchdog))
    # Navigation history is never a registration, whatever it holds.
    monkeypatch.setattr('lib.project_mod.config.get_recent_projects',
                        lambda *, user_id: [{'path': str(tmp_path / 'recent')}])

    def folder_rows(rows):
        folders.write_text(json.dumps(rows))
    folder_rows([])
    return folder_rows, bindings, home, watchdog


def test_folder_roots_and_bindings_register_projects_in_one_list(owner_state, tmp_path):
    folder_rows, bindings, home, watchdog = owner_state
    primary, secondary, bound = (str(tmp_path / name) for name in ('primary', 'secondary', 'bound'))
    folder_rows([{'id': 'f1', 'name': 'a', 'ownerUserId': OWNER, 'projectPaths': [primary, secondary]},
                 {'id': 'f2', 'name': 'b', 'ownerUserId': OWNER, 'projectPaths': [primary]},
                 {'id': 'f3', 'name': 'x', 'ownerUserId': OWNER + 1, 'projectPaths': [str(tmp_path / 'theirs')]},
                 {'id': 'f4', 'name': 'home', 'ownerUserId': OWNER, 'projectPaths': [str(home)]}])
    bindings.bind(bound)
    bindings.bind(primary)
    bindings.bind(str(watchdog))
    bindings.bind(str(tmp_path / 'other-owner'), owner=OWNER + 1)
    assert registered_project_roots(OWNER) == [primary, secondary, bound]
    for path in (primary, secondary, bound, primary + '/'):
        assert registered_project(OWNER, path) == path.rstrip('/')
    for path in (str(home), str(watchdog), str(tmp_path / 'recent'), str(tmp_path / 'theirs'),
                 str(tmp_path / 'other-owner'), 'primary', '/', ''):
        assert registered_project(OWNER, path) is None, path


def test_a_link_to_a_folder_root_resolves_to_the_registered_spelling(owner_state, tmp_path):
    folder_rows, _, _, _ = owner_state
    root = tmp_path / 'real' / 'proj'
    root.mkdir(parents=True)
    (tmp_path / 'link').symlink_to(tmp_path / 'real')
    folder_rows([{'id': 'f1', 'name': 'p', 'ownerUserId': OWNER, 'projectPaths': [str(root)]}])
    assert registered_project(OWNER, str(tmp_path / 'link' / 'proj')) == str(root)


def test_binding_pages_are_followed_to_the_end(owner_state, tmp_path):
    from lib.project_dispatch.history import PAGE_ROWS
    _, bindings, _, _ = owner_state
    paths = [str(tmp_path / f'p{index}') for index in range(PAGE_ROWS * 2 + 1)]
    for path in paths:
        bindings.bind(path)
    assert sorted(registered_project_roots(OWNER)) == sorted(paths)
