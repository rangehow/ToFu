"""Structured, per-user context store and bounded undo history.

The legacy personal profile was a Markdown file whose headings accidentally
encoded runtime semantics.  This module makes those semantics explicit while
keeping the Markdown API as a compatibility projection.

Every item has a lifecycle status (``CONTEXT_STATUSES``). Only active items
live in ``items`` and are injected; superseded and unconfirmed items live in
the same document's ``inactive_items`` list. ``load_context`` also lets the
personal owner's scope inherit the legacy open-mode document once (see
``_inherit_legacy_open_scope``).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import NamedTuple

from lib.identity import PERSONAL_USER_ID
from lib.ids import short_id
from lib.json_store import read_json, write_json_atomic, write_text_atomic, locked_path
from lib.log import audit_log, get_logger
from lib.memory.contracts import MEMORY_STATUS_REASONS

from lib.memory.user_profile._paths import (
    context_changes_path,
    context_path,
    profile_path,
)

logger = get_logger(__name__)

_CONTEXT_TRANSACTIONS = threading.local()


@contextmanager
def context_transaction(scope: str = ''):
    """One scope's RMW/mirror/history transaction; nested helpers share it.

    The shared JSON store owns weak thread locks and supported process locks.
    No model dispatch may run while this short storage transaction is held.
    """
    path = os.path.abspath(context_path(scope))
    held = getattr(_CONTEXT_TRANSACTIONS, 'held', None)
    if held is None:
        held = _CONTEXT_TRANSACTIONS.held = set()
    if path in held:
        yield
        return
    with locked_path(path):
        held.add(path)
        try:
            yield
        finally:
            held.remove(path)


CONTEXT_VERSION = 1
# ``delegation_grant``: authority the owner grants in his own words. It renders
# only inside the profile's bounded section (``_render.DELEGATION_BOUND``);
# shown as an ordinary directive, a grant suppresses necessary questions
# (owner-profile experiment 1, K5).
CONTEXT_TYPES = ('identity', 'work_rule', 'response_preference', 'delegation_grant')
CONTEXT_SOURCES = ('manual', 'assistant', 'legacy_migration')
CONTEXT_STATUSES = ('active', 'superseded', 'needs_confirmation')
# One reason vocabulary for My Context and lessons (lib/memory/contracts.py).
CONTEXT_STATUS_REASONS = MEMORY_STATUS_REASONS
_LIFECYCLE_KEYS = ('status', 'status_reason', 'superseded_by', 'provenance')
CONTEXT_CHANGE_LIMIT = 50


class ContextValidationError(ValueError):
    """A context item or document failed validation."""


class ContextConflictError(RuntimeError):
    """An undo could not be applied without overwriting a newer edit."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _clean_text(value) -> str:
    return str(value or '').strip()


def _scalar_map(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: val for key, val in value.items()
            if isinstance(key, str)
            and (val is None or isinstance(val, (str, int, float, bool)))}


def _lifecycle_fields(raw: dict) -> dict:
    """Stored lifecycle metadata; an unknown status fails closed."""
    fields: dict = {}
    status = _clean_text(raw.get('status'))
    if status:
        fields['status'] = (status if status in CONTEXT_STATUSES
                            else 'needs_confirmation')
    reason = _clean_text(raw.get('status_reason'))
    if reason in CONTEXT_STATUS_REASONS:
        fields['status_reason'] = reason
    for key in ('superseded_by', 'provenance'):
        value = _scalar_map(raw.get(key))
        if value:
            fields[key] = value
    return fields


def is_active_context_item(item: dict) -> bool:
    """Only active items are injected, mirrored or counted."""
    return (item or {}).get('status', 'active') == 'active'


def _with_server_lifecycle(item: dict, stored: dict | None) -> dict:
    """Lifecycle metadata is server-owned: a write can neither set nor erase it.

    A written item is active and keeps the provenance of the stored item with
    the same id, because the Settings full replace strips unknown fields.
    """
    if stored is not None:
        if 'provenance' in stored:
            item['provenance'] = deepcopy(stored['provenance'])
        if 'status' in stored:
            item['status'] = 'active'
    return item


