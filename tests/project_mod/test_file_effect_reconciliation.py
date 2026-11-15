"""Outcome inspection proves exact historical file publication without replay."""
import os
from pathlib import Path

import pytest

from lib.project_mod import modifications as mods
from lib.project_mod.effect_reconciliation import inspect_prepared_write, reference_for
from lib.project_mod.write_intents import intent_store
from tests.project_mod.test_project_write_intents import stage

pytestmark = pytest.mark.unit


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    base, target, staged, request = stage(tmp_path)
    session = mods._get_session_dir(str(base))
    with intent_store(session) as store:
        record = store.prepare(**request)
    return base, target, staged, record


def inspect(record, conversation_id='conversation'):
    return inspect_prepared_write(reference_for(record), conversation_id=conversation_id)


def test_prepared_and_published_queries_are_read_only(prepared):
    base, target, staged, record = prepared
    assert inspect(record) == ('file_not_published', 'prepared_file_unpublished')
    assert target.read_bytes() == b'old\r\n\xff'
    os.replace(staged, target)
    for _ in range(3):
        assert inspect(record) == ('file_published', 'prepared_file_applied')
    assert target.read_bytes() == b'new\n'
    with intent_store(mods._get_session_dir(str(base))) as store:
        assert store.read() == [record]


@pytest.mark.parametrize('change', ['later_edit', 'equal_content_new_inode', 'root_swap', 'missing_stage'])
def test_pending_conflicts_remain_unknown(prepared, change):
    base, target, staged, record = prepared
    if change == 'missing_stage':
        staged.unlink()
    else:
        os.replace(staged, target)
        if change == 'later_edit':
            target.write_bytes(b'user edit')
        elif change == 'equal_content_new_inode':
            other = base / 'other'; other.write_bytes(b'new\n'); os.replace(other, target)
        else:
            base.rename(base.with_name('moved')); base.mkdir(); target.write_bytes(b'new\n')
    before = target.read_bytes()
    assert inspect(record) == ('unknown', 'evidence_conflict')
    assert target.read_bytes() == before


def test_owner_attribution_and_exact_digest_required(prepared):
    _, target, staged, record = prepared
    os.replace(staged, target)
    assert inspect(record, 'other-conversation') == ('unknown', 'evidence_conflict')
    for key, value in [('taskId', 'other-task'), ('postDigest', '0' * 64), ('rootIdentity', [1, 2])]:
        assert inspect({**record, key: value}) == ('unknown', 'evidence_conflict')


def test_committed_receipt_survives_later_edit_and_missing_pending_record(prepared, monkeypatch):
    from lib import file_history
    from lib.project_mod.write_recovery import recover_pending_writes
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    base, target, staged, record = prepared
    os.replace(staged, target)
    assert recover_pending_writes(str(base)) == 1
    target.write_bytes(b'new user content')
    assert inspect(record) == ('file_published', 'undo_committed')
    assert target.read_bytes() == b'new user content'
    session = mods._get_session_dir(str(base))
    rows = mods._load_from_disk(session)
    rows[0]['postImage']['digest'] = '0' * 64
    mods._atomic_json_write(os.path.join(session, 'modifications.json'), {'modifications': rows})
    assert inspect(record) == ('unknown', 'evidence_conflict')


def test_missing_journal_does_not_create_one(prepared, tmp_path):
    _, _, _, record = prepared
    record = {**record, 'basePath': str(tmp_path / 'absent')}
    session = mods._get_session_dir(record['basePath'], create=False)
    assert inspect(record) == ('unknown', 'missing_evidence')
    assert not Path(session).exists()


def test_same_thread_writer_cannot_be_misread_as_not_published(prepared):
    from lib.project_mod import journal_locks
    base, _, _, record = prepared
    with journal_locks.path_locks(mods._get_session_dir(str(base)), [record['path']]):
        assert inspect(record) == ('unknown', 'writer_active')


def test_corrupt_journal_is_unknown_and_preserved(prepared):
    base, target, _, record = prepared
    journal = Path(mods._get_session_dir(str(base))) / 'modifications.json'
    journal.write_text('{broken')
    assert inspect(record) == ('unknown', 'evidence_unavailable')
    assert journal.read_text() == '{broken'
    assert target.read_bytes() == b'old\r\n\xff'


@pytest.mark.parametrize('held', ['same-file', 'other-file'])
def test_other_thread_writer_remains_unknown_until_release(prepared, held):
    import threading
    from lib.project_mod import journal_locks
    base, target, _, record = prepared
    admitted, release = threading.Event(), threading.Event()
    path = record['path'] if held == 'same-file' else 'other.txt'
    def writer():
        with journal_locks.path_locks(mods._get_session_dir(str(base)), [path]):
            admitted.set()
            assert release.wait(3)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert admitted.wait(2)
        # Only a live writer of the inspected file makes its evidence unknown.
        assert inspect(record) == (('unknown', 'writer_active') if held == 'same-file'
                                   else ('file_not_published', 'prepared_file_unpublished'))
        assert target.read_bytes() == b'old\r\n\xff'
    finally:
        release.set(); thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize('conversation_id', ['', None, 'conversation'])
