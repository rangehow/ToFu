"""Atomic segmented project dispatch persistence on the selected SQL authority.

Entry points are the four project.dispatch operations. Reuse versioned document
CAS/codec and the enclosing operation transaction. The project head lock always
precedes child locks. Domain authorization and index contents belong to the
project dispatch repository; this owner enforces scope, bounds and atomicity.
"""
from __future__ import annotations

import hashlib
import re

import orjson

from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC
from lib.storage.protocol import validate_finite_json_numbers
from ._common import _integer, _required_text
from ._records import _record_get_owned, _record_list_owned, _record_put_owned, _record_delete_owned
from ._turns_core import _turn_status_and_settlement

HEADS = SPEC['namespaces']['heads']
DOCUMENTS = SPEC['namespaces']['documents']
LIMITS = SPEC['limits']
_ID = re.compile(r'[A-Za-z0-9._:-]*\Z')


def _invalid(message='Invalid project dispatch storage request'):
    raise StorageError('database_protocol_error', message)


def _owner(payload):
    return _integer(payload, 'user_id', minimum=1, maximum=2**63 - 1)


def _scope(payload):
    owner = _owner(payload)
    project = _required_text(payload, 'project_path', LIMITS['projectPathCharacters'])
    if not project.strip() or '\x00' in project:
        _invalid()
    return owner, project, f'{owner}:{hashlib.sha256(project.encode("utf-8")).hexdigest()}'


def _identity(value, *, empty=False):
    if (not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= LIMITS['identityCharacters']
            or not _ID.fullmatch(value)):
        _invalid()
    return value


def _collection(payload):
    value = payload.get('collection')
    if value not in SPEC['collections']:
        _invalid()
    return value


def _revision(payload, field):
    return _integer(payload, field, minimum=0, maximum=LIMITS['maximumRevision'])


def _document(value, maximum):
    if not isinstance(value, dict):
        _invalid('Dispatch document must be an object')
    # Wire bounds count the caller's canonical JSON. The SQL record encoder may
    # intern projection-shaped values, which must never admit a larger document.
    validate_finite_json_numbers(value)
    try:
        size = len(orjson.dumps(value, option=orjson.OPT_SORT_KEYS))
    except (TypeError, orjson.JSONEncodeError):
        _invalid('Dispatch document is not serializable')
    if size > maximum:
        _invalid('Dispatch document exceeds transport bound')
    return size


def get(session, payload):
    _, _, project_key = _scope(payload)
    identity = _identity(payload.get('id'))
    if payload.get('collection') == 'head':
        if identity != 'root':
            _invalid()
        namespace, key = HEADS, project_key
    else:
        namespace, key = DOCUMENTS, f'{project_key}:{_collection(payload)}:{identity}'
    return _record_get_owned(session, {'namespace': namespace, 'key': key})


def _page(session, payload, namespace, prefix, include_value):
    limit = _integer(payload, 'limit', default=LIMITS['pageRows'], minimum=1, maximum=LIMITS['pageRows'])
    reverse = payload.get('reverse', False)
    if type(reverse) is not bool:
        _invalid('Dispatch ordering must be boolean')
    after = payload.get('after_key', '')
    if (not isinstance(after, str) or len(after) > 512 or not _ID.fullmatch(after)
            or (after and not after.startswith(prefix))):
        _invalid('Dispatch cursor differs from owned prefix')
    return _record_list_owned(session, {'namespace': namespace, 'prefix': prefix,
        'before_key' if reverse else 'after_key': after, 'limit': limit,
        'include_value': include_value}, binary_order=True, reverse=reverse)


def list_documents(session, payload):
    _, _, key = _scope(payload)
    collection = _collection(payload)
    prefix = _identity(payload.get('prefix', ''), empty=True)
    return _page(session, payload, DOCUMENTS, f'{key}:{collection}:{prefix}', collection == 'indexes')


def projects(session, payload):
    return _page(session, payload, HEADS, f'{_owner(payload)}:', True)