def _normalize_item(raw: dict, *, source_default: str = 'manual',
                    now: str | None = None,
                    keep_lifecycle: bool = False) -> dict | None:
    if not isinstance(raw, dict):
        raise ContextValidationError('Each context item must be an object')
    item_type = _clean_text(raw.get('type'))
    if item_type not in CONTEXT_TYPES:
        raise ContextValidationError(f'Unsupported context type: {item_type or "(empty)"}')
    source = _clean_text(raw.get('source')) or source_default
    if source not in CONTEXT_SOURCES:
        source = source_default if source_default in CONTEXT_SOURCES else 'manual'
    stamp = now or _now()
    item = {
        'id': _clean_text(raw.get('id')) or short_id('ctx_', 16),
        'type': item_type,
        'source': source,
        'created_at': _clean_text(raw.get('created_at')) or stamp,
        'updated_at': _clean_text(raw.get('updated_at')) or stamp,
    }
    if item_type == 'work_rule':
        condition = _clean_text(raw.get('condition'))
        action = _clean_text(raw.get('action'))
        if not condition or not action:
            raise ContextValidationError(
                'A work rule requires both condition and action')
        item.update({'condition': condition, 'action': action})
    else:
        text = _clean_text(raw.get('text'))
        if not text:
            return None
        item['text'] = text
    if keep_lifecycle:
        item.update(_lifecycle_fields(raw))
    return item


def context_markdown(items: list[dict]) -> str:
    """Stable, human-readable content of the active items (their ``chars``)."""
    groups = {
        'work_rule': [],
        'response_preference': [],
        'identity': [],
        'delegation_grant': [],
    }
    for item in items or []:
        if not is_active_context_item(item):
            continue
        item_type = item.get('type')
        if item_type == 'work_rule':
            groups[item_type].append(
                f'- WHEN: {item.get("condition", "").strip()}\n'
                f'  DO: {item.get("action", "").strip()}')
        elif item_type in groups and item.get('text'):
            groups[item_type].append(f'- {item["text"].strip()}')
    sections = []
    labels = (
        ('work_rule', 'Work rules'),
        ('response_preference', 'Response preferences'),
        ('identity', 'About the user'),
        ('delegation_grant', 'Delegation grants'),
    )
    for key, label in labels:
        if groups[key]:
            sections.append(f'## {label}\n' + '\n'.join(groups[key]))
    return '\n\n'.join(sections).strip()


def context_char_count(items: list[dict]) -> int:
    return len(context_markdown(items))


def _legacy_markdown(items: list[dict]) -> str:
    """Project structured items onto the old ``## Header`` wire shape.

    Grants are left out: the legacy shape parses every unknown header back as
    identity, which would turn a grant into an unbounded "About the user" fact.
    """
    items = [item for item in items if is_active_context_item(item)]
    sections: list[str] = []
    prefs = [i for i in items if i.get('type') == 'response_preference']
    identities = [i for i in items if i.get('type') == 'identity']
    rules = [i for i in items if i.get('type') == 'work_rule']
    if prefs:
        sections.append('## Preferences\n' + '\n'.join(
            f'- {i["text"]}' for i in prefs))
    if identities:
        sections.append('## About the user\n' + '\n'.join(
            f'- {i["text"]}' for i in identities))
    if rules:
        sections.append('## Work rules\n' + '\n'.join(
            f'- When {i["condition"]} → {i["action"]}' for i in rules))
    return '\n\n'.join(sections).strip()


