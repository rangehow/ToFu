"""Publish portable chat transcripts and their staged originals in one SQL transaction."""

from collections.abc import Mapping
import re
import time
from typing import Any
import uuid

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.base import Session
from lib.storage_authority_sql.operations_pkg._common import (
    _dump, _integer, _load, _required_text,
)
from lib.storage_authority_sql.operations_pkg._conversations import _conversation_create
from lib.storage_authority_sql.operations_pkg._knowledge import (
    _knowledge_document_create, _knowledge_document_find_digest, _safe_stored_name,
    _validated_document,
)
from lib.storage_authority_sql.operations_pkg._turns_core import _append_conversation_change
from lib.turn_projection_segments import projection_with_stable_segments


_PROVENANCE_KEY = '_portableImport'
_REPLAY_KEYS = ('conversation_id', 'source_id', 'source_conversation_id', 'content_hash')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,128}')
_CHAT_KEYS = frozenset({
    'conversation_id', 'title', 'source_id', 'source_conversation_id',
    'source_revision', 'content_hash', 'project_label', 'messages',
})
_MESSAGE_KEYS = frozenset({'role', 'text', 'turnId', 'messageId', 'originals'})
_ACTORS = {'user': 'human', 'assistant': 'assistant'}
_PassiveMessage = tuple[str, dict[str, Any], list[str]]


def _invalid(field: str) -> StorageError:
    return StorageError('database_protocol_error', f'Invalid portable import {field}')


