"""Read-only owner-memory lookup: every role can ask what Tofu knows about the
owner, with each item's trust class and provenance, and why it is believed.

Design record "Owner understanding", Decisions 2, 3, 4 and 7: My Context items
in every lifecycle state and lessons including disproved ones; delegated roles
never see identity items; no role ever sees a sensitive lesson; "why" names the
supersession chain and the Turns whose context applied the item.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit

SCOPE = '7'
STAMP = '2026-11-01T00:00:00Z'
AGENT = {'source_class': 'agent_report', 'conversation_id': 'conv-w', 'turn_id': 'turn-w',
         'at': '2026-11-02T00:00:00Z'}


def _item(item_id, text, *, item_type='response_preference', source='manual', **lifecycle):
    return {'id': item_id, 'type': item_type, 'text': text, 'source': source,
            'created_at': STAMP, 'updated_at': STAMP, **lifecycle}


@pytest.fixture()
def owner_store(tmp_path, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files
    import lib.memory.user_profile as up
    from lib.memory.storage import create_memory, update_memory

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    (project / '.tofu').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage, '_server_data_dir', lambda: str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()

    path = up.context_path(SCOPE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'items': [
            _item('ctx_chinese', 'Reply in Chinese quokkatoken',
                  provenance={'source_class': 'owner_stated', 'turn_id': 'turn-o',
                              'conversation_id': 'router'}),
            _item('ctx_audited', 'Keep reports short quokkatoken', source='legacy_migration',
                  provenance={'source_class': 'legacy_import'}),
            _item('ctx_identity', 'The owner researches agents quokkatoken', item_type='identity'),
        ], 'inactive_items': [
            _item('ctx_learned', 'Prefers tables quokkatoken', source='assistant',
                  status='needs_confirmation', status_reason='needs_owner_confirmation'),
            _item('ctx_english', 'Reply in English quokkatoken', status='superseded',
                  status_reason='owner_revoked', superseded_by={'item_id': 'ctx_chinese'}),
        ]}, handle)

    root = str(project)
    replacement = create_memory('ssh deploy key works', 'deploy key works quokkatoken',
                                'verified with git ls-remote', scope='project',
                                project_path=root, provenance=AGENT)
    stale = create_memory('no ssh key on this host', 'host has no ssh key quokkatoken',
                          'claimed before the key was added', scope='project',
                          project_path=root, provenance=AGENT)
    update_memory(stale['id'], {'status': 'disproved', 'status_reason': 'disproved',
                                'superseded_by': {'item_id': replacement['id']}},
                  project_path=root, provenance=AGENT)
    private = create_memory('owner recovery notes', 'personal recovery quokkatoken',
                            'private medical details', scope='project', project_path=root,
                            provenance=AGENT)
    update_memory(private['id'], {'sensitive': True}, project_path=root,
                  provenance={'source_class': 'owner_stated', 'at': '2026-11-03T00:00:00Z'})
    (project / '.tofu' / 'memories' / 'legacy_lesson.md').write_text(
        '---\nname: legacy lesson\ndescription: legacy quokkatoken lesson\nenabled: true\n'
        'created: 2026-07-01T00:00:00Z\n---\n\nlegacy body\n', encoding='utf-8')
    yield {'root': root, 'replacement': replacement['id'], 'stale': stale['id'],
           'private': private['id']}
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _search(store, **audience):
    from lib.memory.owner_memory import search_owner_memory
    return search_owner_memory('quokkatoken', scope=SCOPE, project_path=store['root'],
                               top_k=10, **audience)


def _trust(records):
    return {record['id']: record['trust'] for record in records}


def test_the_lookup_returns_every_my_context_state_and_lessons_with_trust(owner_store):
    from lib.memory.owner_memory import TRUST_LEGEND, format_owner_search

    result = _search(owner_store)
    context, lessons = result['context'], result['lessons']
    assert _trust(context['items']) == {
        'ctx_chinese': 'owner-stated', 'ctx_audited': 'verified', 'ctx_identity': 'owner-stated'}
    assert _trust(context['inactive_items']) == {
        'ctx_learned': 'inferred', 'ctx_english': 'disproved'}
    assert _trust(lessons['items']) == {
        owner_store['replacement']: 'inferred', 'legacy_lesson': 'inferred'}
    assert _trust(lessons['inactive_items']) == {owner_store['stale']: 'disproved'}
    assert lessons['sensitive_withheld'] == 1

    text = format_owner_search(result)
    assert TRUST_LEGEND in text
    for item_id, trust in {'ctx_chinese': 'OWNER-STATED', 'ctx_audited': 'VERIFIED',
                           'ctx_learned': 'INFERRED', 'ctx_english': 'DISPROVED',
                           owner_store['stale']: 'DISPROVED'}.items():
        assert f'[{item_id}] {trust}' in text
    assert owner_store['private'] not in text and 'recovery' not in text


def test_delegated_roles_never_receive_identity_items(owner_store):
    from lib.memory.owner_memory import explain_memory_item

    context = _search(owner_store, identity=False)['context']
    assert 'ctx_identity' not in _trust(context['items'])
    assert context['withheld_identity'] == 1
    assert explain_memory_item('ctx_identity', scope=SCOPE, identity=False,
                               usage_limit=5) is None


def test_no_role_sees_a_sensitive_lesson_but_the_owner_does(owner_store):
    from lib.memory.owner_memory import explain_memory_item, format_explanation

    role_view = explain_memory_item(owner_store['private'], scope=SCOPE,
                                    project_path=owner_store['root'], usage_limit=5)
    answer = format_explanation(role_view, owner_store['private'])
    assert 'withheld' in answer
    assert 'personal recovery' not in answer and 'medical' not in answer

    owner = _search(owner_store, sensitive=True)['lessons']
    flagged = {record['id']: record for record in owner['items']}
    assert flagged[owner_store['private']]['sensitive'] is True
    assert owner['sensitive_withheld'] == 0
    owner_view = explain_memory_item(owner_store['private'], scope=SCOPE, sensitive=True,
                                     project_path=owner_store['root'], usage_limit=5)
    assert owner_view['item']['body'] == 'private medical details'


def test_why_names_the_chain_and_the_turns_that_applied_the_item(owner_store):
    from lib.memory.owner_memory import explain_memory_item, format_explanation

    old = explain_memory_item('ctx_english', scope=SCOPE, usage_limit=5)
    [link] = old['chain']
    assert (link['status'], link['status_reason']) == ('superseded', 'owner_revoked')
    assert link['successor']['id'] == 'ctx_chinese'
    assert link['successor']['trust'] == 'owner-stated'

    after = 4102444800000  # 2100-01-01: later than every item above
    before = 1700000000000  # older than the item: it cannot have applied it
    turns = [
        {'turnId': 'newest', 'conversationId': 'c', 'createdAt': after + 3,
         'projection': {'preferencesApplied': {'ids': ['ctx_chinese'], 'items': []}}},
        {'turnId': 'other', 'conversationId': 'c', 'createdAt': after + 2,
         'projection': {'preferencesApplied': {'ids': ['ctx_audited'], 'items': []}}},
        {'turnId': 'older-than-item', 'conversationId': 'c', 'createdAt': before,
         'projection': {'preferencesApplied': {'ids': ['ctx_chinese']}}},
        # A branch lane follows the main lane's oldest Turn; its Turns predate
        # ids, so the exact applied text identifies the item.
        {'turnId': 'branch', 'conversationId': 'c', 'createdAt': after + 1,
         'projection': {'preferencesApplied': {'items': ['Reply in Chinese quokkatoken']}}},
    ]
    current = explain_memory_item('ctx_chinese', scope=SCOPE, turns=turns, usage_limit=5)
    assert [row['id'] for row in current['supersedes']] == ['ctx_english']
    assert [row['turn_id'] for row in current['used_in']['turns']] == ['newest', 'branch']
    assert current['used_in']['complete'] is True
    text = format_explanation(current, 'ctx_chinese')
    assert 'trust: owner-stated' in text and 'Turn newest' in text

    lesson = explain_memory_item(owner_store['stale'], scope=SCOPE,
                                 project_path=owner_store['root'], usage_limit=1, turns=[
        {'turnId': 'b', 'conversationId': 'c', 'createdAt': after + 2,
         'projection': {'memoryPrefetch': {'memories': [{'id': owner_store['stale']}]}}},
        {'turnId': 'a', 'conversationId': 'c', 'createdAt': after + 1,
         'projection': {'memoryPrefetch': {'memories': [{'id': owner_store['stale']}]}}},
    ])
    assert lesson['item']['trust'] == 'disproved'
    assert lesson['chain'][0]['successor']['id'] == owner_store['replacement']
    assert [row['turn_id'] for row in lesson['used_in']['turns']] == ['b']
    assert lesson['used_in']['complete'] is False


def test_the_search_tool_answers_owner_mode_and_memory_id(owner_store, monkeypatch):
    from lib.tasks_pkg.handlers.memory import _memory_search

    task = {'_profileScope': SCOPE, 'config': {'preferencesEnabled': True}}
    found, _badge, _title = _memory_search({'mode': 'owner', 'query': 'quokkatoken'},
                                           owner_store['root'], task=task)
    assert '[ctx_chinese] OWNER-STATED' in found
    assert f'[{owner_store["stale"]}] DISPROVED' in found

    why, _badge, _title = _memory_search({'memory_id': owner_store['stale']},
                                         owner_store['root'], task=task)
    assert 'trust: disproved' in why and 'claimed before the key was added' in why

    closed, _badge, _title = _memory_search(
        {'mode': 'owner', 'query': 'quokkatoken'}, owner_store['root'],
        task={'_profileScope': SCOPE, 'config': {'preferencesEnabled': False}})
    assert 'My Context: unavailable to this task (preferences_disabled)' in closed
    assert 'ctx_chinese' not in closed

    monkeypatch.setattr('lib.tasks_pkg.execution_profile.is_third_party_text', lambda _task: True)
    with pytest.raises(ValueError):
        _memory_search({'mode': 'owner', 'query': 'quokkatoken'}, owner_store['root'], task=task)


@pytest.mark.parametrize(('status', 'source', 'edited', 'trust'), [
    ('active', 'owner_stated', None, 'owner-stated'),
    ('active', 'owner_confirmed', None, 'verified'),
    ('active', 'legacy_import', None, 'verified'),
    ('active', 'agent_report', 'owner_stated', 'verified'),
    ('active', 'agent_report', None, 'inferred'),
    (None, None, None, 'inferred'),
    ('needs_confirmation', 'owner_stated', None, 'inferred'),
    ('disproved', 'owner_stated', None, 'disproved'),
    ('superseded', 'agent_report', None, 'disproved'),
])
def test_trust_comes_from_the_write_path_and_the_status(status, source, edited, trust):
    from lib.memory.contracts import MEMORY_TRUST_CLASSES, memory_trust_class

    assert memory_trust_class(status, source, edited) == trust
    assert trust in MEMORY_TRUST_CLASSES
