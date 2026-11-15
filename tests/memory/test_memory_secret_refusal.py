"""Credentials in lessons and My Context: refused on write, redacted on read.

Design record "Owner understanding", Decision 7: every memory and My Context
write is checked before anything is stored, with the browser-evidence
redaction shapes rather than a third copy of them; a refusal names the field,
key and line, never the value. Text stored before the refusal existed is
redacted with the same shapes wherever a model reads memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.error_envelope import BadRequest

pytestmark = pytest.mark.unit

# Synthetic, credential-shaped and never a real credential.
SECRET = 'sk-live-' + 'q9w8e7r6t5y4u3i2o1p0'
LEGACY_SECRET = '0123456789abcdef' * 2
AGENT = {'source_class': 'agent_report', 'at': '2026-11-11T08:00:00Z'}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    (project / '.tofu').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage, '_server_data_dir', lambda: str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()
    yield str(project)
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _stored(project):
    return sorted(path.name for path in (Path(project) / '.tofu' / 'memories').glob('*.md'))


@pytest.mark.parametrize(('field', 'value'), [
    ('body', f'Call the gateway with\napi_key: {SECRET}'),
    ('description', f'send Authorization: Bearer {SECRET} upstream'),
    ('name', f'session token={SECRET}'),
    ('tags', ['gateway', f'password={SECRET}']),
])
def test_a_new_lesson_with_a_credential_is_refused_and_the_value_never_echoed(store, field, value):
    from lib.memory.storage import create_memory

    payload = {'name': 'gateway lesson', 'description': 'how to call the gateway',
               'body': 'use the vault entry', 'tags': ['gateway']}
    payload[field] = value
    with pytest.raises(BadRequest) as refused:
        create_memory(**payload, scope='project', project_path=store, provenance=AGENT)
    assert SECRET not in str(refused.value)
    assert field in str(refused.value)
    assert _stored(store) == []


def test_an_edit_that_adds_a_credential_leaves_the_lesson_unchanged(store):
    from lib.memory.storage import create_memory, get_memory, update_memory

    lesson = create_memory('gateway lesson', 'how to call the gateway', 'use the vault entry',
                           scope='project', project_path=store, provenance=AGENT)
    with pytest.raises(BadRequest) as refused:
        update_memory(lesson['id'], {'body': f'line one\ntoken: {SECRET}'}, project_path=store,
                      provenance=AGENT)
    assert 'body: token (line 2)' in str(refused.value)
    assert get_memory(lesson['id'], project_path=store)['body'] == 'use the vault entry'


def test_a_merge_with_a_credential_deletes_no_source(store):
    from lib.memory.storage import create_memory, merge_memories

    first = create_memory('first', 'first lesson', 'one', scope='project', project_path=store)
    second = create_memory('second', 'second lesson', 'two', scope='project', project_path=store)
    with pytest.raises(BadRequest):
        merge_memories([first['id'], second['id']], 'merged', 'merged lesson',
                       f'cookie={SECRET}', project_path=store, provenance=AGENT)
    assert _stored(store) == ['first.md', 'second.md']


def test_a_lifecycle_change_does_not_rescan_text_written_before_the_scanner(store):
    from lib.memory.storage import get_memory, update_memory

    directory = Path(store) / '.tofu' / 'memories'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'legacy.md').write_text(
        '---\nname: legacy\ndescription: legacy lesson\nenabled: true\n---\n\n'
        f'token: {SECRET}\n', encoding='utf-8')

    update_memory('legacy', {'status': 'superseded', 'status_reason': 'stale',
                             'superseded_by': {'evidence': 'rotated'}},
                  project_path=store, provenance=AGENT)
    assert get_memory('legacy', project_path=store)['status'] == 'superseded'


def test_my_context_refuses_a_credential_before_anything_is_stored(store):
    import lib.memory.user_profile as up

    up.create_context_item({'type': 'response_preference', 'text': 'Reply in Chinese.'}, '7')
    before = up.load_context('7')
    with pytest.raises(up.ContextValidationError) as refused:
        up.create_context_item({'type': 'work_rule', 'condition': 'calling the gateway',
                                'action': f'send password={SECRET}'}, '7')
    assert SECRET not in str(refused.value)
    assert up.load_context('7')['items'] == before['items']


@pytest.mark.parametrize('text', [
    f'api_key: {SECRET}', f'"refresh_token": "{SECRET}"', f'Bearer {SECRET}',
    f'ssoid={SECRET}', 'max_tokens: 128000', 'the token budget is large', 'plain lesson text',
])
def test_the_scanner_refuses_exactly_what_browser_evidence_redacts(text):
    from lib.browser.network_evidence import _redact_text
    from lib.memory.secret_scan import secret_findings

    assert bool(secret_findings({'body': text})) == (_redact_text(text) != text)


def test_every_model_facing_memory_read_redacts_credentials_stored_before_the_refusal(store):
    import json
    import os
    import lib.memory.user_profile as up
    from lib.memory.owner_memory import (
        explain_memory_item, format_explanation, format_owner_search, search_owner_memory,
    )
    from lib.memory.prefetch._inject import _render_relevant_memories_block
    from lib.memory.relevance import search_memories
    from lib.memory.storage import get_memory

    # Written straight to disk, as lessons and items were before the refusal existed.
    directory = Path(store) / '.tofu' / 'memories'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'legacy_gateway.md').write_text(
        '---\nname: legacy gateway\ndescription: wombat gateway notes, api_key=' + SECRET
        + '\nenabled: true\n---\n\nDefault credentials:\nclient_secret = ' + LEGACY_SECRET
        + '\nsend Authorization: Bearer ' + SECRET + '\nkeep the protocol notes\n', encoding='utf-8')
    path = up.context_path('7')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'items': [{
            'id': 'ctx_legacy', 'type': 'response_preference', 'source': 'manual',
            'text': 'wombat reports, password: ' + LEGACY_SECRET,
            'created_at': '2026-07-01T00:00:00Z', 'updated_at': '2026-07-01T00:00:00Z'}]}, handle)

    outputs = {
        'memory_id': format_explanation(explain_memory_item(
            'legacy_gateway', scope='7', project_path=store, usage_limit=5), 'legacy_gateway'),
        'context item': format_explanation(explain_memory_item(
            'ctx_legacy', scope='7', project_path=store, usage_limit=5), 'ctx_legacy'),
        'owner search': format_owner_search(search_owner_memory(
            'wombat', scope='7', project_path=store, top_k=10)),
        'lesson search': search_memories('wombat', project_path=store),
        'prefetch': _render_relevant_memories_block(
            [get_memory('legacy_gateway', project_path=store)], openers={'search_memories'}),
    }
    for where, text in outputs.items():
        assert SECRET not in text and LEGACY_SECRET not in text, where
        assert '[redacted]' in text, where
    assert 'keep the protocol notes' in outputs['memory_id']
    assert 'keep the protocol notes' in outputs['prefetch']
    assert 'legacy_gateway' in outputs['owner search'] and 'ctx_legacy' in outputs['owner search']
