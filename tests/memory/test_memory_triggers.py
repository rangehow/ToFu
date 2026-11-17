"""A lesson records the situation it applies in (its trigger), and selection matches on it.

Lessons are written after the fix but needed before it; the trigger joins the
name, description and tags in the one BM25 document and the prefetch
confidence gate (lib/memory/triggers.py). Synthetic lessons only.
"""

from __future__ import annotations

import pytest

from lib.error_envelope import BadRequest

pytestmark = pytest.mark.unit

WORKER = {'convId': 'worker-1', '_turnId': 'turn-w1'}


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


def _create(project, name, description, trigger, body='body', **extra):
    from lib.tasks_pkg.handlers.memory import _memory_create
    return _memory_create({'name': name, 'description': description, 'trigger': trigger,
                           'body': body, 'scope': 'project', 'repeat_of': 'new', **extra},
                          project, [], task=WORKER)


def _prefetch(project, text):
    from lib.memory.prefetch import _run
    return [row['id'] for row in _run.run_memory_prefetch(
        [{'role': 'user', 'content': text}], project, task={})]


def test_create_memory_requires_and_stores_the_trigger(store):
    from lib.memory.storage import get_eligible_memories, get_memory
    from lib.memory.tools import CREATE_MEMORY_TOOL, MERGE_MEMORY_TOOL, UPDATE_MEMORY_TOOL

    assert 'trigger' in CREATE_MEMORY_TOOL['function']['parameters']['required']
    assert 'trigger' in MERGE_MEMORY_TOOL['function']['parameters']['required']
    assert 'trigger' in UPDATE_MEMORY_TOOL['function']['parameters']['properties']

    _create(store, 'zorblat font cache', 'zorblat hangs: point fontconfig at a writable cache',
            'studio page visual tests in a headless browser never finish')
    lesson = get_memory('zorblat_font_cache', project_path=store)
    assert lesson['trigger'] == 'studio page visual tests in a headless browser never finish'
    [listed] = get_eligible_memories(store, include_body=False, record_view='retrieval')
    assert listed['trigger'] == lesson['trigger']


def test_trigger_is_one_bounded_line(store):
    from lib.memory.contracts import MEMORY_TRIGGER_MAX_CHARS
    from lib.memory.storage import create_memory

    with pytest.raises(BadRequest, match='single line'):
        create_memory('a', 'b', 'c', scope='project', project_path=store, trigger='one\ntwo')
    with pytest.raises(BadRequest, match='limit'):
        create_memory('a', 'b', 'c', scope='project', project_path=store,
                      trigger='x' * (MEMORY_TRIGGER_MAX_CHARS + 1))


def test_prefetch_selects_a_lesson_by_its_trigger_alone(store):
    _create(store, 'zorblat font cache', 'zorblat hangs: point fontconfig at a writable cache',
            'studio page visual tests in a headless browser never finish')
    _create(store, 'quux palette', 'quux palette tokens live in one stylesheet',
            'restyling quux buttons')

    request = 'Polish the studio page and run its visual tests in the browser'
    assert _prefetch(store, request) == ['zorblat_font_cache']


def test_lessons_without_a_trigger_select_exactly_as_before(store):
    from lib.memory.relevance._tokenize import _memory_document_text
    from lib.memory.storage import create_memory

    create_memory('quux palette', 'quux palette tokens live in one stylesheet', 'b',
                  scope='project', project_path=store)
    lesson = {'name': 'n', 'description': 'd', 'tags': ['t']}
    assert _memory_document_text(lesson).split() == _memory_document_text(
        {**lesson, 'trigger': ''}).split()
    assert _prefetch(store, 'restyle the quux palette tokens') == ['quux_palette']


