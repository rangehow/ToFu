"""Contracts for the personal owner's one-time inheritance of the legacy
open-mode My Context (scope ``''``).

Authority: docs/modules/context_engineering.md, "Legacy My Context
inheritance". Tasks stamp ``_profileScope = str(owner_user_id)``; once open mode
began to carry ``PERSONAL_USER_ID`` the populated scope-``''`` document became
unreachable. The owner's scope inherits it exactly once, with provenance and an
audited status, and only ``active`` entries are ever injected.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from typing import NamedTuple

import pytest

from lib.identity import PERSONAL_USER_ID

pytestmark = pytest.mark.unit

# Install-specific audit data, excluded from Internal and Open exports.
AUDIT_MODULE = 'lib.memory.user_profile._legacy_audit'
OWNER = str(PERSONAL_USER_ID)
ACTIVE_TEXT = 'Keep status updates short'
STALE_TEXT = 'Release through the retired gateway'
UNAUDITED_TEXT = 'Works on project Q'
STAMP = '2026-08-12T17:03:59.959204Z'


class Verdict(NamedTuple):
    """The verdict shape an install's audit module provides."""
    text_sha256: str
    status: str
    status_reason: str = ''
    superseded_by: dict | None = None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _install_audit(monkeypatch, verdicts: dict) -> None:
    monkeypatch.setitem(sys.modules, AUDIT_MODULE, types.SimpleNamespace(
        LEGACY_OPEN_SCOPE_AUDIT_ID='test-audit',
        LEGACY_OPEN_SCOPE_VERDICTS=verdicts))


def _write_legacy_document(up) -> str:
    def legacy(item_id, item_type, text):
        return {'id': item_id, 'type': item_type, 'text': text,
                'source': 'legacy_migration', 'created_at': STAMP,
                'updated_at': STAMP}

    path = up.context_path('')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'items': [
            legacy('ctx_active', 'response_preference', ACTIVE_TEXT),
            legacy('ctx_stale', 'response_preference', STALE_TEXT),
            legacy('ctx_unaudited', 'identity', UNAUDITED_TEXT),
        ]}, handle)
    return path


def _read(path: str) -> bytes:
    with open(path, 'rb') as handle:
        return handle.read()


@pytest.fixture
def context_data_dir(tmp_path, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs

    monkeypatch.setattr(storage, '_server_data_dir', lambda: str(tmp_path))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(tmp_path))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    yield tmp_path
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


@pytest.fixture
def legacy_scope(context_data_dir, monkeypatch):
    """A legacy scope-'' document; the audit activates one item, supersedes one."""
    import lib.memory.user_profile as up
    path = _write_legacy_document(up)
    _install_audit(monkeypatch, {
        'ctx_active': Verdict(_sha(ACTIVE_TEXT), 'active'),
        'ctx_stale': Verdict(_sha(STALE_TEXT), 'superseded', 'stale',
                             {'evidence': 'gateway retired'}),
    })
    return path


def test_owner_scope_reads_only_the_active_inherited_entries(legacy_scope):
    import lib.memory.user_profile as up
    assert not os.path.exists(up.context_path(OWNER))

    doc = up.load_context(OWNER)

    assert [item['text'] for item in doc['items']] == [ACTIVE_TEXT]
    assert doc['items'][0]['status'] == 'active'
    inactive = {item['id']: item for item in doc['inactive_items']}
    assert set(inactive) == {'ctx_stale', 'ctx_unaudited'}
    assert inactive['ctx_stale']['status'] == 'superseded'
    assert inactive['ctx_stale']['status_reason'] == 'stale'
    assert inactive['ctx_stale']['superseded_by'] == {'evidence': 'gateway retired'}
    assert inactive['ctx_unaudited']['status'] == 'needs_confirmation'
    assert inactive['ctx_unaudited']['status_reason'] == 'needs_owner_confirmation'
    for item in doc['items'] + doc['inactive_items']:
        assert item['source'] == 'legacy_migration'
        assert item['created_at'] == STAMP
        provenance = item['provenance']
        assert provenance['source_class'] == 'legacy_import'
        assert provenance['origin_scope'] == ''
        assert provenance['origin_source'] == 'legacy_migration'
        assert provenance['origin_created_at'] == STAMP
    assert doc['inherited']['from_scope'] == ''
    assert doc['inherited']['source_sha256'] == hashlib.sha256(
        _read(legacy_scope)).hexdigest()


