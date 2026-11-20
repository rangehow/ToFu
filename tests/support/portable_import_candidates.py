"""Synthetic candidate-boundary assertions shared by the selected authorities."""

import pytest

from lib.storage.errors import StorageError


def assert_candidate_contract(client, user_id):
    _assert_message_provenance_contract(client, user_id)
    digest = '9' * 64
    document = {
        'id': 'candidate-contract-doc', 'sha256': digest,
        'name': 'fixture.bin', 'stored_name': 'fixture.bin', 'kind': '.bin',
        'scope': 'attachment', 'method': 'portable_import', 'size_bytes': 7,
        'text_chars': 0, 'chunk_count': 0, 'chunks': [], 'assets': [],
        'created_at': 1, 'updated_at': 1,
    }
    stage = {'user_id': user_id, 'document_id': document['id'], 'document': document}
    chat = {
        'conversation_id': 'candidate-contract-first', 'source_id': 'fixture',
        'source_conversation_id': 'fixture-chat', 'source_revision': 1,
        'content_hash': '8' * 64, 'title': 'Candidate fixture', 'project_label': '',
        'messages': [{'role': 'user', 'text': 'Fixture', 'originals': [digest]}],
    }
    payload = {'user_id': user_id, 'chats': [chat], 'documents': [stage]}
    digest_query = {'user_id': user_id, 'sha256': digest}

    def reject(payload, label, code):
        with pytest.raises(StorageError) as error:
            client.command('conversation.portable_import', payload, f'candidate-{label}')
        assert error.value.code == code
        for chat in payload['chats']:
            assert client.query('conversation.get', {
                'user_id': user_id, 'conv_id': chat['conversation_id'],
            }) is None

    for label, bad_stage in {
        'stage-owner': {**stage, 'user_id': user_id + 1},
        'stage-id': {**stage, 'document_id': 'different-id'},
        'stage-chunks': {**stage, 'document': {**document, 'chunks': [{}]}},
        'stage-chunk-count': {**stage, 'document': {**document, 'chunk_count': 1}},
        'stage-assets': {**stage, 'document': {**document, 'assets': [{}]}},
    }.items():
        reject({**payload, 'documents': [bad_stage]}, label, 'database_protocol_error')
        reject({**payload, 'chats': [], 'documents': [bad_stage]},
               f'unused-{label}', 'database_protocol_error')
    reject({**payload, 'documents': [stage, stage]}, 'duplicate', 'database_protocol_error')
    assert client.query('knowledge.document.find_digest', digest_query) is None

    first = client.command('conversation.portable_import', payload, 'candidate-publish')
    assert first['retained_document_ids'] == [document['id']]
    another = {**payload, 'chats': [{**chat, 'conversation_id': 'candidate-contract-second'}]}
    reject({**another, 'documents': []}, 'unverified-existing', 'database_protocol_error')
    reject(another, 'unexpected-dedup', 'database_conflict')
    witness = {'document_id': document['id'], 'stored_name': document['stored_name']}
    reject({**another, 'verified_originals': {digest: witness}},
           'ambiguous-candidate', 'database_protocol_error')
    use_verified = {**another, 'documents': [], 'verified_originals': {digest: witness}}
    for field in witness:
        reject({**use_verified, 'verified_originals': {digest: {**witness, field: 'different'}}},
               f'wrong-{field}', 'database_conflict')
    assert client.command('conversation.portable_import', use_verified, 'candidate-verified') == {
        'inserted': ['candidate-contract-second'], 'skipped': [], 'retained_document_ids': [],
    }


def _assert_message_provenance_contract(client, user_id):
    operation = 'conversation.portable_import'
    conversation_id = 'candidate-contract-provenance'
    identity = {'user_id': user_id, 'conv_id': conversation_id}
    message = {'role': 'user', 'text': 'Provenance fixture'}
    chat = {
        'conversation_id': conversation_id, 'source_id': 'fixture',
        'source_conversation_id': 'fixture-provenance', 'source_revision': 1,
        'content_hash': '7' * 64, 'title': 'Provenance fixture',
    }
    for field in ('turnId', 'messageId'):
        rejected = {**chat, 'messages': [{**message, field: None}]}
        with pytest.raises(StorageError) as error:
            client.command(operation, {'user_id': user_id, 'chats': [rejected]},
                           f'candidate-null-{field}')
        assert error.value.code == 'database_protocol_error'
        assert client.query('conversation.get', identity) is None

    accepted = {**chat, 'messages': [
        {**message, 'text': 'No source IDs'},
        {**message, 'text': 'Only turn ID', 'turnId': 'source-turn'},
        {**message, 'text': 'Only message ID', 'messageId': 'source-message'},
    ]}
    assert client.command(operation, {'user_id': user_id, 'chats': [accepted]},
                          'candidate-optional-provenance') == {
        'inserted': [conversation_id], 'skipped': [], 'retained_document_ids': [],
    }
    stored = client.query('conversation.get', identity)['messages']
    assert [row['content'] for row in stored] == [row['text'] for row in accepted['messages']]
