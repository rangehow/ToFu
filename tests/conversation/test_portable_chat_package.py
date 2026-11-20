"""Synthetic round trips for selected chat portability; no real authority."""
import hashlib
import json

import pytest

from lib.conv_ref import _export, portable

pytestmark = pytest.mark.unit


@pytest.fixture
def source(monkeypatch, tmp_path):
    from lib import knowledge

    content = b'\x00\xffsynthetic artifact\n'
    original = tmp_path / 'owned.bin'
    original.write_bytes(content)
    chats = {'chat-a': [
        {'role': 'user', 'content': '你好', '_turnId': 'source-turn',
         'attachments': [{'attachmentId': 'allowed', 'sourceUrl': 'private-url'},
                         {'attachmentId': 'not-approved'}]},
        {'role': 'assistant', 'content': 'Synthetic answer', 'messageId': 'message-b',
         'thinking': 'private-thinking', 'toolRounds': [{'command': 'pending-command'}],
         '_responses_encrypted_content': 'fake-ciphertext', 'grant': 'live-grant'},
    ], 'chat-b': [{'role': 'user', 'content': 'New selection'}]}
    rev = [3]

    def read(conversation_id, *, user_id, message_window, before_sequence=None):
        assert user_id == 71
        messages = chats[conversation_id]
        end = len(messages) if before_sequence is None else before_sequence
        return {'messages': messages[max(0, end - message_window):end], 'rev': rev[0],
                'msg_count': len(messages), 'title': 'Synthetic chat',
                'settings': {'apiKey': 'fake-api-key', 'oauth': 'fake-oauth-token'}}

    def resolve(document_id, *, user_id):
        assert document_id == 'allowed' and user_id == 71
        return original if original.is_file() else None

    monkeypatch.setattr(knowledge, 'read_source_path', resolve)
    monkeypatch.setattr(_export, '_read_conversation_snapshot', read)

    def export(ids=('chat-a',), originals=('allowed',)):
        return portable.export_selected(source_id='synthetic-origin', user_id=71,
                                        conversation_ids=ids, approved_original_ids=originals,
                                        project_labels={'chat-a': 'Display only'})

    return export, chats, rev, original, content


def test_exact_original_and_public_history_only(source):
    export, _, _, _, original = source
    raw = export()
    assert raw == export()
    for excluded in (b'private-url', b'not-approved', b'private-thinking', b'pending-command',
                     b'fake-ciphertext', b'live-grant', b'fake-api-key', b'fake-oauth-token'):
        assert excluded not in raw
    imported = portable.plan_import(raw, existing={})
    chat, = imported.chats
    digest = hashlib.sha256(original).hexdigest()
    assert imported.originals == ((digest, original),)
    assert chat.source_id == 'synthetic-origin'
    assert json.loads(chat.document) == {
        'id': 'chat-a', 'revision': 3, 'title': 'Synthetic chat', 'projectLabel': 'Display only',
        'messages': [{'role': 'user', 'text': '你好', 'turnId': 'source-turn', 'originals': [digest]},
                     {'role': 'assistant', 'text': 'Synthetic answer', 'messageId': 'message-b',
                      'originals': []}],
    }


def test_delta_replay_and_conflict_do_not_return_updates(source):
    export, chats, rev, *_ = source
    first, = portable.plan_import(export(), existing={}).chats
    existing = {(first.source_id, first.conversation_id): first.content_hash}
    assert portable.plan_import(export(), existing=existing) == portable.ValidatedPackage((), ())
    rev[0] += 1  # a revision-only change is not a new portable version
    raw = export(('chat-b', 'chat-a'))
    assert raw == export(('chat-a', 'chat-b'))
    delta = portable.plan_import(raw, existing=existing)
    assert [chat.conversation_id for chat in delta.chats] == ['chat-b']
    assert delta.originals == ()
    chats['chat-a'][0]['content'] = 'Changed portable content'
    with pytest.raises(portable.PortableChatError, match='source_content_conflict'):
        portable.plan_import(export(('chat-b', 'chat-a')), existing=existing)
    assert existing == {(first.source_id, first.conversation_id): first.content_hash}


def _mutate(raw, apply):
    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii')

    archive = json.loads(raw)
    apply(archive['manifest'])
    archive['sha256'] = hashlib.sha256(encode(archive['manifest'])).hexdigest()
    return encode(archive)


@pytest.mark.parametrize('mutation', [
    lambda raw: raw[:-1],
    lambda raw: b'{"manifest":null,' + raw[1:],
    lambda raw: raw.replace(b'Synthetic answer', b'unhashed answer'),
    lambda raw: _mutate(raw, lambda m: m['chats'][0].update(id='../outside')),
    lambda raw: _mutate(raw, lambda m: m['chats'][0]['messages'][1].update(grant='forged')),
    lambda raw: _mutate(raw, lambda m: m['chats'][0]['messages'][0].update(originals=['../../secret'])),
    lambda raw: _mutate(raw, lambda m: m.update(originals={})),
    lambda raw: _mutate(raw, lambda m: m['originals'].update({next(iter(m['originals'])): 'YQ=='})),
])
def test_invalid_package_has_no_import_plan(source, mutation):
    export, *_ = source
    with pytest.raises(portable.PortableChatError):
        portable.plan_import(mutation(export()), existing={})


def test_missing_and_symlink_original_are_failures(source, tmp_path):
    export, _, _, original, content = source
    original.unlink()
    with pytest.raises(portable.PortableChatError, match='attachment_original_missing'):
        export()
    outside = tmp_path / 'elsewhere'
    outside.write_bytes(content)
    original.symlink_to(outside)
    with pytest.raises(portable.PortableChatError, match='attachment_original_unreadable'):
        export()
    assert outside.read_bytes() == content


def test_approval_must_be_linked_to_selected_messages(source):
    export, *_ = source
    with pytest.raises(portable.PortableChatError, match='approved_attachment_not_in_selection'):
        export(('chat-b',))


def test_import_checkpoint_rolls_back_staged_originals_and_never_overwrites(source):
    from contextlib import contextmanager

    export, chats, *_ = source
    committed, opened = {}, []
    fail = [True]

    @contextmanager
    def transaction(*, user_id):
        assert user_id == 91
        opened.append(user_id)
        staged = committed.copy()

        class Destination:
            def lock_provenance(self, identities):
                return {key: staged[key][0] for key in identities if key in staged}

            def insert_passive_batch(self, batch):
                staged.update(batch.originals)
                for chat in batch.chats:
                    staged[(chat.source_id, chat.conversation_id)] = (
                        chat.content_hash, chat.document)
                if fail[0]:
                    raise RuntimeError('synthetic failure before commit')

        yield Destination()
        committed.update(staged)

    def import_package(raw):
        return portable.import_selected(raw, user_id=91, transaction=transaction)

    raw = export()
    with pytest.raises(portable.PortableChatError):
        import_package(raw[:-2])
    assert not opened
    with pytest.raises(RuntimeError, match='synthetic failure'):
        import_package(raw)
    assert not committed
    fail[0] = False
    imported = import_package(raw)
    chat, = imported.chats
    key = (chat.source_id, chat.conversation_id)
    committed[key] = (chat.content_hash, b'local edit')
    assert not import_package(raw).chats
    chats['chat-a'][0]['content'] = 'diverged'
    snapshot = committed.copy()
    with pytest.raises(portable.PortableChatError, match='source_content_conflict'):
        import_package(export(('chat-a', 'chat-b')))
    assert committed == snapshot and committed[key][1] == b'local edit'