def test_superseded_and_unconfirmed_entries_are_never_injected(legacy_scope):
    import lib.memory.user_profile as up
    from lib.tasks_pkg.context_composer import ComposeRequest, compose_context

    task = {'_profileScope': OWNER, 'config': {'preferencesEnabled': True}}
    result = compose_context([{'role': 'user', 'content': 'hello'}],
                             ComposeRequest(task=task))
    rendered = json.dumps(result.messages, ensure_ascii=False)
    assert '[USER CONTEXT]' in rendered
    assert ACTIVE_TEXT in rendered
    assert STALE_TEXT not in rendered
    assert UNAUDITED_TEXT not in rendered
    assert task['_appliedPreferences']['items'] == [ACTIVE_TEXT]
    mirror = _read(up.profile_path(OWNER)).decode('utf-8')
    assert ACTIVE_TEXT in mirror
    assert STALE_TEXT not in mirror and UNAUDITED_TEXT not in mirror

    # Settings saves with a full replace of what it was shown, stripped to its
    # own fields (frontend/src/features/memory/preferences.ts cleanItems).
    shown = up.context_status(OWNER)['items']
    keys = ('id', 'type', 'source', 'created_at', 'updated_at', 'text')
    up.save_context_items(
        [{key: item[key] for key in keys} for item in shown]
        + [{'type': 'response_preference', 'text': 'Owner typed this'}], OWNER)

    after = up.load_context(OWNER)
    assert [item['text'] for item in after['items']] == [
        ACTIVE_TEXT, 'Owner typed this']
    assert after['items'][0]['provenance']['source_class'] == 'legacy_import'
    assert {item['id']: item['status'] for item in after['inactive_items']} == {
        'ctx_stale': 'superseded', 'ctx_unaudited': 'needs_confirmation'}
    block = up.render_profile_block(scope=OWNER)
    assert ACTIVE_TEXT in block
    assert STALE_TEXT not in block and UNAUDITED_TEXT not in block
    assert STALE_TEXT not in up.context_markdown(
        after['items'] + after['inactive_items'])


def test_entry_edited_after_the_audit_is_not_activated(legacy_scope, monkeypatch):
    import lib.memory.user_profile as up
    _install_audit(monkeypatch, {
        'ctx_active': Verdict(_sha('audited other text'), 'active'),
    })

    doc = up.load_context(OWNER)

    assert doc['items'] == []
    assert {item['status'] for item in doc['inactive_items']} == {
        'needs_confirmation'}
    assert up.render_profile_block(scope=OWNER) is None


def test_undo_restores_the_previous_state(legacy_scope):
    import lib.memory.user_profile as up
    source = _read(legacy_scope)
    changes_path = up.context_changes_path(OWNER)
    os.makedirs(os.path.dirname(changes_path), exist_ok=True)
    with open(changes_path, 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'changes': []}, handle)
    changes = _read(changes_path)
    assert up.load_context(OWNER)['items']

    assert up.undo_legacy_context_inheritance(OWNER) == {'undone': True}

    doc = up.load_context(OWNER)
    assert doc['items'] == [] and doc['inactive_items'] == []
    assert up.render_profile_block(scope=OWNER) is None
    assert not os.path.exists(up.profile_path(OWNER))
    assert _read(legacy_scope) == source
    assert _read(changes_path) == changes
    assert up.load_context(OWNER)['inactive_items'] == []
    assert up.undo_legacy_context_inheritance(OWNER) == {
        'undone': True, 'already_undone': True}


def test_undo_refuses_to_discard_owner_edits(legacy_scope):
    import lib.memory.user_profile as up
    up.load_context(OWNER)
    up.create_context_item({'type': 'identity', 'text': 'Owner fact'}, OWNER)

    with pytest.raises(up.ContextConflictError):
        up.undo_legacy_context_inheritance(OWNER)

    assert 'Owner fact' in [item['text'] for item in up.load_context(OWNER)['items']]


def test_second_run_changes_nothing(legacy_scope):
    import lib.memory.user_profile as up
    source = _read(legacy_scope)
    first = up.load_context(OWNER)
    stored = _read(up.context_path(OWNER))

    assert up.load_context(OWNER) == first
    assert _read(up.context_path(OWNER)) == stored

    # Removing every active entry must not resurrect the inheritance.
    up.save_context_items([], OWNER)
    doc = up.load_context(OWNER)
    assert doc['items'] == []
    assert {item['id'] for item in doc['inactive_items']} == {
        'ctx_stale', 'ctx_unaudited'}
    assert doc['inherited']['migrated_at'] == first['inherited']['migrated_at']
    assert _read(legacy_scope) == source


