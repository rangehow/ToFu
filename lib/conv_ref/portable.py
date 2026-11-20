"""Selected-chat archive codec and pure import planner; no execution/storage writes.

Uses the complete transcript reader and owner-scoped original-source resolver.
The caller must approve the selected public prose and each durable original.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

FORMAT = 'tofu.selected-chats.v1'
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_SHA = re.compile(r'[0-9a-f]{64}')
_COVERAGE = 'public-string-messages-and-approved-durable-originals-only'

def _read_all_messages(conversation_id, *, user_id):
    from ._export import _read_all_messages as read

    return read(conversation_id, user_id=user_id)


class PortableChatError(ValueError):
    """Invalid, incomplete or conflicting portability input."""


def _require(ok, reason='malformed_package'):
    if not ok:
        raise PortableChatError(reason)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True,
                      allow_nan=False).encode('ascii')


def _canonical_chunks(value):
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(',', ':'),
                               sort_keys=True, allow_nan=False)
    return (piece.encode('ascii') for piece in encoder.iterencode(value))


def _matches_canonical(value, raw):
    offset = 0
    for piece in _canonical_chunks(value):
        if not raw.startswith(piece, offset):
            return False
        offset += len(piece)
    return offset == len(raw)


def _manifest_hash(manifest):
    digest = hashlib.sha256()
    for piece in _canonical_chunks(manifest):
        digest.update(piece)
    return digest.hexdigest()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _shape(value, *keys):
    _require(type(value) is dict and set(value) == set(keys))


def _id(value):
    _require(type(value) is str and _ID.fullmatch(value))
    return value


def _digest(value):
    _require(type(value) is str and _SHA.fullmatch(value))
    return value


def _original(document_id, *, user_id):
    from lib.knowledge import read_source_path

    path = read_source_path(document_id, user_id=user_id)
    _require(path is not None, 'attachment_original_missing')
    from lib.artifacts.file_snapshot import read_project_snapshot

    try:
        snapshot = read_project_snapshot(str(path), [str(path.parent)],
                                         include_bytes=True, fingerprint=False, allow_empty=True)
        return snapshot.content
    except (OSError, ValueError) as exc:
        raise PortableChatError('attachment_original_unreadable') from exc


def _project_messages(messages, approved, blobs, *, user_id, originals):
    result = []
    for message in messages:
        _require(type(message) is dict and message.get('role') in ('user', 'assistant')
                 and type(message.get('content')) is str, 'unsupported_message')
        references = []
        for attachment in message.get('attachments') or []:
            _require(type(attachment) is dict, 'unsupported_attachment')
            document = attachment.get('attachmentId')
            if not isinstance(document, str) or document not in approved:
                continue
            _id(document)
            if document not in originals:
                raw = _original(document, user_id=user_id)
                digest = _hash(raw)
                originals[document] = digest
                blobs[digest] = base64.b64encode(raw).decode('ascii')
            references.append(originals[document])
        entry = {'role': message['role'], 'text': message['content'], 'originals': references}
        for key, value in (('turnId', message.get('_turnId', message.get('turnId'))),
                           ('messageId', message.get('messageId'))):
            if value is not None:
                entry[key] = _id(value)
        result.append(entry)
    return result


def export_selected(*, source_id: str, user_id: int, conversation_ids,
                    approved_original_ids=(), project_labels: Mapping[str, str] | None = None):
    """Return deterministic archive bytes; caller supplies stable source namespace.

    This is a library entry point, not a route or automatic sync service.
    Project labels are explicitly supplied display prose, not live project keys.
    """
    _id(source_id)
    _require(type(user_id) is int and user_id > 0, 'invalid_owner')
    ids = sorted({_id(value) for value in conversation_ids})
    approved = {_id(value) for value in approved_original_ids}
    chats, blobs, originals = [], {}, {}
    for conversation_id in ids:
        snapshot, messages = _read_all_messages(conversation_id, user_id=user_id)
        chats.append({
            'id': conversation_id, 'revision': snapshot['rev'],
            'title': snapshot.get('title') or '',
            'projectLabel': (project_labels or {}).get(conversation_id, ''),
            'messages': _project_messages(messages, approved, blobs, user_id=user_id,
                                          originals=originals),
        })
    _require(set(originals) == approved, 'approved_attachment_not_in_selection')
    manifest = {'format': FORMAT, 'source': source_id, 'coverage': _COVERAGE,
                'chats': chats, 'originals': blobs}
    raw = _canonical({'manifest': manifest, 'sha256': _manifest_hash(manifest)})
    validate_package(raw)
    return raw


@dataclass(frozen=True)
class ChatImport:
    source_id: str
    conversation_id: str
    revision: int
    content_hash: str
    document: bytes


@dataclass(frozen=True)
class ValidatedPackage:
    chats: tuple[ChatImport, ...]
    originals: tuple[tuple[str, bytes], ...]


def _check_chat(chat):
    _shape(chat, 'id', 'revision', 'title', 'projectLabel', 'messages')
    _id(chat['id'])
    _require(type(chat['revision']) is int and chat['revision'] >= 0)
    _require(type(chat['title']) is str and type(chat['projectLabel']) is str)
    _require(type(chat['messages']) is list)
    referenced = set()
    for message in chat['messages']:
        optional = [key for key in ('turnId', 'messageId') if key in message] if type(message) is dict else []
        _shape(message, 'role', 'text', 'originals', *optional)
        for key in optional:
            _id(message[key])
        _require(message['role'] in ('user', 'assistant') and type(message['text']) is str)
        _require(type(message['originals']) is list)
        referenced.update(_digest(value) for value in message['originals'])
    return referenced


def validate_package(raw: bytes) -> ValidatedPackage:
    """Validate all structure, identities and bytes before producing any plan."""
    _require(type(raw) is bytes)
    try:
        archive = json.loads(raw)
        _shape(archive, 'manifest', 'sha256')
        _require(_matches_canonical(archive, raw))  # rejects duplicate keys/trailing bytes
        return _validate_manifest(archive['manifest'], archive['sha256'])
    except PortableChatError:
        raise
    except (ValueError, TypeError, RecursionError, binascii.Error) as exc:
        raise PortableChatError('malformed_package') from exc


def _validate_manifest(manifest, digest):
    _shape(manifest, 'format', 'source', 'coverage', 'chats', 'originals')
    _require(_manifest_hash(manifest) == _digest(digest), 'manifest_hash_mismatch')
    _require(manifest['format'] == FORMAT and manifest['coverage'] == _COVERAGE)
    source = _id(manifest['source'])
    _require(type(manifest['chats']) is list and type(manifest['originals']) is dict)
    seen, referenced, chats = set(), set(), []
    for chat in manifest['chats']:
        referenced.update(_check_chat(chat))
        identity = chat['id']
        _require(identity not in seen, 'duplicate_source_conversation')
        seen.add(identity)
        document = _canonical(chat)
        content = _canonical({key: val for key, val in chat.items() if key != 'revision'})
        chats.append(ChatImport(source, identity, chat['revision'], _hash(content), document))
    _require(referenced == set(manifest['originals']), 'attachment_coverage_mismatch')
    originals = []
    for key, value in sorted(manifest['originals'].items()):
        _require(type(value) is str)
        data = base64.b64decode(value, validate=True)
        _require(_hash(data) == key and base64.b64encode(data).decode('ascii') == value,
                 'attachment_hash_mismatch')
        originals.append((key, data))
    return ValidatedPackage(tuple(sorted(chats, key=lambda chat: chat.conversation_id)),
                            tuple(originals))


def plan_import(raw: bytes, *, existing: Mapping[tuple[str, str], str]) -> ValidatedPackage:
    """Insert only. Existing values are committed provenance digests, not local edits.

    Read and compare them inside the destination's eventual import transaction.
    Unknown identities are admitted; a divergent identity refuses the whole batch.
    """
    return _plan(validate_package(raw), existing)


def import_selected(raw: bytes, *, user_id: int, transaction) -> ValidatedPackage:
    """Stage an insert-only batch using an explicitly supplied destination port.

    The trusted port must scope by owner, lock provenance, publish all originals
    and chats atomically, and roll back on exception. No production port is
    registered by this module. Archive data never selects or configures the port.
    """
    _require(type(user_id) is int and user_id > 0, 'invalid_owner')
    validated = validate_package(raw)
    identities = tuple((c.source_id, c.conversation_id) for c in validated.chats)
    with transaction(user_id=user_id) as destination:
        additions = _plan(validated, destination.lock_provenance(identities))
        if additions.chats:
            destination.insert_passive_batch(additions)
    return additions


def _plan(validated, existing):
    additions, referenced = [], set()
    for chat in validated.chats:
        identity = (chat.source_id, chat.conversation_id)
        if identity in existing:
            _require(existing[identity] == chat.content_hash, 'source_content_conflict')
            continue
        additions.append(chat)
        referenced.update(_check_chat(json.loads(chat.document)))
    return ValidatedPackage(tuple(additions), tuple(item for item in validated.originals
                                                    if item[0] in referenced))
