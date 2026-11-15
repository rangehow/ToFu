"""A snapshot of the incident's size is archived, adopted and read back after a restart.

Incident 2026-11-12: the Tofu Project Router's cold-rebuilt context had 8,386
messages (about 6.06M estimated tokens, about 70 MB of JSON), past the native
64 MiB ``compaction_archives`` document bound. ``compaction_archive.create``
refused the whole snapshot as a non-retryable ``database_protocol_error``, so
every compaction, and so every Turn, failed. ``test_compaction_archive_bound.py``
proves the fit at patched budgets; this runs the unpatched contract budget at
the incident's size through a real authority, across a storage restart. About
70 MB per backend, so it runs in the expensive deterministic tier
(``make test-slow``).
"""
import uuid
from pathlib import Path

import pytest

from lib.agent_core.storage_errors import StorageError
from lib.storage.protocol import encode_json_value
from lib.tasks_pkg.compaction import _archive as archive_owner
from lib.tasks_pkg.compaction._archive import _archive_transcript

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = 8_386
TOOL_OUTPUT = 'compiler output line with evidence\n' * 240


def _snapshot():
    return [{'role': 'tool', 'tool_call_id': f'call-{number:05d}', 'content': f'#{number} ' + TOOL_OUTPUT}
            if number % 2 else {'role': 'assistant', 'content': f'#{number} ' + TOOL_OUTPUT}
            for number in range(MESSAGES)]


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
def test_an_incident_size_snapshot_is_archived_adopted_and_read_back_after_a_restart(
        monkeypatch, tmp_path, backend):
    import lib.agent_core.store
    from lib.storage import StorageSupervisor
    from lib.tasks_pkg import persistence_store
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt

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
        events = []
        monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                            lambda task, event: events.append(event))
        supervisor.client.command('conversation.create', {'conv_id': 'router', 'user_id': 1},
                                  uuid.uuid4().hex)
        source = _snapshot()
        assert len(encode_json_value(source)) > archive_owner.MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES
        if backend == 'tofudb':
            # What production refused: the whole snapshot as one archive document.
            with pytest.raises(StorageError) as refused:
                store.archive_transcript('router', source, user_id=1, trigger='window')
            assert refused.value.code == 'database_protocol_error'
            assert refused.value.retryable is False

        task = {'id': 'router-task', 'convId': 'router', '_turnId': 'router-turn',
                '_userId': 1, 'aborted': False, 'model': 'router-model'}
        archive_id = _archive_transcript('router', source, user_id=1, task=task,
                                         trigger='window', tokens_before=6_060_977,
                                         msgs_before=MESSAGES)
        assert archive_id
        assert [event['type'] for event in events] == ['compaction']
        summary = 'The cold rebuild covered the project review and its Worker results. ' * 300
        post = [{'role': 'user', 'content': summary}, *source[-24:]]
        receipt = build_compaction_receipt(
            trigger='window', status='completed', strategy='selective_summary',
            implementation='model_summary', summary_generated=True, summary_text=summary)
        store.update_archive_summary(archive_id, summary, 60_000, len(post), user_id=1,
                                     receipt=receipt, post_messages=post)
        supervisor.stop()
        supervisor.start()

        saved = store.get_compaction_archive('router', archive_id, user_id=1)
        messages = saved['messages']
        boundary = messages[0]['_compactionArchiveBoundary']
        assert boundary['totalMessages'] == MESSAGES
        assert 0 < boundary['omittedMessages'] < MESSAGES
        assert messages[1:] == source[boundary['omittedMessages']:]
        assert len(encode_json_value(messages)) <= archive_owner.ARCHIVE_MESSAGES_BUDGET_BYTES
        assert saved['archive']['msgsBefore'] == MESSAGES
        assert saved['archive']['summary'] == summary
        assert saved['archive']['receipt']['status'] == 'completed'
        view = store.get_compaction_archive('router', archive_id, user_id=1,
                                            include_messages=False, include_post_messages=True)
        assert view['postMessages'] == post
        assert task['_compaction_archives'][0]['messages'] == source
    finally:
        supervisor.stop()
