"""An isolated Worker's project scope is its canonical project, not its checkout.

Incident 2026-11-10: Workers run in task-owned worktrees, and the
conversation-reference handler scoped ``list_conversations(scope=project)`` to
that checkout, so RESTART-ANOMALIES could not find RECOVERY-SOURCE by title and
DX-RECONNECT reported DX-BRIDGE-RECOVER's conversation "not visible from my
project scope". These specs drive the real tool handler over real authorities.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]


@pytest.fixture
def world(semantic_authority, monkeypatch, tmp_path):
    client, owner, engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    project, other = str(tmp_path / 'project'), str(tmp_path / 'other')

    def create(conversation_id, title, project_path, *, user_id=owner):
        client(write=True).command('conversation.create', {
            'conv_id': conversation_id, 'user_id': user_id, 'title': title,
            'settings': {'projectPath': project_path},
        }, 'create-' + conversation_id)

    create('coordinator-alpha', 'Project Coordinator', project)
    create('worker-self', '[RESTART-ANOMALIES] what the restart left', project)
    create('worker-sibling', '[RECOVERY-SOURCE] recovery KeyError after restart', project)
    create('worker-elsewhere', '[RECOVERY-SOURCE] another project', other)
    # A native authority serves exactly one owner; SQLite hosts several.
    foreign = engine is None
    if foreign:
        create('worker-foreign', '[RECOVERY-SOURCE] another tenant', project, user_id=owner + 1)
    checkout = str(tmp_path / 'worktrees' / 'worker-self')
    task = {'id': 'task-self', 'convId': 'worker-self', '_userId': owner,
            'config': {'projectPath': checkout, 'canonicalProjectPath': project}}
    # The search projection follows writes asynchronously (native opens it lazily).
    from tests._seed import wait_for_conversation_search
    wait_for_conversation_search('worker-sibling', user_id=owner, expected_ids=('worker-sibling',),
                                 timeout_s=60.0, client=client(write=False))
    return SimpleNamespace(project=project, checkout=checkout, task=task, foreign=foreign)


def _list(world, **arguments):
    from lib.tasks_pkg.handlers.misc._brain import _handle_conv_ref_tool
    task = world.task
    with patch('lib.tasks_pkg.handlers._adapter._finalize_tool_round'), \
            patch('lib.tasks_pkg.handlers._adapter._build_simple_meta', return_value={}):
        _, content, _ = _handle_conv_ref_tool(
            task, {}, 'list_conversations', 'call-1', arguments, 0, {},
            task['config'], task['config']['projectPath'], True)
    return content


def _listed(content) -> set[str]:
    return {item['id'] for item in content.conversation_search['items']}


def test_worker_in_its_checkout_lists_its_siblings_and_coordinator(world):
    content = _list(world, scope='project')
    assert _listed(content) == {'coordinator-alpha', 'worker-sibling'}
    assert content.conversation_search['projectPath'] == world.project
    assert f'in this project ({world.project})' in content
    assert world.checkout not in content
    assert 'The current conversation [worker-self] is not listed' in content


@pytest.mark.parametrize('keyword', ['RECOVERY-SOURCE', 'worker-sibling'])
def test_worker_finds_a_sibling_by_title_and_by_id(world, keyword):
    # Default scope 'auto' is the project scope in project mode.
    content = _list(world, keyword=keyword)
    assert _listed(content) == {'worker-sibling'}
    assert '• [worker-sibling]' in content


def test_project_scope_never_lists_other_projects_or_tenants(world):
    for arguments in ({'scope': 'project'}, {'keyword': 'RECOVERY-SOURCE'}):
        listed = _listed(_list(world, **arguments))
        assert 'worker-elsewhere' not in listed
        assert 'worker-foreign' not in listed
    # The other project's conversation exists for this owner; only scope hides it.
    assert 'worker-elsewhere' in _listed(_list(world, keyword='RECOVERY-SOURCE', scope='all'))
    if world.foreign:
        assert 'worker-foreign' not in _listed(_list(world, keyword='RECOVERY-SOURCE', scope='all'))
