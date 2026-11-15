"""Owner-facing memory API: edit/delete with recorded provenance, search what Tofu
knows about the owner, and ask why one item is believed.

Design record "Owner understanding", Decisions 2 and 6. A write through the
API is owner speech only when the owner acts in the Tofu UI (same-origin
browser evidence, ``auth.request_provenance``); a script or agent with a
credential is an agent report. The owner audience sees sensitive lessons,
flagged; roles never do (tests/memory/test_owner_memory_lookup.py).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.api

UI = {'Sec-Fetch-Site': 'same-origin'}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files
    import lib.memory.user_profile as up
    from lib.api_keys import local_admin_context
    from lib.identity import PERSONAL_USER_ID
    from quart import Quart, g
    from routes.api_v1.memory import api_v1_memory_bp

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    (project / '.tofu').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage, '_server_data_dir', lambda: str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()

    scope = str(PERSONAL_USER_ID)
    path = up.context_path(scope)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'items': [{
            'id': 'ctx_chinese', 'type': 'response_preference', 'text': 'Reply in Chinese wombatword',
            'source': 'manual', 'created_at': '2026-11-01T00:00:00Z',
            'updated_at': '2026-11-01T00:00:00Z'}]}, handle)

    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(api_v1_memory_bp)
    yield app, str(project)
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _call(app, method, path, **kwargs):
    async def run():
        response = await getattr(app.test_client(), method)(path, **kwargs)
        return response.status_code, await response.get_json()
    return asyncio.run(run())


def _create(app, project, name, *, headers=None):
    status, body = _call(app, 'post', f'/api/v1/memory?project_path={project}', headers=headers or {},
                         json={'name': name, 'description': f'{name} wombatword lesson',
                               'body': f'{name} body', 'scope': 'project'})
    assert status == 201, body
    return body


def test_an_owner_ui_write_is_owner_speech_and_a_script_write_is_an_agent_report(api):
    app, project = api
    assert _create(app, project, 'from the ui', headers=UI)['provenance']['source_class'] == 'owner_stated'
    assert _create(app, project, 'from a script')['provenance']['source_class'] == 'agent_report'


def test_the_owner_can_disprove_restore_and_hide_a_lesson(api):
    app, project = api
    lesson = _create(app, project, 'no ssh key')
    status, body = _call(app, 'put', f'/api/v1/memory/{lesson["id"]}?project_path={project}', headers=UI,
                         json={'status': 'disproved', 'status_reason': 'disproved',
                               'superseded_by': {'evidence': 'the deploy key works'}})
    assert status == 200, body
    assert (body['status'], body['superseded_by']) == ('disproved', {'evidence': 'the deploy key works'})
    assert body['provenance']['edited_source_class'] == 'owner_stated'

    status, body = _call(app, 'put', f'/api/v1/memory/{lesson["id"]}?project_path={project}', headers=UI,
                         json={'status': 'disproved', 'status_reason': 'disproved'})
    assert status == 400, body

    status, body = _call(app, 'put', f'/api/v1/memory/{lesson["id"]}?project_path={project}', headers=UI,
                         json={'status': 'active', 'sensitive': True})
    assert status == 200 and body['status'] == 'active' and body['sensitive'] is True

    status, body = _call(app, 'delete', f'/api/v1/memory/{lesson["id"]}?project_path={project}')
    assert status == 200 and body['deleted'] is True


def test_owner_memory_search_returns_my_context_and_lessons_with_trust(api):
    app, project = api
    hidden = _create(app, project, 'private notes', headers=UI)
    _call(app, 'put', f'/api/v1/memory/{hidden["id"]}?project_path={project}', headers=UI,
          json={'sensitive': True})
    reported = _create(app, project, 'agent finding')

    status, body = _call(app, 'get', f'/api/v1/owner-memory?query=wombatword&project_path={project}')
    assert status == 200 and body['ok'] is True
    [item] = body['context']['items']
    assert (item['id'], item['trust'], item['source_class']) == ('ctx_chinese', 'owner-stated', 'owner_stated')
    lessons = {record['id']: record for record in body['lessons']['items']}
    assert lessons[reported['id']]['trust'] == 'inferred'
    assert lessons[hidden['id']]['sensitive'] is True and lessons[hidden['id']]['trust'] == 'owner-stated'

    status, body = _call(app, 'get', '/api/v1/owner-memory?query=wombatword&top_k=many')
    assert status == 400


def test_why_explains_one_item_with_the_turns_that_applied_it(api, monkeypatch):
    app, project = api
    lesson = _create(app, project, 'gateway lesson')
    readers = []

    def turns(conversation_id, *, user_id):
        readers.append((conversation_id, user_id))
        return iter([{'turnId': 'applied', 'conversationId': conversation_id, 'createdAt': 4102444800000,
                      'projection': {'preferencesApplied': {'ids': ['ctx_chinese']}}}])
    monkeypatch.setattr('lib.turn_lifecycle.iter_turns_newest_first', turns)

    status, body = _call(app, 'get', '/api/v1/owner-memory/ctx_chinese?conversation_id=router-1')
    assert status == 200, body
    assert body['item']['trust'] == 'owner-stated'
    assert [row['turn_id'] for row in body['used_in']['turns']] == ['applied']
    from lib.identity import PERSONAL_USER_ID
    assert readers == [('router-1', PERSONAL_USER_ID)]

    status, body = _call(app, 'get', f'/api/v1/owner-memory/{lesson["id"]}?project_path={project}')
    assert status == 200 and body['item']['body'] == 'gateway lesson body' and body['used_in'] is None

    status, _body = _call(app, 'get', f'/api/v1/owner-memory/no_such_item?project_path={project}')
    assert status == 404