def test_headless_and_conversation_writes_keep_their_journal_identity(
        tmp_path, monkeypatch, conversation_id):
    from lib import file_history
    from lib.project_mod.config import build_root_policy, execution_root_scope
    from lib.project_mod.tool_admission import tool_admission_scope
    from lib.project_mod.write_tools import _ops
    from lib.tasks_pkg.tool_runtime.context import ToolExecutionContext

    base = tmp_path / 'project'
    base.mkdir()
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    task = {'id': 'research-task', 'convId': conversation_id, '_userId': 1}
    context = ToolExecutionContext(task, 1, 'write-call', 'write_file', 1, {})
    with execution_root_scope(task['id'], build_root_policy(str(base))), \
            tool_admission_scope(task, context, tool_name='write_file'):
        result = _ops.tool_write_file(str(base), 'protocol.txt', 'frozen protocol\n',
                                      conv_id=task['id'], task_id=task['id'])
    assert result['ok'], result
    assert (base / 'protocol.txt').read_text() == 'frozen protocol\n'
    records = mods._load_from_disk(mods._get_session_dir(str(base)))
    assert len(records) == 1
    assert records[0]['convId'] == (conversation_id or task['id'])
    assert records[0]['taskId'] == task['id']


@pytest.mark.parametrize('conversation_id', ['', 'conversation'])
@pytest.mark.parametrize('foreign_field', ['convId', 'taskId'])
def test_prepared_write_still_rejects_foreign_occurrence(
        prepared, conversation_id, foreign_field):
    from lib.project_mod.effect_reconciliation import bind_planned_write
    from lib.project_mod.tool_admission import tool_admission_scope
    from lib.tasks_pkg.tool_runtime.context import ToolExecutionContext

    base, target, _, record = prepared
    task = {'id': 'task', 'convId': conversation_id, '_userId': 1}
    context = ToolExecutionContext(task, 1, 'write-call', 'write_file', 1, {})
    attribution = {'conv_id': conversation_id or task['id'], 'task_id': task['id']}
    attribution[{'convId': 'conv_id', 'taskId': 'task_id'}[foreign_field]] = 'foreign'
    with tool_admission_scope(task, context, tool_name='write_file'):
        with pytest.raises(ValueError, match='attribution differs'):
            bind_planned_write(change_id=record['changeId'], base_path=str(base),
                               path=record['path'], post_content=b'new\n', **attribution)
    assert target.read_bytes() == b'old\r\n\xff'


def _tracked_write(tmp_path, monkeypatch, bind):
    from lib import file_history
    from lib.project_mod.config import build_root_policy, execution_root_scope
    from lib.project_mod.tool_admission import tool_admission_scope
    from lib.project_mod.write_tools import _ops
    from lib.tasks_pkg.executor import _effects
    from lib.tasks_pkg.tool_runtime.context import ToolExecutionContext

    base = tmp_path / 'project'
    base.mkdir(exist_ok=True)
    (base / 'file.txt').write_text('old\n')
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    monkeypatch.setattr(_effects, 'bind_tool_effect_reconciliation', bind)
    task = {'id': 'task', 'convId': 'conversation', '_userId': 1}
    context = ToolExecutionContext(task, 1, 'write-call', 'write_file', 1, {})
    with execution_root_scope(task['id'], build_root_policy(str(base))), \
            tool_admission_scope(task, context, tool_name='write_file'):
        result = _ops.tool_write_file(str(base), 'file.txt', 'new\n',
                                      conv_id=task['id'], task_id=task['id'])
    return base, result


def test_reference_ack_does_not_hold_the_project_journal(tmp_path, monkeypatch):
    """A slow storage ACK must not make another writer of the same file wait."""
    import threading
    from lib.project_mod import journal_locks
    monkeypatch.setattr(journal_locks, '_wait_seconds', lambda: .5)
    session = mods._get_session_dir(str(tmp_path / 'project'))
    references, admitted = [], threading.Event()

    def bind(context, reference):
        def other_writer():
            with journal_locks.path_locks(session, ['file.txt']):
                admitted.set()
        thread = threading.Thread(target=other_writer)
        thread.start(); thread.join(3)
        references.append(reference)

    base, result = _tracked_write(tmp_path, monkeypatch, bind)
    assert result['ok'], result
    assert admitted.is_set()
    assert (base / 'file.txt').read_text() == 'new\n'
    [row] = mods._load_from_disk(mods._get_session_dir(str(base)))
    [reference] = references
    assert row['changeId'] == reference['changeId']
    assert inspect_prepared_write(reference, conversation_id='conversation') == (
        'file_published', 'undo_committed')


def test_intent_differing_from_committed_reference_is_never_published(tmp_path, monkeypatch):
    def bind(context, reference):
        reference['rootIdentity'] = [0, 0]  # As if the root were swapped after the ACK.

    base, result = _tracked_write(tmp_path, monkeypatch, bind)
    assert not result['ok']
    assert (base / 'file.txt').read_text() == 'old\n'
    assert mods._load_from_disk(mods._get_session_dir(str(base))) == []
    with intent_store(mods._get_session_dir(str(base))) as store:
        assert store.read() == []
