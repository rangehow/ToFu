"""App-side attachment publication against a fake owner-scoped authority."""
import hashlib

import pytest

from lib.conv_ref.portable import PortableChatError
from lib.conv_ref.portable_import import restore_selected
from lib.knowledge import store
from lib.storage.errors import StorageError
from tests.conversation.test_portable_chat_package import source  # noqa: F401
__all__ = ['source']

pytestmark = pytest.mark.unit


@pytest.fixture
def target(monkeypatch, tmp_path):
    base = tmp_path / 'knowledge'
    monkeypatch.setattr(store, '_SOURCE_ROOT_OVERRIDE', str(base))
    path = base / '91' / 'sources'
    path.mkdir(parents=True)
    return path


class Authority:
    existing = None

    def __init__(self, content, outcome):
        self.digest = hashlib.sha256(content).hexdigest()
        self.outcome = outcome
        self.commands = []

    def query(self, operation, payload):
        assert operation == 'knowledge.document.find_digest'
        assert payload == {'user_id': 91, 'sha256': self.digest}
        return self.existing

    def command(self, operation, payload, command_id):
        assert operation == 'conversation.portable_import'
        assert payload['user_id'] == 91 and command_id.startswith('portable-import-')
        self.commands.append(command_id)
        return self.outcome(payload)


@pytest.mark.parametrize('unavailable', ['missing', 'different', 'symlink'])
def test_existing_metadata_without_exact_bytes_refuses_before_dispatch(source, target, unavailable):
    export, _, _, original, content = source
    owned = target / 'existing.bin'
    if unavailable == 'different':
        owned.write_bytes(b'x' * len(content))
    elif unavailable == 'symlink':
        owned.symlink_to(original)
    client = Authority(content, lambda _: pytest.fail('unexpected restore command'))
    client.existing = {'id': 'existing', 'stored_name': owned.name}

    with pytest.raises(PortableChatError, match='attachment_original_missing_or_invalid'):
        restore_selected(export(), user_id=91, client=client)

    assert not client.commands
    assert list(target.iterdir()) == ([] if unavailable == 'missing' else [owned])


def test_restore_uses_only_the_exact_verified_dedup_witness(source, target):
    export, _, _, _, content = source
    owned = target / 'existing.bin'
    owned.write_bytes(content)
    witness = {'id': 'existing', 'stored_name': owned.name}

    def insert(payload):
        chat, = payload['chats']
        assert payload['documents'] == []
        assert payload['verified_originals'] == {
            client.digest: {'document_id': witness['id'], 'stored_name': witness['stored_name']},
        }
        assert list(target.iterdir()) == [owned]
        return {'inserted': [chat['conversation_id']], 'skipped': [], 'retained_document_ids': []}

    client = Authority(content, insert)
    client.existing = witness
    assert restore_selected(export(), user_id=91, client=client)['inserted']
    assert owned.read_bytes() == content


@pytest.mark.parametrize(('outcome', 'error_type', 'preserve'), [
    ('inserted', None, True), ('skipped', None, False),
    ('conflict', StorageError, False), ('predispatch', StorageError, False),
    ('lost_ack', StorageError, True), ('post_commit_codec_error', StorageError, True),
    ('missing_retained_id', PortableChatError, True), ('foreign_retained_id', PortableChatError, True),
    ('incomplete_chat_partition', PortableChatError, True),
])
def test_candidate_disposition_needs_unambiguous_settlement(source, target, outcome, error_type, preserve):
    export, _, _, _, content = source

    def settle(payload):
        chat, = payload['chats']
        entry, = payload['documents']
        assert payload['verified_originals'] == {}
        assert (target / entry['document']['stored_name']).read_bytes() == content
        conv_id = chat['conversation_id']
        if outcome == 'conflict':
            raise StorageError('database_conflict', 'transaction rejected')
        if outcome == 'predispatch':
            raise StorageError('database_unavailable', 'not dispatched', request_not_dispatched=True)
        if outcome == 'lost_ack':
            raise StorageError('database_timeout', 'ACK lost after simulated commit')
        if outcome == 'post_commit_codec_error':
            raise StorageError('database_protocol_error', 'committed-event envelope malformed')
        if outcome == 'skipped':
            return {'inserted': [], 'skipped': [conv_id], 'retained_document_ids': []}
        if outcome == 'incomplete_chat_partition':
            return {'inserted': [], 'skipped': [], 'retained_document_ids': []}
        retained = {'inserted': [entry['document_id']], 'missing_retained_id': [],
                    'foreign_retained_id': ['unrelated-document']}[outcome]
        return {'inserted': [conv_id], 'skipped': [], 'retained_document_ids': retained}

    client = Authority(content, settle)
    if error_type is None:
        restore_selected(export(), user_id=91, client=client)
    else:
        with pytest.raises(error_type):
            restore_selected(export(), user_id=91, client=client)
    assert len(client.commands) == 1  # never replay an ambiguous command
    assert [path.read_bytes() for path in target.iterdir()] == ([content] if preserve else [])


def test_directory_barrier_failure_rolls_back_without_dispatch(source, target, monkeypatch):
    export, _, _, _, content = source
    client = Authority(content, lambda _: pytest.fail('unexpected restore command'))

    def fail_barrier(**kwargs):
        assert [p.read_bytes() for p in target.iterdir()] == [content]
        raise OSError('synthetic directory sync failure')

    monkeypatch.setattr(store, '_sync_publication_directories', fail_barrier)
    with pytest.raises(OSError, match='directory sync failure'):
        restore_selected(export(), user_id=91, client=client)
    assert not client.commands
    assert not list(target.iterdir())