def _legacy_items(body: str) -> list[dict]:
    """Deterministically migrate the old two-section Markdown profile."""
    rows: list[dict] = []
    header = ''
    stamp = _now()
    for raw in (body or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            header = line.lstrip('#').strip()
            continue
        if line.startswith(('- ', '* ')):
            line = line[2:].strip()
        if not line:
            continue
        normalized = header.casefold()
        item_type = ('response_preference'
                     if not header or normalized == 'preferences'
                     else 'identity')
        rows.append({
            'id': short_id('ctx_', 16),
            'type': item_type,
            'text': line,
            'source': 'legacy_migration',
            'created_at': stamp,
            'updated_at': stamp,
        })
    return rows


def _valid_doc(data) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        return None
    held = data.get('inactive_items', [])
    if not isinstance(held, list):
        return None
    items: list[dict] = []
    inactive: list[dict] = []
    seen: set[str] = set()
    try:
        for raw, from_held in ([(raw, False) for raw in data['items']]
                               + [(raw, True) for raw in held]):
            item = _normalize_item(raw, source_default='manual',
                                   keep_lifecycle=True)
            if item is None:
                continue
            if item['id'] in seen:
                item['id'] = short_id('ctx_', 16)
            seen.add(item['id'])
            if from_held and is_active_context_item(item):
                item['status'] = 'needs_confirmation'
            (items if is_active_context_item(item) else inactive).append(item)
    except ContextValidationError as exc:
        logger.warning('[UserContext] invalid context document: %s', exc)
        return None
    doc = {'version': CONTEXT_VERSION, 'items': items, 'inactive_items': inactive}
    if isinstance(data.get('inherited'), dict):
        doc['inherited'] = deepcopy(data['inherited'])
    return doc


def _entries_fingerprint(doc: dict) -> str:
    payload = json.dumps([doc.get('items', []), doc.get('inactive_items', [])],
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class LegacyVerdict(NamedTuple):
    """One audited legacy entry, bound to the SHA-256 of its exact text."""
    text_sha256: str
    status: str
    status_reason: str = ''
    superseded_by: dict | None = None


_LEGACY_AUDIT_MODULE = 'lib.memory.user_profile._legacy_audit'


def _legacy_open_scope_audit() -> tuple[str, dict]:
    """This install's audit of its legacy entries: ``(audit id, verdicts)``.

    The audit module holds one install's personal data and is excluded from
    Internal and Open exports. Without it nothing is audited, so every
    inherited entry is ``needs_confirmation``: absence fails closed.
    """
    try:
        audit = importlib.import_module(_LEGACY_AUDIT_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != _LEGACY_AUDIT_MODULE:
            raise
        return '', {}
    return audit.LEGACY_OPEN_SCOPE_AUDIT_ID, audit.LEGACY_OPEN_SCOPE_VERDICTS


def _legacy_verdict(verdicts: dict, item_id: str, text: str) -> LegacyVerdict | None:
    verdict = verdicts.get(item_id)
    if verdict is None:
        return None
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != verdict.text_sha256:
        return None
    return verdict


def _inherit_legacy_open_scope(scope: str, path: str) -> dict | None:
    """Give the personal owner the legacy open-mode document, exactly once.

    Open and private mode used scope ``''`` until ``local_admin_context``
    began to carry ``PERSONAL_USER_ID``; from then on every task read
    ``str(PERSONAL_USER_ID)`` and the populated ``''`` document went unread.
    The owner's scope receives a copy whose entries carry provenance and the
    audited status (``_legacy_open_scope_audit``); the source is only read.
    The written ``inherited`` record is the idempotency and undo marker.
    """
    # TODO(enterprise): scope '' had exactly one operator, the personal owner;
    # a multi-owner install must map legacy open-mode data explicitly.
    if scope != str(PERSONAL_USER_ID) or os.path.exists(path):
        return None
    source_path = context_path('')
    try:
        with open(source_path, 'rb') as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning('[UserContext] legacy open-mode context unreadable: %s', exc)
        return None
    try:
        source = _valid_doc(json.loads(raw))
    except ValueError as exc:
        logger.warning('[UserContext] legacy open-mode context is not JSON: %s', exc)
        return None
    entries = (source['items'] + source['inactive_items']) if source else []
    if not entries:
        return None
    audit_id, verdicts = _legacy_open_scope_audit()
    items: list[dict] = []
    inactive: list[dict] = []
    for entry in entries:
        item = {key: value for key, value in entry.items()
                if key not in _LIFECYCLE_KEYS}
        verdict = _legacy_verdict(verdicts, item['id'], item.get('text') or '')
        if verdict is None:
            item.update(status='needs_confirmation',
                        status_reason='needs_owner_confirmation')
        else:
            item['status'] = verdict.status
            if verdict.status_reason:
                item['status_reason'] = verdict.status_reason
            if verdict.superseded_by:
                item['superseded_by'] = dict(verdict.superseded_by)
        item['provenance'] = {
            'source_class': 'legacy_import',
            'origin_scope': '',
            'origin_file': os.path.basename(source_path),
            'origin_source': entry.get('source', ''),
            'origin_created_at': entry.get('created_at', ''),
            'audit': audit_id if verdict else '',
        }
        (items if item['status'] == 'active' else inactive).append(item)
    doc = {'version': CONTEXT_VERSION, 'items': items, 'inactive_items': inactive}
    doc['inherited'] = {
        'from_scope': '',
        'source_file': os.path.basename(source_path),
        'source_sha256': hashlib.sha256(raw).hexdigest(),
        'audit': audit_id,
        'migrated_at': _now(),
        'fingerprint': _entries_fingerprint(doc),
        'undone_at': '',
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, doc)
    _write_legacy_mirror(items, scope)
    audit_log('user_context_inherited', active=len(items), inactive=len(inactive))
    return doc


def undo_legacy_context_inheritance(scope: str) -> dict:
    """Return the owner's scope to its pre-inheritance state: no context.

    The ``inherited`` record stays, marked undone, so the next read does not
    inherit again; the source document and the changes log are untouched.
    Refuses when the context changed after the inheritance, like
    ``undo_context_change``.
    """
    with context_transaction(scope):
        path = context_path(scope)
        doc = _valid_doc(read_json(path, default=None))
        record = (doc or {}).get('inherited')
        if not record:
            return {'undone': False, 'not_found': True}
        if record.get('undone_at'):
            return {'undone': True, 'already_undone': True}
        if _entries_fingerprint(doc) != record.get('fingerprint'):
            raise ContextConflictError(
                'My Context changed after the legacy entries were inherited; '
                'edit it directly instead of undoing the inheritance')
        write_json_atomic(path, {
            'version': CONTEXT_VERSION, 'items': [], 'inactive_items': [],
            'inherited': {**record, 'undone_at': _now()}})
        _write_legacy_mirror([], scope)
        audit_log('user_context_inheritance_undone')
        return {'undone': True}


def _write_legacy_mirror(items: list[dict], scope: str) -> None:
    path = profile_path(scope)
    body = _legacy_markdown(items)
    if not body:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.warning('[UserContext] legacy mirror clear failed: %s', exc)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_text_atomic(path, body + '\n')


def load_context(scope: str = '') -> dict:
    """Load structured context, migrating a legacy document once."""
    with context_transaction(scope):
        path = context_path(scope)
        data = _valid_doc(read_json(path, default=None))
        if data is not None:
            return data

        legacy = ''
        try:
            with open(profile_path(scope), encoding='utf-8') as handle:
                legacy = handle.read().strip()
        except FileNotFoundError as exc:
            logger.debug('[UserContext] no legacy profile to migrate: %s', exc)
        except OSError as exc:
            logger.warning('[UserContext] legacy profile read failed: %s', exc)

        data = {'version': CONTEXT_VERSION, 'items': _legacy_items(legacy),
                'inactive_items': []}
        if legacy:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_json_atomic(path, data)
            audit_log('user_context_migrated', items=len(data['items']))
            return data
        return _inherit_legacy_open_scope(scope, path) or data


def context_status(scope: str = '') -> dict:
    doc = load_context(scope)
    return {
        'version': CONTEXT_VERSION,
        'items': deepcopy(doc['items']),
        'inactive_items': deepcopy(doc['inactive_items']),
        'chars': context_char_count(doc['items']),
    }


def save_context_items(items: list[dict], scope: str = '', *,
                       source_default: str = 'manual') -> dict:
    """Replace all active context items after validation.

    There is no size cap: the always-present profile is exactly the owner's
    active statements, rendered whole (docs/modules/context_engineering.md,
    "Owner profile (Phase 1)"); the provider's context window, through
    ``ContextPlanV2``, is its only limit.

    Every item that disappears in the replacement leaves a ``delete`` record
    in the bounded changes log, so the consolidation learner can tell
    "user explicitly removed this" apart from "never seen it" and does not
    resurrect deleted content.

    Inactive items are not shown to the Settings editor, so a replacement
    keeps them unless it names one by id, which activates it.
    """
    with context_transaction(scope):
        if not isinstance(items, list):
            raise ContextValidationError('items must be a list')
        current = load_context(scope)
        previous = current['items']
        stored = {item['id']: item
                  for item in previous + current['inactive_items']}
        stamp = _now()
        normalized: list[dict] = []
        seen: set[str] = set()
        written: dict[str, str] = {}
        for raw in items:
            item = _normalize_item(raw, source_default=source_default, now=stamp)
            if item is None:
                continue
            if item['id'] in seen:
                raise ContextValidationError(f'Duplicate context id: {item["id"]}')
            seen.add(item['id'])
            if _item_text(item) != _item_text(stored.get(item['id'])):
                written[item['id']] = _item_text(item)
            normalized.append(_with_server_lifecycle(item, stored.get(item['id'])))
        from lib.memory.secret_scan import refusal_message, secret_findings
        findings = secret_findings(written)
        if findings:
            raise ContextValidationError(refusal_message(findings))
        chars = context_char_count(normalized)
        inactive = [item for item in current['inactive_items']
                    if item['id'] not in seen]
        doc = {'version': CONTEXT_VERSION, 'items': normalized,
               'inactive_items': inactive}
        if 'inherited' in current:
            doc['inherited'] = current['inherited']
        path = context_path(scope)
        if normalized or inactive or 'inherited' in doc:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_json_atomic(path, doc)
        else:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as exc:
                logger.warning('[UserContext] clear failed: %s', exc)
                raise
        _write_legacy_mirror(normalized, scope)
        for old in previous:
            if old.get('id') and old['id'] not in seen:
                _append_change(scope, 'delete', old, None)
        audit_log('user_context_saved', items=len(normalized), chars=chars)
        return {'saved': True, 'items': deepcopy(normalized), 'chars': chars}


def _item_text(item: dict | None) -> str:
    """The owner-visible text of one item, as the write-time scan reads it."""
    if not item:
        return ''
    if item.get('type') == 'work_rule':
        return f'{item.get("condition", "")}\n{item.get("action", "")}'
    return item.get('text') or ''


def _append_change(scope: str, operation: str, before: dict | None,
                   after: dict | None) -> str:
    path = context_changes_path(scope)
    data = read_json(path, default={})
    changes = list(data.get('changes') or []) if isinstance(data, dict) else []
    change_id = short_id('chg_', 16)
    changes.append({
        'id': change_id,
        'operation': operation,
        'item_id': (after or before or {}).get('id', ''),
        'before': deepcopy(before),
        'after': deepcopy(after),
        'created_at': _now(),
        'undone_at': '',
    })
    changes = changes[-CONTEXT_CHANGE_LIMIT:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, {'version': 1, 'changes': changes})
    return change_id


def create_context_item(raw: dict, scope: str = '', *, source: str = 'manual',
                        record_change: bool = False) -> dict:
    with context_transaction(scope):
        doc = load_context(scope)
        item = _normalize_item({**(raw or {}), 'source': source},
                               source_default=source)
        if item is None:
            raise ContextValidationError('Context text is required')
        result = save_context_items(doc['items'] + [item], scope)
        change_id = (_append_change(scope, 'create', None, item)
                     if record_change else '')
        return {'item': deepcopy(item), 'change_id': change_id, **result}


def update_context_item(item_id: str, updates: dict, scope: str = '', *,
                        source: str | None = None,
                        record_change: bool = False) -> dict | None:
    with context_transaction(scope):
        doc = load_context(scope)
        idx = next((i for i, item in enumerate(doc['items'])
                    if item.get('id') == item_id), None)
        if idx is None:
            return None
        before = deepcopy(doc['items'][idx])
        merged = {**before, **(updates or {}), 'id': before['id'],
                  'created_at': before['created_at'], 'updated_at': _now()}
        if source:
            merged['source'] = source
        after = _normalize_item(merged, source_default=source or before['source'])
        if after is None:
            raise ContextValidationError('Context text is required')
        after = _with_server_lifecycle(after, before)
        next_items = list(doc['items'])
        next_items[idx] = after
        result = save_context_items(next_items, scope)
        change_id = (_append_change(scope, 'update', before, after)
                     if record_change else '')
        return {'item': deepcopy(after), 'change_id': change_id, **result}


def delete_context_item(item_id: str, scope: str = '') -> bool:
    """Delete an active item, or one still awaiting the owner's confirmation.

    Either deletion leaves a ``delete`` record, the learner's tombstone.
    Superseded items are refused: they record why a belief was dropped.
    """
    with context_transaction(scope):
        doc = load_context(scope)
        remaining = [item for item in doc['items'] if item.get('id') != item_id]
        if len(remaining) < len(doc['items']):
            save_context_items(remaining, scope)
            return True
        pending = next((item for item in doc['inactive_items']
                        if item.get('id') == item_id
                        and item.get('status') == 'needs_confirmation'), None)
        if pending is None:
            return False
        doc['inactive_items'] = [item for item in doc['inactive_items']
                                 if item is not pending]
        write_json_atomic(context_path(scope), doc)
        _append_change(scope, 'delete', pending, None)
        audit_log('user_context_pending_deleted')
        return True


def undo_context_change(change_id: str, scope: str = '') -> dict:
    with context_transaction(scope):
        path = context_changes_path(scope)
        data = read_json(path, default={})
        changes = list(data.get('changes') or []) if isinstance(data, dict) else []
        change = next((c for c in changes if c.get('id') == change_id), None)
        if change is None:
            return {'undone': False, 'not_found': True}
        if change.get('undone_at'):
            return {'undone': True, 'already_undone': True}

        doc = load_context(scope)
        current = next((i for i in doc['items']
                        if i.get('id') == change.get('item_id')), None)
        expected = change.get('after')
        if current != expected:
            raise ContextConflictError(
                'This context item changed after it was learned; refresh before undoing')

        if change.get('operation') == 'create':
            next_items = [i for i in doc['items']
                          if i.get('id') != change.get('item_id')]
            restored = None
        elif change.get('operation') == 'update' and change.get('before'):
            restored = deepcopy(change['before'])
            next_items = [restored if i.get('id') == change.get('item_id') else i
                          for i in doc['items']]
        elif change.get('operation') == 'delete' and change.get('before'):
            restored = deepcopy(change['before'])
            next_items = list(doc['items']) + [restored]
        else:
            raise ContextConflictError('This context change cannot be undone')
        result = save_context_items(next_items, scope)
        change['undone_at'] = _now()
        write_json_atomic(path, {'version': 1, 'changes': changes})
        audit_log('user_context_change_undone', change_id=change_id)
        return {'undone': True, 'item': restored, **result}


def legacy_profile_body(scope: str = '') -> str:
    return _legacy_markdown(load_context(scope)['items'])
def deleted_context_items(scope: str = '') -> list[dict]:
    """Snapshots of items the user explicitly removed (not since undone).

    The consolidation learner treats these as tombstones: content matching a
    tombstone must not be re-learned, because the user already rejected it.
    """
    with context_transaction(scope):
        data = read_json(context_changes_path(scope), default={})
        changes = list(data.get('changes') or []) if isinstance(data, dict) else []
        tombstoned: list[dict] = []
        for change in changes:
            if change.get('operation') != 'delete' or change.get('undone_at'):
                continue
            before = change.get('before')
            if isinstance(before, dict):
                tombstoned.append(deepcopy(before))
        return tombstoned


def legacy_items_to_context(items: list[dict]) -> list[dict]:
    """Translate the old ``[{header,text}]`` API into structured items."""
    stamp = _now()
    out: list[dict] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        text = _clean_text(raw.get('text'))
        if not text:
            continue
        header = _clean_text(raw.get('header'))
        item_type = ('response_preference'
                     if not header or header.casefold() == 'preferences'
                     else 'identity')
        out.append({
            'id': _clean_text(raw.get('id')) or short_id('ctx_', 16),
            'type': item_type,
            'text': text,
            'source': _clean_text(raw.get('source')) or 'manual',
            'created_at': _clean_text(raw.get('created_at')) or stamp,
            'updated_at': stamp,
        })
    return out


__all__ = [
    'CONTEXT_VERSION', 'CONTEXT_TYPES',
    'CONTEXT_STATUSES', 'CONTEXT_STATUS_REASONS', 'is_active_context_item',
    'undo_legacy_context_inheritance',
    'ContextValidationError', 'ContextConflictError', 'load_context',
    'context_status', 'context_markdown', 'context_char_count',
    'save_context_items', 'create_context_item', 'update_context_item',
    'delete_context_item', 'undo_context_change', 'deleted_context_items',
    'legacy_profile_body', 'legacy_items_to_context',
]
