"""Pre-compaction archives stay within the storage authority's document bound.

A cold rebuild of a long-lived conversation's full history grows past the
native ``compaction_archives.maximumDocumentBytes`` bound. Archiving it whole
made every compaction, and so every Turn, fail with a refusal that no retry
could pass, reported as a retryable storage outage.
"""
import builtins
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.agent_core.storage_errors import StorageError
from lib.storage.protocol import encode_json_value
from lib.tasks_pkg.compaction import _archive as archive_owner
from lib.tasks_pkg.compaction._archive import _archive_transcript

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _task():
    return {'id': 'archive-bound-task', 'convId': 'archive-bound-conversation',
            '_turnId': 'archive-bound-turn', '_userId': 41, 'aborted': False,
            'model': 'observed-parent'}


def _store(monkeypatch, refusal=None):
    events, stored = [], []

    def archive_transcript(conv_id, messages, **kwargs):
        if refusal is not None:
            raise refusal
        stored.append({'conv_id': conv_id, 'messages': messages, **kwargs})
        return 'archive-bound-id'

    monkeypatch.setattr('lib.agent_core.store.get_conversation_store', lambda: SimpleNamespace(
        archive_transcript=archive_transcript, prune_archives=lambda *a, **k: 0))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, event: events.append(event))
    return events, stored


def _bytes(value):
    return len(encode_json_value(value))


def _archive(task, source):
    return _archive_transcript(task['convId'], source, user_id=41, task=task,
                               trigger='window', round_num=3,
                               tokens_before=6_065_647, msgs_before=len(source))


def test_archive_bounds_are_the_storage_contract_bounds():
    contract = json.loads((ROOT / 'contracts/tofudb_ir_v1.json').read_text(encoding='utf-8'))
    table = next(table for table in contract['logicalTables']
                 if table['name'] == 'compaction_archives')
    fields = {field['name']: field for field in table['fields']}
    document_bound = table['maximumDocumentBytes']
    summary_characters = fields['summary']['maximumCharacters']
    receipt_bytes = fields['receipt']['maximumBytes']
    assert archive_owner.MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES == document_bound
    assert archive_owner.MAX_COMPACTION_ARCHIVE_SUMMARY_CHARACTERS == summary_characters
    assert archive_owner.MAX_COMPACTION_ARCHIVE_RECEIPT_BYTES == receipt_bytes
    # A whole archive is read back in one bounded response: its messages leave
    # room for a maximal summary (every character a \u00XX escape) and receipt.
    maximal_summary = _bytes('\x01' * summary_characters)
    assert (archive_owner.ARCHIVE_MESSAGES_BUDGET_BYTES + maximal_summary + receipt_bytes
            <= document_bound)


def test_snapshot_at_the_budget_is_archived_unchanged(monkeypatch):
    _, stored = _store(monkeypatch)
    source = [{'role': 'user', 'content': f'message {number} ' + 'é' * number}
              for number in range(9)]
    monkeypatch.setattr(archive_owner, 'ARCHIVE_MESSAGES_BUDGET_BYTES', _bytes(source))

    assert _archive(_task(), source) == 'archive-bound-id'

    assert stored[0]['messages'] is source
    assert stored[0]['msgs_before'] == len(source)