def _require_turn_witnesses(session, owner, witnesses):
    if not isinstance(witnesses, list) or len(witnesses) > LIMITS['commitChanges']:
        _invalid('Invalid dispatch Turn witnesses')
    prepared = {}
    for row in witnesses:
        if not isinstance(row, dict) or row.keys() != {
                'conversation_id', 'turn_id', 'attempt_id', 'projection_revision', 'status'}:
            _invalid('Invalid dispatch Turn witness')
        conv, ident, attempt, status = (row[key] for key in ('conversation_id', 'turn_id', 'attempt_id', 'status'))
        for part, empty in ((conv, False), (ident, False), (attempt, True), (status, False)):
            _identity(part, empty=empty)
        if (conv, ident) in prepared:
            _invalid('Duplicate dispatch Turn witness')
        prepared[conv, ident] = (_revision(row, 'projection_revision'), attempt, status)
    for conv in sorted({conv for conv, _ in prepared}):
        session.lock_key('turn_conversation', f'{owner}:{conv}')
    for (conv, ident), expected in sorted(prepared.items()):
        current = session.fetch_one_for_update(
            'SELECT projection_revision, current_attempt_id, status, settlement_json FROM storage_conversation_turns '
            'WHERE conversation_id=? AND turn_id=? AND user_id=?', (conv, ident, owner))
        if current is None or (current['projection_revision'], current['current_attempt_id'] or '',
                               _turn_status_and_settlement(current)[0]) != expected:
            raise StorageError('database_conflict', 'Dispatch Turn witness changed')


def commit(session, payload):
    owner, project, key = _scope(payload)
    changes = payload.get('changes')
    if not isinstance(changes, list) or len(changes) > LIMITS['commitChanges']:
        _invalid()
    # Without expected_revision and head, a document commit updates existing
    # evidence under each document's own CAS and leaves the project head (and
    # every walk it witnesses) untouched.
    headless = 'expected_revision' not in payload and 'head' not in payload
    if (not headless and ('expected_revision' not in payload or 'head' not in payload)
            or headless and not changes):
        _invalid('Dispatch commit needs both expected_revision and head, or neither')
    size = 0
    if not headless:
        expected = _revision(payload, 'expected_revision')
        head = payload.get('head')
        size = _document(head, LIMITS['headBytes'])
    prepared, seen = [], set()
    for change in changes:
        if not isinstance(change, dict):
            _invalid()
        collection = _collection(change)
        identity = _identity(change.get('id'))
        child_key = f'{key}:{collection}:{identity}'
        if child_key in seen:
            _invalid('Duplicate dispatch document identity')
        seen.add(child_key)
        deleting = change.get('delete') is True
        if headless and (collection == 'indexes' or _revision(change, 'expected_version') == 0):
            _invalid('A document commit updates existing evidence only')
        if deleting:
            if headless or collection != 'indexes' or 'value' in change:
                _invalid('Only reconstructible dispatch indexes can be deleted')
        elif 'delete' in change:
            _invalid()
        else:
            size += _document(change.get('value'), LIMITS['indexBytes'] if collection == 'indexes' else LIMITS['documentBytes'])
        if size > LIMITS['commitBytes']:
            _invalid('Dispatch commit exceeds transport bound')
        prepared.append((deleting, {'namespace': DOCUMENTS, 'key': child_key,
            'expected_version': _revision(change, 'expected_version'), 'value': change.get('value')}))

    _require_turn_witnesses(session, owner, payload.get('turn_witnesses', []))

    # All validation precedes mutations. The shared operation transaction rolls
    # back the head too when any later child CAS fails or the receipt fails.
    result = None if headless else _record_put_owned(session, {'namespace': HEADS, 'key': key,
        'expected_version': expected, 'value': {'format': SPEC['format'],
        'owner': owner, 'projectPath': project, 'state': head}})
    receipts = {}
    for deleting, row in sorted(prepared, key=lambda item: item[1]['key']):
        if deleting:
            # Generic delete permits absent rows at every expected version;
            # this semantic CAS requires the exact witnessed absence/version.
            current = _record_get_owned(session, row)
            if (current['version'] if current else 0) != row['expected_version']:
                raise StorageError('database_conflict', 'Dispatch index version conflict')
            _record_delete_owned(session, row)
        else:
            receipts[row['key']] = _record_put_owned(session, row)
    # A document commit answers with its first change's receipt, as natively.
    return receipts[prepared[0][1]['key']] if headless else result
