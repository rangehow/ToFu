"""Publish a validated selected-chat batch through the selected authority."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from .portable import PortableChatError, validate_package


def restore_selected(raw: bytes, *, user_id: int, client=None):
    """Validate before staging files, then use one owner-scoped command.

    ``client`` is an explicit test/composition seam, never selected by archive data.
    """
    if type(user_id) is not int or user_id <= 0:
        raise PortableChatError('invalid_owner')
    package = validate_package(raw)
    chats = [_chat_payload(chat, user_id) for chat in package.chats]
    if client is None:
        from lib.storage import get_storage_client

        client = get_storage_client(write=True)
    from lib.knowledge.store import publish_portable_originals

    verified = _verify_existing_originals(package, user_id=user_id, client=client)
    command_id = _command_id()

    def publish(documents):
        receipt = client.command(
            'conversation.portable_import',
            _command_payload(user_id, chats, documents, verified),
            command_id=command_id,
        )
        _validate_receipt(receipt, chats, documents)
        return receipt

    return publish_portable_originals(
        ((digest, content) for digest, content in package.originals if digest not in verified),
        user_id=user_id, now=time.time(), publish=publish,
    )


def _command_payload(user_id, chats, documents, verified):
    return {'user_id': user_id, 'chats': chats, 'documents': documents,
            'verified_originals': verified}


def _command_id():
    return f'portable-import-{uuid.uuid4().hex}'


def check_text_import_frame(package, *, user_id, client):
    """Use the selected client's fixed-frame preflight, where applicable."""
    check = getattr(client, 'check_command_frame', None)
    if check is not None:
        chats = [_chat_payload(chat, user_id) for chat in package.chats]
        check('conversation.portable_import', _command_payload(user_id, chats, [], {}),
              command_id=_command_id())


def preview_selected(package, *, user_id: int, client):
    """Read owned headers, without staging or commands."""
    chats = [_chat_payload(chat, user_id) for chat in package.chats]
    outcomes = {'candidate': [], 'unchanged': [], 'conflicts': []}
    for chat in chats:
        dest = chat['conversation_id']
        row = client.query('conversation.get', {
            'user_id': user_id, 'conv_id': dest, 'derive_messages': False,
        })
        if row is None:
            outcomes['candidate'].append(dest)
            continue
        witness = row['metadata']['settings'].get('_portableImport')
        keys = ('conversation_id', 'source_id', 'source_conversation_id', 'content_hash')
        matched = isinstance(witness, dict) and all(witness.get(k) == chat[k] for k in keys)
        outcomes['unchanged' if matched else 'conflicts'].append(dest)
    return {**outcomes, 'chats': len(chats), 'messages': sum(len(c['messages']) for c in chats),
            'originals': len(package.originals)}


def _verify_existing_originals(package, *, user_id, client):
    from lib.knowledge.store import (
        KnowledgeIngestError, verify_portable_original,
    )

    verified = {}
    for digest, content in package.originals:
        existing = client.query('knowledge.document.find_digest', {
            'user_id': user_id, 'sha256': digest,
        })
        if existing is not None:
            try:
                verified[digest] = verify_portable_original(
                    existing, user_id=user_id, digest=digest, size=len(content))
            except KnowledgeIngestError as exc:
                raise PortableChatError('attachment_original_missing_or_invalid') from exc

    return verified


def _validate_receipt(receipt, chats, documents):
    """Only a complete settlement can prove a staged candidate was unused."""
    if not isinstance(receipt, dict):
        raise PortableChatError('invalid_restore_receipt')
    fields = [receipt.get(key) for key in ('inserted', 'skipped', 'retained_document_ids')]
    if any(not isinstance(ids, list) or any(not isinstance(item, str) for item in ids)
           or len(set(ids)) != len(ids) for ids in fields):
        raise PortableChatError('invalid_restore_receipt')
    inserted, skipped, retained = map(set, fields)
    if inserted & skipped or inserted | skipped != {chat['conversation_id'] for chat in chats}:
        raise PortableChatError('invalid_restore_receipt')
    needed = {digest for chat in chats if chat['conversation_id'] in inserted
              for message in chat['messages'] for digest in message['originals']}
    expected = {entry['document_id'] for entry in documents
                if entry['document']['sha256'] in needed}
    if retained != expected:
        raise PortableChatError('invalid_restore_receipt')


def _chat_payload(chat, user_id):
    data = json.loads(chat.document)
    identity = json.dumps([user_id, chat.source_id, chat.conversation_id], separators=(',', ':'))
    conversation_id = 'portable_' + hashlib.sha256(identity.encode('ascii')).hexdigest()
    title = data['title'] or 'New Chat'
    if len(title) > 500:
        raise PortableChatError('unsupported_title')
    return {
        'conversation_id': conversation_id,
        'source_id': chat.source_id,
        'source_conversation_id': chat.conversation_id,
        'source_revision': chat.revision,
        'content_hash': chat.content_hash,
        'title': title,
        'project_label': data['projectLabel'],
        'messages': data['messages'],
    }