def _digest(value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _invalid('digest')
    return value


def _identity(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise _invalid(key)
    return value


def _objects(items: Any, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        raise _invalid(field)
    return items


def _message(raw: Mapping[str, Any]) -> _PassiveMessage:
    role = raw.get('role')
    text = raw.get('text')
    if (set(raw) - _MESSAGE_KEYS or not isinstance(role, str)
            or role not in _ACTORS or not isinstance(text, str)):
        raise _invalid('message')
    originals = raw.get('originals', [])
    if not isinstance(originals, list):
        raise _invalid('originals')
    ids = {
        key: _identity(raw, key)
        for key in ('turnId', 'messageId') if key in raw
    }
    projection: dict[str, Any] = {'content': text}
    if ids:
        projection[_PROVENANCE_KEY] = ids
    return _ACTORS[role], projection, [_digest(value) for value in originals]


def _provenance(conv_id: str, chat: Mapping[str, Any]) -> dict[str, Any]:
    project_label = chat.get('project_label', '')
    if not isinstance(project_label, str):
        raise _invalid('project_label')
    return {
        # A clone inherits settings, but it must not become a replay witness.
        'conversation_id': conv_id,
        'source_id': _identity(chat, 'source_id'),
        'source_conversation_id': _identity(chat, 'source_conversation_id'),
        'source_revision': _integer(chat, 'source_revision', minimum=0),
        'content_hash': _digest(chat.get('content_hash')),
        'project_label': project_label,
    }


def _candidates(payload: Mapping[str, Any], user_id: int) -> tuple[dict, dict]:
    staged, verified, ids = {}, {}, set()
    for entry in _objects(payload.get('documents', []), 'documents'):
        document = entry.get('document')
        if (set(entry) != {'user_id', 'document_id', 'document'}
                or type(entry['user_id']) is not int or entry['user_id'] != user_id
                or not isinstance(document, Mapping)
                or document.get('scope') != 'attachment'
                or document.get('assets') != [] or document.get('chunks') != []
                or type(document.get('chunk_count')) is not int
                or document['chunk_count'] != 0):
            raise _invalid('staged attachment')
        digest = _digest(document.get('sha256'))
        doc_id = _identity(entry, 'document_id')
        if digest in staged or doc_id in ids or document.get('id') != doc_id:
            raise _invalid('duplicate or mismatched staged attachment')
        _validated_document(entry)
        staged[digest] = entry
        ids.add(doc_id)
    witnesses = payload.get('verified_originals', {})
    if not isinstance(witnesses, Mapping):
        raise _invalid('verified originals')
    for digest, witness in witnesses.items():
        if (_digest(digest) in staged or not isinstance(witness, Mapping)
                or set(witness) != {'document_id', 'stored_name'}):
            raise _invalid('verified original')
        doc_id = _identity(witness, 'document_id')
        if doc_id in ids:
            raise _invalid('duplicate original document identity')
        stored_name = _safe_stored_name(witness)
        if '\x00' in stored_name:
            raise _invalid('verified original basename')
        verified[digest] = (doc_id, stored_name)
        ids.add(doc_id)
    return staged, verified


def _documents(
    session: Session, user_id: int, staged: dict, verified: dict, needed: set[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    from lib.media_attachments import attachment_ref

    if needed - staged.keys() - verified.keys():
        raise _invalid('unresolved original')
    refs, retained = {}, []
    for digest in sorted(needed):
        if digest in staged:
            result = _knowledge_document_create(session, staged[digest])
            if not result['created']:
                raise StorageError('database_conflict', 'Portable original changed')
            document = result['document']
            retained.append(document['id'])
        else:
            doc_id, stored_name = verified[digest]
            session.lock_key('knowledge.document', f'{user_id}:{doc_id}')
            document = _knowledge_document_find_digest(
                session, {'user_id': user_id, 'sha256': digest})
            if (document is None or document['id'] != doc_id
                    or document['stored_name'] != stored_name):
                raise StorageError('database_conflict', 'Portable original changed')
        metadata = _load(document['media_metadata_json'])
        refs[digest] = attachment_ref({**document, 'media_metadata': metadata})
    return refs, retained


def _insert_turns(
    session: Session, conv_id: str, user_id: int, messages: list[_PassiveMessage],
    refs: Mapping[str, dict[str, Any]], now: int,
) -> None:
    parent_id = None
    settlement = _dump({
        'outcome': 'completed', 'cause': 'portable_import', 'resumeOptions': [],
    })
    for ordinal, (actor, projection, digests) in enumerate(messages):
        if digests:
            projection['attachments'] = [refs[digest] for digest in digests]
        projection = projection_with_stable_segments(
            projection, actor=actor, status='completed')
        turn_id = str(uuid.uuid4())
        session.execute(
            'INSERT INTO storage_conversation_turns('
            'turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,'
            'actor,kind,status,projection_json,projection_revision,settlement_json,'
            'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)',
            (turn_id, conv_id, user_id, 'main', parent_id, ordinal, actor,
             'portable_import', 'completed', _dump(projection), settlement, now, now),
        )
        parent_id = turn_id
    count = len(messages)
    if count:
        session.execute(
            'UPDATE storage_conversations SET msg_count=?,rev=? WHERE id=? AND user_id=?',
            (count, count, conv_id, user_id),
        )
        _append_conversation_change(
            session, conversation_id=conv_id, user_id=user_id,
            change_type='conversation.activity',
            payload={'requiresSnapshot': True, 'conversationRevision': count},
        )
        session.worker_hints.conversation_changes.add((user_id, conv_id))


def _conversation_portable_import(session: Session, payload: Mapping[str, Any]) -> Any:
    """Import server-normalized text/original references, never executable state."""
    if (set(payload) - {'user_id', 'chats', 'documents', 'verified_originals'}
            or type(payload.get('user_id')) is not int):
        raise _invalid('batch')
    user_id = _integer(payload, 'user_id', minimum=1)
    staged, verified = _candidates(payload, user_id)
    chats = {}
    for chat in _objects(payload.get('chats'), 'chats'):
        if set(chat) - _CHAT_KEYS:
            raise _invalid('chat')
        conv_id = _identity(chat, 'conversation_id')
        if conv_id in chats:
            raise _invalid('duplicate destination')
        chats[conv_id] = (
            _required_text(chat, 'title', 500), _provenance(conv_id, chat),
            [_message(raw) for raw in _objects(chat.get('messages'), 'messages')],
        )
    # Same lifecycle locks as create/delete, acquired in stable order for batches.
    for conv_id in sorted(chats):
        session.lock_key('conversation', f'{user_id}:{conv_id}')
    inserted, skipped = [], []
    needed = set()
    now = int(time.time() * 1000)
    for conv_id, (title, provenance, messages) in chats.items():
        row = session.fetch_one(
            'SELECT settings_json FROM storage_conversations WHERE id=? AND user_id=?',
            (conv_id, user_id),
        )
        if row is not None:
            settings = _load(row['settings_json'])
            witness = settings.get(_PROVENANCE_KEY) if isinstance(settings, Mapping) else None
            if not isinstance(witness, Mapping) or any(
                witness.get(key) != provenance[key] for key in _REPLAY_KEYS
            ):
                raise StorageError('database_conflict', 'Conversation already exists')
            skipped.append(conv_id)
            continue
        _conversation_create(session, {
            'conv_id': conv_id, 'user_id': user_id, 'title': title,
            'created_at': now, 'settings': {_PROVENANCE_KEY: provenance},
        })
        inserted.append(conv_id)
        needed.update(digest for _, _, digests in messages for digest in digests)
    refs, retained = _documents(session, user_id, staged, verified, needed)
    for conv_id in inserted:
        _insert_turns(session, conv_id, user_id, chats[conv_id][2], refs, now)
    return {'inserted': inserted, 'skipped': skipped, 'retained_document_ids': retained}
