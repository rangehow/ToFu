"""Selected-chat package through the actual embedded, source-pinned native authority."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from tests.conversation.test_portable_chat_package import source  # noqa: F401
from tests.support.portable_import_candidates import assert_candidate_contract
__all__ = ['source']

pytestmark = pytest.mark.api


def test_portable_roundtrip_native(source, tmp_path, monkeypatch):
    from tofu_db.embedded import EmbeddedRuntime
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.storage import service
    from lib.storage.errors import StorageError
    from lib.knowledge import store
    from lib.conv_ref import _detail, _export
    from lib.conv_ref.portable import validate_package
    from lib.conv_ref.portable_import import _chat_payload, restore_selected
    from serverctl_pkg.native_engine import source_fingerprint

    binary = os.environ.get('TOFUDB_TEST_BINARY')
    library = os.environ.get('TOFUDB_TEST_LIBRARY')
    if not binary or not library:
        pytest.skip('private source-matched native engine required')
    expected = source_fingerprint(Path.cwd())
    export, chats, revision, original_path, original = source
    raw = export()
    revision[0] += 1
    delta = export(('chat-a', 'chat-b'))
    chats['chat-a'][0]['content'] = 'changed'
    conflict = export(('chat-a', 'chat-b'))
    late_bytes = b'uncommitted synthetic original'
    original_path.write_bytes(late_bytes)
    chats['chat-c'] = [{'role': 'user', 'content': 'Roll back first chat',
                        'attachments': [{'attachmentId': 'allowed'}]}]
    chats['chat-d'] = [{'role': 'user', 'content': 'Colliding second chat'}]
    late_failure = export(('chat-c', 'chat-d'))
    staged_ids = [_chat_payload(chat, 71)['conversation_id']
                  for chat in validate_package(late_failure).chats]
    destination = tmp_path / 'authority'
    destination.mkdir()
    build_info = json.loads(subprocess.check_output([binary, '--build-info']))
    assert expected and build_info['source_sha256'] == expected
    subprocess.run([binary, 'init', '--data-dir', str(destination)], check=True, capture_output=True)
    monkeypatch.setattr(store, '_SOURCE_ROOT_OVERRIDE', str(tmp_path / 'knowledge'))
    monkeypatch.setattr(_export, '_read_conversation_snapshot',
                        _detail._read_conversation_snapshot)
    with EmbeddedRuntime(Path(library), destination, owner_id=71, tenant_id=1, secret=b'x'*32) as native:
        assert native.build_identity()['source_sha256'] == expected
        client = EmbeddedStorageV2Client(native, b'x'*32, owner_id=71, tenant_id=1)
        monkeypatch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=lambda **_: client))
        first = restore_selected(raw, user_id=71, client=client)
        conversation_id, = first['inserted']
        document_id, = first['retained_document_ids']
        assert store.read_source_path(document_id, user_id=71).read_bytes() == original
        assert restore_selected(raw, user_id=71, client=client)['skipped'] == [conversation_id]
        with pytest.raises(StorageError) as refused:
            restore_selected(conflict, user_id=71, client=client)
        assert refused.value.code == 'database_conflict'
        assert len(restore_selected(delta, user_id=71, client=client)['inserted']) == 1
        rendered, count = _export.render_full_transcript(conversation_id, user_id=71)
        assert count == 2 and '你好' in rendered and 'Synthetic answer' in rendered
        assert 'Imported original' in rendered and 'pending-command' not in rendered
        state = client.query('conversation.get', {'user_id': 71, 'conv_id': conversation_id})
        assert not state['metadata']['settings'].get('activeTaskId')
        assert all(not msg.get('_attemptId') and not msg.get('_commandPending')
                   for msg in state['messages'])
        # Occupy the second batch member's Turn ID in an unrelated chat. The
        # collision occurs after the first chat/Turn were written to the transaction.
        collision = 'pt_' + hashlib.sha256(f'{staged_ids[1]}:0'.encode()).hexdigest()
        client.command('turn.append_settled', {
            'user_id': 71, 'conversation_id': 'collision-fixture', 'turn_id': collision,
            'actor': 'human', 'projection': {'content': 'synthetic collision'},
            'command_id': 'collision-fixture', 'conversation_defaults': {'allowCreate': True},
        }, 'collision-fixture')
        with pytest.raises(StorageError) as late_refusal:
            restore_selected(late_failure, user_id=71, client=client)
        assert late_refusal.value.code == 'database_conflict'
        for dest_id in staged_ids:
            assert client.query('conversation.get', {'user_id': 71, 'conv_id': dest_id}) is None
        assert client.query('knowledge.document.find_digest', {
            'user_id': 71, 'sha256': hashlib.sha256(late_bytes).hexdigest(),
        }) is None
        assert_candidate_contract(client, 71)