def test_archive_sizing_runs_without_host_storage(monkeypatch):
    """Storage-free runtimes (the tofu-agent wheel ships no lib.storage) size archives too."""
    _, stored = _store(monkeypatch)
    importing = builtins.__import__

    def storage_free(name, *args, **kwargs):
        if name == 'lib.storage' or name.startswith('lib.storage.'):
            raise ModuleNotFoundError(f'No module named {name!r}', name=name)
        return importing(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', storage_free)
    source = [{'role': 'user', 'content': 'Archive this without the storage client.'}]

    assert _archive(_task(), source) == 'archive-bound-id'

    assert stored[0]['messages'] is source


def test_oversized_snapshot_keeps_the_exact_newest_suffix_behind_one_notice(monkeypatch):
    source = [{'role': 'tool', 'tool_call_id': f'call-{number:03d}', 'content': 'é' * 500}
              for number in range(40)]
    message_bytes = _bytes(source[0])
    assert all(_bytes(message) == message_bytes for message in source)
    # Size the bound from an observed notice so exactly ``kept`` messages fit
    # with half a message to spare; the notice's digits cannot cross that margin.
    _, probe = _store(monkeypatch)
    monkeypatch.setattr(archive_owner, 'ARCHIVE_MESSAGES_BUDGET_BYTES', _bytes(source) // 2)
    _archive(_task(), source)
    kept = 12
    bound = 2 + _bytes(probe[0]['messages'][0]) + kept * (message_bytes + 1) + message_bytes // 2
    monkeypatch.setattr(archive_owner, 'ARCHIVE_MESSAGES_BUDGET_BYTES', bound)
    events, stored = _store(monkeypatch)
    original = deepcopy(source)
    task = _task()

    assert _archive(task, source) == 'archive-bound-id'

    archived = stored[0]['messages']
    notice, suffix = archived[0], archived[1:]
    assert suffix == source[-kept:]
    assert all(kept_message is message for kept_message, message in zip(suffix, source[-kept:]))
    assert _bytes(archived) <= bound
    assert notice['role'] == 'system'
    assert notice['_compactionArchiveBoundary'] == {
        'totalMessages': 40, 'omittedMessages': 28, 'archivedMessages': kept,
        'omittedBytes': 28 * message_bytes, 'snapshotBytes': _bytes(source),
        'messagesBudgetBytes': bound}
    assert 'oldest 28 messages' in notice['content']
    assert stored[0]['msgs_before'] == 40
    assert source == original
    assert task['_compaction_archives'][0]['messages'] == original
    assert [event['type'] for event in events] == ['compaction']


def test_a_newest_message_larger_than_the_budget_leaves_only_the_notice(monkeypatch):
    _, stored = _store(monkeypatch)
    source = [{'role': 'user', 'content': 'short'}, {'role': 'tool', 'content': 'x' * 50_000}]
    monkeypatch.setattr(archive_owner, 'ARCHIVE_MESSAGES_BUDGET_BYTES', 2_000)

    assert _archive(_task(), source) == 'archive-bound-id'

    archived = stored[0]['messages']
    assert len(archived) == 1
    assert archived[0]['_compactionArchiveBoundary']['omittedMessages'] == 2
    assert _bytes(archived) <= 2_000


def test_a_store_refusal_keeps_its_typed_nonretryable_cause(monkeypatch):
    refusal = StorageError('database_protocol_error', 'archive payload exceeds its bound',
                           retryable=False, operation_id='compaction_archive.create')
    events, _ = _store(monkeypatch, refusal)

    with pytest.raises(StorageError) as caught:
        _archive(_task(), [{'role': 'user', 'content': 'Preserve this source.'}])

    assert caught.value is refusal
    assert [event['type'] for event in events] == ['compaction_done']
    receipt = events[0]['receipt']
    assert receipt['status'] == 'failed'
    assert receipt['outcomeReason'] == 'compaction_archive_unavailable'


def test_an_unencodable_snapshot_fails_with_the_encoders_typed_error(monkeypatch):
    events, stored = _store(monkeypatch)

    with pytest.raises(StorageError) as caught:
        _archive(_task(), [{'role': 'tool', 'content': 'x', 'score': float('nan')}])

    assert caught.value.code == 'database_protocol_error'
    assert caught.value.retryable is False
    assert stored == [] and [event['type'] for event in events] == ['compaction_done']


def test_a_refusal_outside_a_turn_returns_no_archive(monkeypatch):
    events, _ = _store(monkeypatch, StorageError('database_unavailable', 'down', retryable=True))

    assert _archive({**_task(), '_turnId': ''}, [{'role': 'user', 'content': 'x'}]) is None
    assert events == []


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_bounded_archive_round_trips_through_the_selected_authority(
        monkeypatch, tmp_path, backend):
    import uuid
    import lib.agent_core.store
    from lib.storage import StorageSupervisor
    from lib.tasks_pkg import persistence_store

    if backend == 'tofudb':
        from tests.support.tofudb_binary import ensure_engine_binary
        engine = ensure_engine_binary(ROOT)
        if engine is None:
            pytest.skip('explicit native test engine unavailable')
        monkeypatch.setenv('TOFUDB_BIN', str(engine))
    supervisor = StorageSupervisor(project_root=tmp_path, backend=backend, startup_timeout=60)
    supervisor.start()
    try:
        monkeypatch.setattr(persistence_store, '_storage_client',
                            lambda **kwargs: supervisor.client)
        store = persistence_store.DefaultConversationStore()
        monkeypatch.setattr(lib.agent_core.store, 'get_conversation_store', lambda: store)
        supervisor.client.command('conversation.create',
            {'conv_id': 'conversation', 'user_id': 1}, uuid.uuid4().hex)
        source = [{'role': 'assistant' if number % 2 else 'user',
                   'content': f'turn {number} "quoted" \\ é中\n\t' + 'evidence ' * 40,
                   'score': 1.5e-05, 'large': 1e16, 'flags': [True, None, number]}
                  for number in range(30)]
        bound = _bytes(source) // 3
        monkeypatch.setattr(archive_owner, 'ARCHIVE_MESSAGES_BUDGET_BYTES', bound)

        archive_id = _archive_transcript('conversation', source, user_id=1,
                                         trigger='window', msgs_before=len(source))

        saved = store.get_compaction_archive('conversation', archive_id, user_id=1)
        messages = saved['messages']
        boundary = messages[0]['_compactionArchiveBoundary']
        assert messages[1:] == source[boundary['omittedMessages']:]
        assert boundary['archivedMessages'] == len(messages) - 1 > 0
        assert saved['archive']['msgsBefore'] == len(source)
        assert _bytes(messages) <= bound
        if backend == 'tofudb':
            # The engine bounds its own canonical encoding of the document.
            assert saved['archive']['payloadSize'] <= _bytes(messages)
    finally:
        supervisor.stop()