def test_other_owners_see_nothing(legacy_scope):
    import lib.memory.user_profile as up
    source = _read(legacy_scope)

    for scope in ('2', 'tenant7'):
        doc = up.load_context(scope)
        assert doc['items'] == []
        assert doc.get('inactive_items', []) == []
        assert up.render_profile_block(scope=scope) is None
        assert not os.path.exists(up.context_path(scope))
    assert _read(legacy_scope) == source


def test_missing_audit_module_fails_closed(legacy_scope, context_data_dir):
    """An export without the install's audit module inherits nothing active."""
    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(f'''
        import json, sys
        sys.modules[{AUDIT_MODULE!r}] = None
        import lib.memory.storage as storage
        import lib.memory.storage._dirs as storage_dirs
        storage._server_data_dir = lambda: sys.argv[1]
        storage_dirs._server_data_dir = lambda: sys.argv[1]
        import lib.memory.user_profile as up
        doc = up.load_context(sys.argv[2])
        print('REPORT ' + json.dumps({{
            'active': len(doc['items']),
            'statuses': sorted({{item['status'] for item in doc['inactive_items']}}),
            'inactive': len(doc['inactive_items']),
            'block': up.render_profile_block(scope=sys.argv[2]),
        }}))
    ''')
    result = subprocess.run(
        [sys.executable, '-c', script, str(context_data_dir), OWNER],
        capture_output=True, text=True, cwd=str(root),
        env={**os.environ, 'PYTHONPATH': str(root)})

    assert result.returncode == 0, result.stderr[-2000:]
    report = next(line for line in result.stdout.splitlines()
                  if line.startswith('REPORT '))
    assert json.loads(report[len('REPORT '):]) == {
        'active': 0, 'statuses': ['needs_confirmation'], 'inactive': 3,
        'block': None}


def test_authenticated_composition_without_scope_gets_no_user_context(context_data_dir):
    import lib.memory.user_profile as up
    from lib.tasks_pkg.context_composer import ComposeRequest, compose_context
    from lib.tasks_pkg.wire_messages import build_wire_messages
    _write_legacy_document(up)

    task = {'config': {'preferencesEnabled': True}}
    result = compose_context([{'role': 'user', 'content': 'hello'}],
                             ComposeRequest(task=task, user_id=PERSONAL_USER_ID))
    rendered = json.dumps(result.messages, ensure_ascii=False)
    assert '[USER CONTEXT]' not in rendered
    assert ACTIVE_TEXT not in rendered and UNAUDITED_TEXT not in rendered
    assert '_appliedPreferences' not in task
    row = next(row for row in result.manifest if row['id'] == 'user_context')
    assert row['reason'] == 'profile_scope_missing'

    # The debug wire builder composes a throwaway task for an authenticated user.
    wire, manifest = build_wire_messages(
        [{'role': 'user', 'content': 'hello'}], {'preferencesEnabled': True},
        user_id=PERSONAL_USER_ID, return_manifest=True)
    assert '[USER CONTEXT]' not in json.dumps(wire, ensure_ascii=False)
    assert next(row for row in manifest if row['id'] == 'user_context')[
        'reason'] == 'profile_scope_missing'


def test_offline_composition_without_a_user_keeps_the_legacy_scope(context_data_dir):
    import lib.memory.user_profile as up
    from lib.tasks_pkg.context_composer import ComposeRequest, compose_context
    _write_legacy_document(up)

    result = compose_context([{'role': 'user', 'content': 'hello'}],
                             ComposeRequest(task={'config': {'preferencesEnabled': True}}))

    rendered = json.dumps(result.messages, ensure_ascii=False)
    assert '[USER CONTEXT]' in rendered and ACTIVE_TEXT in rendered


def test_shipped_audit_activates_no_legacy_entry():
    import lib.memory.user_profile as up
    audit = pytest.importorskip(
        AUDIT_MODULE, reason='install-specific; excluded from Internal and Open')
    if isinstance(audit, types.SimpleNamespace):
        pytest.fail('a test leaked its fake audit module')
    verdicts = audit.LEGACY_OPEN_SCOPE_VERDICTS

    assert len(verdicts) == 17
    for verdict in verdicts.values():
        assert len(verdict.text_sha256) == 64
        assert verdict.status in up.CONTEXT_STATUSES
        assert verdict.status != 'active'
        assert verdict.status_reason in up.CONTEXT_STATUS_REASONS
        if verdict.status == 'superseded':
            assert verdict.status_reason != 'needs_owner_confirmation'
            assert len(verdict.superseded_by or {}) >= 1
        else:
            assert verdict.status_reason == 'needs_owner_confirmation'
            assert not verdict.superseded_by