def test_a_new_version_keeps_or_replaces_the_trigger(store):
    from lib.memory.storage import get_memory
    from lib.tasks_pkg.handlers.memory import _memory_update

    _create(store, 'zorblat font cache', 'zorblat hangs: fontconfig cache', 'visual tests hang')
    content, badge, _ = _memory_update({'memory_id': 'zorblat_font_cache', 'body': 'new body'},
                                       store, [], task=WORKER)
    assert badge == '✏️ new version'
    assert get_memory('zorblat_font_cache_1', project_path=store)['trigger'] == 'visual tests hang'

    _memory_update({'memory_id': 'zorblat_font_cache_1', 'trigger': 'e2e tier never finishes'},
                   store, [], task=WORKER)
    assert get_memory('zorblat_font_cache_2', project_path=store)['trigger'] == 'e2e tier never finishes'
    assert get_memory('zorblat_font_cache_1', project_path=store)['status'] == 'superseded'


def test_a_repeat_records_the_new_trigger_on_the_current_version(store):
    from lib.memory.storage import get_memory
    from lib.tasks_pkg.handlers.memory import _memory_create

    _create(store, 'zorblat font cache', 'zorblat hangs: fontconfig cache', 'visual tests hang')
    _memory_create({'name': 'zorblat font cache', 'description': 'zorblat hangs: fontconfig cache',
                    'trigger': 'screenshot step of the studio check hangs', 'body': 'again',
                    'repeat_of': 'zorblat_font_cache'}, store, [],
                   task={'convId': 'worker-2', '_turnId': 'turn-w2'})
    current = get_memory('zorblat_font_cache_1', project_path=store)
    assert current['trigger'] == 'screenshot step of the studio check hangs'
    assert list(current['recurrences']) == ['worker-2/turn-w2']


def test_parse_trigger_accepts_one_json_line_and_refuses_the_rest():
    from lib.memory.triggers import parse_trigger, trigger_backfill_input

    assert parse_trigger('{"trigger": "  run the e2e tier\\n of the studio page "}') == \
        'run the e2e tier of the studio page'
    with pytest.raises(ValueError):
        parse_trigger('no json here')
    with pytest.raises(ValueError):
        parse_trigger('{"trigger": "   "}')
    with pytest.raises(ValueError):
        parse_trigger('{"trigger": "' + 'x' * 600 + '"}')
    assert trigger_backfill_input('---\nname: n\n---\nbody') == 'LESSON:\n---\nname: n\n---\nbody'


def test_backfill_inserts_the_trigger_and_keeps_every_other_byte(tmp_path):
    from lib.memory.storage._frontmatter import _parse_frontmatter
    from scripts import backfill_memory_triggers as script

    store = tmp_path / 'memories'
    (store / 'pkg').mkdir(parents=True)
    cached = '---\nname: a\ndescription: d\nkeywords: hand, written\n---\n\nbody a\n'
    asked = '---\nname: b\ndescription: e\n---\n\nbody b\n'
    done = '---\nname: c\ntrigger: already\n---\n\nbody c\n'
    for name, text in (('a.md', cached), ('b.md', asked), ('c.md', done)):
        (store / name).write_text(text)
    (store / 'pkg' / 'SKILL.md').write_text(cached)
    cache = {script._sha(cached): {'trigger': 'from the measured pass'}}

    dry = script.backfill([store], cache, dry_run=True, ask=lambda text: 1 / 0)
    assert (dry['fromCache'], dry['fromModel'], dry['already']) == (1, 1, 1)
    assert (store / 'a.md').read_text() == cached

    summary = script.backfill([store], cache, dry_run=False, ask=lambda text: 'model wrote this')
    assert (summary['fromCache'], summary['fromModel'], summary['failed']) == (1, 1, [])
    written = (store / 'a.md').read_text()
    assert written == cached.replace('written\n---', 'written\ntrigger: from the measured pass\n---')
    assert _parse_frontmatter((store / 'b.md').read_text())[0]['trigger'] == 'model wrote this'
    assert (store / 'c.md').read_text() == done
    assert (store / 'pkg' / 'SKILL.md').read_text() == cached
    again = script.backfill([store], {}, dry_run=False, ask=lambda text: 1 / 0)
    assert again['already'] == 3 and again['failed'] == []
