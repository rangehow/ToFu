"""Lesson lifecycle: a disproved or superseded lesson stays on disk with its
provenance but never reaches automatic injection or default search, and only
the write path decides who said what (design record "Owner understanding",
Decisions 2, 7 and 8; docs/modules/context_engineering.md "Memory boundary").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.error_envelope import BadRequest

pytestmark = pytest.mark.unit

AGENT = {'source_class': 'agent_report', 'conversation_id': 'conv-{a}',
         'turn_id': 'turn-"1"', 'at': '2026-11-10T08:00:00Z'}
OWNER = {'source_class': 'owner_stated', 'via': 'api', 'at': '2026-11-11T08:00:00Z'}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    (project / '.tofu').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()
    yield str(project)
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _lesson(project, name, *, provenance=AGENT):
    from lib.memory.storage import create_memory
    return create_memory(name, f'{name} quokkatoken lesson', f'{name} body quokkatoken',
                         scope='project', project_path=project, provenance=provenance)


def _disprove(project, memory_id, **pointer):
    from lib.memory.storage import update_memory
    return update_memory(memory_id, {
        'status': 'disproved', 'status_reason': 'disproved',
        'superseded_by': pointer or {'evidence': 'reproduced the opposite'}},
        project_path=project, provenance=AGENT)


def test_lifecycle_and_provenance_survive_a_reload(store):
    import lib.memory.storage._files as memory_files
    from lib.memory.storage import get_memory

    old = _lesson(store, 'host has no ssh key')
    evidence = 'git ls-remote {exit 0} printed "refs/heads/main"\nwith the deploy key'
    _disprove(store, old['id'], evidence=evidence)
    memory_files._metadata_cache.clear()

    loaded = get_memory(old['id'], project_path=store)
    assert loaded['status'] == 'disproved'
    assert loaded['status_reason'] == 'disproved'
    assert loaded['superseded_by'] == {'evidence': evidence}
    assert loaded['provenance'] == {**AGENT, **{f'edited_{key}': value for key, value in AGENT.items()}}
    assert loaded['name'] == 'host has no ssh key'
    assert loaded['body'] == 'host has no ssh key body quokkatoken'


def test_only_active_unflagged_lessons_reach_injection_and_default_search(store):
    from lib.memory.prefetch import _run
    from lib.memory.relevance import search_memories
    from lib.memory.storage import (
        get_eligible_memories, iter_eligible_memories, load_eligible_memories, update_memory,
    )

    active = _lesson(store, 'alpha zebrafinch')
    disproved = _lesson(store, 'beta zebrafinch')
    superseded = _lesson(store, 'gamma zebrafinch')
    sensitive = _lesson(store, 'delta zebrafinch')
    query = [{'role': 'user', 'content': 'beta zebrafinch quokkatoken'}]
    # Control: before the change the query selects the lesson about to be disproved.
    assert disproved['id'] in {row['id'] for row in _run.run_memory_prefetch(query, store, task={})}

    _disprove(store, disproved['id'])
    update_memory(superseded['id'], {'status': 'superseded', 'status_reason': 'duplicate',
                                     'superseded_by': {'item_id': active['id']}},
                  project_path=store, provenance=AGENT)
    update_memory(sensitive['id'], {'sensitive': True}, project_path=store, provenance=OWNER)

    ids = {active['id'], disproved['id'], superseded['id'], sensitive['id']}
    # The metadata-only retrieval view carries a non-default lifecycle, so
    # selection drops the lesson without reading its body.
    from lib.memory.storage import get_enabled_memories
    views = {row['id']: row for row in get_enabled_memories(
        store, include_body=False, record_view='retrieval')}
    assert 'status' not in views[active['id']] and 'sensitive' not in views[active['id']]
    assert views[disproved['id']]['status'] == 'disproved'
    assert views[sensitive['id']]['sensitive'] is True
    assert {row['id'] for row in get_eligible_memories(store)} & ids == {active['id']}
    assert {row['id'] for row in get_eligible_memories(
        store, include_body=False, record_view='retrieval')} & ids == {active['id']}
    assert {row['id'] for row in iter_eligible_memories(store, body_char_limit=200)} & ids == {active['id']}
    assert [row['id'] for row in load_eligible_memories(sorted(ids), store, body_char_limit=200)] == [
        active['id']]
    found = search_memories('zebrafinch quokkatoken', project_path=store)
    assert active['id'] in found
    assert not [memory_id for memory_id in ids - {active['id']} if memory_id in found]
    assert disproved['id'] not in {row['id'] for row in _run.run_memory_prefetch(query, store, task={})}


def test_restoring_a_lesson_to_active_clears_its_reason_and_pointer(store):
    import lib.memory.storage._files as memory_files
    from lib.memory.storage import get_eligible_memories, get_memory, update_memory

    lesson = _lesson(store, 'restorable')
    _disprove(store, lesson['id'])
    update_memory(lesson['id'], {'status': 'active'}, project_path=store, provenance=OWNER)
    memory_files._metadata_cache.clear()

    loaded = get_memory(lesson['id'], project_path=store)
    assert (loaded['status'], loaded['status_reason'], loaded['superseded_by']) == ('active', '', {})
    assert 'status' not in Path(loaded['filepath']).read_text(encoding='utf-8').split('---')[1]
    assert lesson['id'] in {row['id'] for row in get_eligible_memories(store)}


@pytest.mark.parametrize('updates', [
    {'status': 'disproved', 'status_reason': 'disproved'},
    {'status': 'disproved', 'superseded_by': {'evidence': 'checked'}},
    {'status_reason': 'stale'},
    {'superseded_by': {'item_id': 'other'}},
    {'status': 'archived', 'status_reason': 'stale', 'superseded_by': {'evidence': 'checked'}},
    {'status': 'disproved', 'status_reason': 'wrong', 'superseded_by': {'evidence': 'checked'}},
    {'status': 'superseded', 'status_reason': 'stale',
     'superseded_by': {'item_id': 'other', 'evidence': 'checked'}},
    {'status': 'superseded', 'status_reason': 'stale', 'superseded_by': {'turn_id': 'only-a-turn'}},
    {'status': 'active', 'status_reason': 'stale'},
])
def test_a_lifecycle_change_names_one_reason_and_exactly_one_pointer(store, updates):
    from lib.memory.storage import get_memory, update_memory

    lesson = _lesson(store, 'validated')
    with pytest.raises(BadRequest):
        update_memory(lesson['id'], updates, project_path=store, provenance=AGENT)
    assert get_memory(lesson['id'], project_path=store)['status'] == 'active'


def test_model_writes_are_agent_reports_that_cannot_touch_owner_only_fields(store):
    from lib.memory.storage import get_memory
    from lib.tasks_pkg.handlers.memory import _memory_create, _memory_update

    task = {'convId': 'conv-model', '_turnId': 'turn-model'}
    _memory_create({'name': 'model lesson', 'description': 'model lesson quokkatoken',
                    'body': 'first', 'scope': 'project'}, store, task=task)
    created = get_memory('model_lesson', project_path=store)
    assert created['provenance']['source_class'] == 'agent_report'
    assert (created['provenance']['conversation_id'], created['provenance']['turn_id']) == (
        'conv-model', 'turn-model')

    _memory_update({'memory_id': 'model_lesson', 'body': 'second', 'sensitive': True,
                    'enabled': False, 'provenance': {'source_class': 'owner_stated'}},
                   store, task=task)
    # A corrected body is a new version (tests/memory/test_memory_compounding.py).
    assert get_memory('model_lesson', project_path=store)['status'] == 'superseded'
    updated = get_memory('model_lesson_1', project_path=store)
    assert updated['body'] == 'second'
    assert updated['sensitive'] is False and updated['enabled'] is True
    assert updated['provenance']['source_class'] == 'agent_report'
    assert updated['provenance']['turn_id'] == 'turn-model'
    _memory_update({'memory_id': 'model_lesson_1', 'tags': ['kept'], 'sensitive': True},
                   store, task=task)
    tagged = get_memory('model_lesson_1', project_path=store)
    assert tagged['tags'] == ['kept'] and tagged['sensitive'] is False
    assert tagged['provenance']['edited_source_class'] == 'agent_report'
    assert tagged['provenance']['edited_turn_id'] == 'turn-model'


def test_a_storage_write_refuses_an_undeclared_source_class(store):
    from lib.memory.storage import create_memory

    with pytest.raises(ValueError):
        create_memory('forged', 'forged quokkatoken', 'body', scope='project', project_path=store,
                      provenance={'source_class': 'owner_confirmed_by_model'})
    assert not list((Path(store) / '.tofu' / 'memories').glob('*.md'))


def test_an_unknown_hand_written_status_fails_closed(store):
    from lib.memory.storage import get_eligible_memories, iter_lesson_records

    directory = Path(store) / '.tofu' / 'memories'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'hand_written.md').write_text(
        '---\nname: hand written\ndescription: hand written quokkatoken\nenabled: true\n'
        'status: archived\n---\n\nbody\n', encoding='utf-8')

    assert 'hand_written' not in {row['id'] for row in get_eligible_memories(store)}
    records = {row['id']: row for row in iter_lesson_records(store, body_char_limit=200)}
    assert records['hand_written']['status'] == 'archived'


def test_the_owner_lookup_corpus_keeps_inactive_lessons_and_never_loads_sensitive_bodies(store):
    from lib.memory.storage import iter_lesson_records, update_memory

    active = _lesson(store, 'kept active')
    disproved = _lesson(store, 'kept disproved')
    sensitive = _lesson(store, 'kept sensitive')
    _disprove(store, disproved['id'])
    update_memory(sensitive['id'], {'sensitive': True}, project_path=store, provenance=OWNER)

    records = {row['id']: row for row in iter_lesson_records(store, body_char_limit=200)}
    assert records[active['id']]['body'] and records[disproved['id']]['body']
    assert records[disproved['id']]['status'] == 'disproved'
    assert records[sensitive['id']]['sensitive'] is True
    assert not records[sensitive['id']].get('body')


def test_lessons_and_my_context_share_one_status_reason_vocabulary():
    from lib.memory.contracts import MEMORY_STATUS_REASONS
    from lib.memory.user_profile._context import CONTEXT_STATUS_REASONS

    assert CONTEXT_STATUS_REASONS is MEMORY_STATUS_REASONS
