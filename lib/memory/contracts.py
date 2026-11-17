"""Bounded contracts shared by memory tools, APIs, storage, and search.

This dependency-free module is the single source of truth for model-created
memory payload limits.  It validates before storage discovery or mutation so a
bad request cannot spend a full-corpus scan, create a partial file, or begin a
destructive merge.  Existing durable files remain readable; the limits govern
new or explicitly replaced fields and never reclaim user data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lib.error_envelope import BadRequest


MEMORY_NAME_MAX_CHARS = 160
MEMORY_DESCRIPTION_MAX_CHARS = 512
# A trigger is a search line like the description, so it shares its limit.
MEMORY_TRIGGER_MAX_CHARS = MEMORY_DESCRIPTION_MAX_CHARS
MEMORY_BODY_MAX_CHARS = 32_768
MEMORY_FRONTMATTER_READ_MAX_CHARS = 65_536
MEMORY_TAG_MAX_ITEMS = 32
MEMORY_TAG_MAX_CHARS = 64
# Supplied IDs may belong to legacy flat files or skill-package directories,
# so retain the common filesystem component ceiling. Newly generated flat-file
# IDs use the smaller bound below, leaving ample room for collision suffixes
# and the ``.md`` extension.
MEMORY_ID_MAX_CHARS = 255
MEMORY_ID_MAX_BYTES = 255
MEMORY_GENERATED_ID_MAX_BYTES = 192
MEMORY_MERGE_MAX_ITEMS = 32
MEMORY_SEARCH_QUERY_MAX_CHARS = 2_048
MEMORY_SEARCH_BODY_MAX_CHARS = 2_000
MEMORY_SEARCH_TOP_K_MIN = 1
MEMORY_SEARCH_TOP_K_MAX = 50
MEMORY_SEARCH_TOP_K_DEFAULT = 30
MEMORY_SEARCH_MODES = ('lessons', 'owner')

# Lesson lifecycle reuses My Context's field names (``status``,
# ``status_reason``, ``superseded_by``, ``provenance.source_class``); its reason
# vocabulary is this one tuple (lib/memory/user_profile/_context.py).
# Only ``active`` lessons are eligible for automatic injection and default
# search; the others stay on disk so the owner-memory lookup can say why.
MEMORY_STATUSES = ('active', 'disproved', 'superseded')
MEMORY_STATUS_REASONS = (
    'stale', 'disproved', 'duplicate', 'conflicts_with_owner_decision',
    'owner_revoked', 'needs_owner_confirmation')
# Fixed by the write path, never by a model argument: a model memory tool call
# is an agent report; an authenticated owner edit through the API is the
# owner's own statement.
MEMORY_SOURCE_CLASSES = ('owner_stated', 'agent_report')
# ``superseded_by`` names exactly one pointer, in My Context's shapes.
MEMORY_SUPERSEDED_BY_SHAPES = (
    ('item_id',), ('conversation_id', 'turn_id', 'at'), ('evidence',))
MEMORY_POINTER_MAX_CHARS = 512
# What a role may rely on, shown beside every memory a model reads. Derived
# by ``memory_trust_class`` from the write path's source class and the
# lifecycle status, never from a model field.
MEMORY_TRUST_CLASSES = ('owner-stated', 'verified', 'inferred', 'disproved')
# The one evidence path that verifies a lesson without the owner: a Worker
# recorded a verdict citing what it ran, and a reviewer accepted that Worker's
# result (lib/memory/verification.py). Stored as ``provenance.verified_by``
# by the settlement, never by a model argument. It is not owner speech.
MEMORY_VERIFIED_BY_ACCEPTED_RESULT = 'accepted_result'
# What a Worker's evidence says about a lesson; settled on acceptance.
MEMORY_VERDICTS = ('confirmed', 'contradicted')
# ``create_memory``'s write-time decision after it showed the nearest lessons:
# this literal records a new lesson; an existing lesson id records a repeat.
MEMORY_RECORD_AS_NEW = 'new'


def memory_trust_class(status: Any, source_class: Any,
                       edited_source_class: Any = None,
                       verified_by: Any = None) -> str:
    """The trust class of one lesson or My Context item.

    ``disproved``: no longer believed (disproved or superseded; the status
    reason says which). ``owner-stated``: the owner wrote or typed it.
    ``verified``: the owner confirmed or edited it, the legacy audit bound it
    to an owner Turn, or an accepted Worker result's evidence confirmed it
    (``verified_by``). ``inferred``: everything else - agent reports,
    unrecorded legacy sources and items awaiting confirmation.
    """
    status = status or 'active'
    if status in ('disproved', 'superseded'):
        return 'disproved'
    if status != 'active':
        return 'inferred'
    if source_class == 'owner_stated':
        return 'owner-stated'
    if (source_class in ('owner_confirmed', 'legacy_import')
            or edited_source_class == 'owner_stated'
            or verified_by == MEMORY_VERIFIED_BY_ACCEPTED_RESULT):
        return 'verified'
    return 'inferred'


# My Context items written before provenance existed carry only their write
# path's ``source``; this is the source class each one stands for. The
# owner-memory lookup and the always-present profile read it from here, so
# they can never disagree about who stands behind an item.
CONTEXT_SOURCE_CLASSES = {
    'manual': 'owner_stated',
    'assistant': 'agent_report',
    'legacy_migration': 'legacy_import',
}


def context_item_source_class(item: Mapping) -> str:
    """The recorded source class of one My Context item, else its write path's."""
    provenance = item.get('provenance') or {}
    return str(provenance.get('source_class')
               or CONTEXT_SOURCE_CLASSES.get(item.get('source', ''), 'unrecorded'))


def context_item_trust_class(item: Mapping) -> str:
    """``memory_trust_class`` of one My Context item."""
    provenance = item.get('provenance') or {}
    return memory_trust_class(item.get('status') or 'active', context_item_source_class(item),
                              provenance.get('edited_source_class'),
                              provenance.get('verified_by'))


def lesson_trust_class(memory: Mapping) -> str:
    """``memory_trust_class`` of one stored lesson record."""
    provenance = memory.get('provenance') or {}
    return memory_trust_class(memory.get('status'), provenance.get('source_class'),
                              provenance.get('edited_source_class'),
                              provenance.get('verified_by'))


def normalize_memory_verdict(value: Any) -> dict[str, str]:
    """``{result, evidence}``: what a Worker's run showed about one lesson."""
    if not isinstance(value, Mapping) or set(value) != {'result', 'evidence'}:
        raise BadRequest('verdict must be {result, evidence}', field='verdict')
    if value['result'] not in MEMORY_VERDICTS:
        raise BadRequest('verdict.result must be one of ' + ', '.join(MEMORY_VERDICTS),
                         field='verdict')
    return {'result': value['result'],
            'evidence': _bounded_text(value['evidence'], field='verdict.evidence',
                                      max_chars=MEMORY_POINTER_MAX_CHARS, allow_blank=False)}


def _bounded_text(
    value: Any,
    *,
    field: str,
    max_chars: int,
    allow_blank: bool = True,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str):
        raise BadRequest(f'{field} must be a string', field=field)
    if not allow_blank and not value.strip():
        raise BadRequest(f'{field} must not be empty', field=field)
    if len(value) > max_chars:
        raise BadRequest(
            f'{field} exceeds the {max_chars:,}-character limit', field=field)
    if single_line and ('\n' in value or '\r' in value):
        raise BadRequest(f'{field} must be a single line', field=field)
    return value


def normalize_memory_tags(
    tags: Any,
    *,
    allow_none: bool = False,
    field: str = 'tags',
) -> list[str] | None:
    """Return a detached, bounded tag list; optionally preserve ``None``."""
    if tags is None and allow_none:
        return None
    if tags is None:
        return []
    if (not isinstance(tags, Sequence)
            or isinstance(tags, (str, bytes, bytearray))):
        raise BadRequest(f'{field} must be an array of strings', field=field)
    if len(tags) > MEMORY_TAG_MAX_ITEMS:
        raise BadRequest(
            f'{field} exceeds the {MEMORY_TAG_MAX_ITEMS}-item limit', field=field)

    normalized: list[str] = []
    seen: set[str] = set()
    for index, tag in enumerate(tags):
        value = _bounded_text(
            tag,
            field=f'{field}[{index}]',
            max_chars=MEMORY_TAG_MAX_CHARS,
            allow_blank=False,
            single_line=True,
        )
        # The retained frontmatter format represents lists as comma-separated
        # bracket contents. A comma inside one tag would silently round-trip as
        # two tags, so reject the ambiguous value instead of corrupting it.
        if ',' in value:
            raise BadRequest(f'{field}[{index}] must not contain a comma', field=field)
        if value in seen:
            raise BadRequest(f'{field} contains duplicate value {value!r}', field=field)
        seen.add(value)
        normalized.append(value)
    return normalized


def normalize_memory_payload(
    *,
    name: Any,
    description: Any,
    body: Any,
    tags: Any,
    scope: Any,
    allow_tags_none: bool = False,
) -> tuple[str, str, str, list[str] | None, str]:
    """Validate and detach every persisted field before a write starts."""
    normalized_name = _bounded_text(
        name, field='name', max_chars=MEMORY_NAME_MAX_CHARS,
        allow_blank=False, single_line=True)
    normalized_description = _bounded_text(
        description, field='description',
        max_chars=MEMORY_DESCRIPTION_MAX_CHARS, single_line=True)
    normalized_body = _bounded_text(
        body, field='body', max_chars=MEMORY_BODY_MAX_CHARS)
    normalized_tags = normalize_memory_tags(
        tags, allow_none=allow_tags_none)
    if scope not in ('global', 'project'):
        raise BadRequest("scope must be 'global' or 'project'", field='scope')
    return (
        normalized_name,
        normalized_description,
        normalized_body,
        normalized_tags,
        scope,
    )


def normalize_memory_trigger(trigger: Any) -> str:
    """A lesson's trigger: one bounded line naming the situation it applies in."""
    return _bounded_text(trigger, field='trigger', max_chars=MEMORY_TRIGGER_MAX_CHARS,
                         single_line=True).strip()


def normalize_memory_updates(updates: Any) -> dict[str, Any]:
    """Validate fields a caller explicitly replaces; ignore legacy extras."""
    if not isinstance(updates, Mapping):
        raise BadRequest('updates must be an object', field='updates')
    normalized = dict(updates)
    text_limits = {
        'name': (MEMORY_NAME_MAX_CHARS, False, True),
        'description': (MEMORY_DESCRIPTION_MAX_CHARS, True, True),
        'body': (MEMORY_BODY_MAX_CHARS, True, False),
    }
    for field, (limit, allow_blank, single_line) in text_limits.items():
        if field in normalized:
            normalized[field] = _bounded_text(
                normalized[field], field=field, max_chars=limit,
                allow_blank=allow_blank, single_line=single_line)
    if 'tags' in normalized:
        normalized['tags'] = normalize_memory_tags(normalized['tags'])
    if 'trigger' in normalized:
        normalized['trigger'] = normalize_memory_trigger(normalized['trigger'])
    for field in ('requires_bins', 'requires_env'):
        if field in normalized:
            normalized[field] = normalize_memory_tags(
                normalized[field], field=field)
    if 'enabled' in normalized and not isinstance(normalized['enabled'], bool):
        raise BadRequest('enabled must be a boolean', field='enabled')
    if 'sensitive' in normalized and not isinstance(normalized['sensitive'], bool):
        raise BadRequest('sensitive must be a boolean', field='sensitive')
    if any(key in normalized for key in ('status', 'status_reason', 'superseded_by')):
        normalized.update(normalize_memory_lifecycle(normalized))
    # Provenance is written by the storage write path, never by a caller.
    normalized.pop('provenance', None)
    return normalized


def normalize_superseded_by(value: Any, *, field: str = 'superseded_by') -> dict[str, str]:
    """Validate one supersession pointer in exactly one declared shape."""
    if not isinstance(value, Mapping) or not value:
        raise BadRequest(f'{field} must be an object', field=field)
    keys = tuple(sorted(value))
    shape = next((shape for shape in MEMORY_SUPERSEDED_BY_SHAPES
                  if keys == tuple(sorted(shape))), None)
    if shape is None:
        raise BadRequest(
            f'{field} must be exactly one of {{item_id}}, '
            '{conversation_id, turn_id, at} or {evidence}', field=field)
    return {key: _bounded_text(value[key], field=f'{field}.{key}',
                               max_chars=MEMORY_POINTER_MAX_CHARS,
                               allow_blank=False)
            for key in shape}


def normalize_memory_lifecycle(updates: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a requested lifecycle change; never the server-owned provenance.

    ``active`` clears the reason and pointer. Any other status needs both, so
    a demoted lesson can always answer why (design record, Decision 2).
    """
    status = updates.get('status')
    if status not in MEMORY_STATUSES:
        raise BadRequest(
            'status must be one of ' + ', '.join(MEMORY_STATUSES), field='status')
    if status == 'active':
        if updates.get('status_reason') or updates.get('superseded_by'):
            raise BadRequest('An active lesson has no status_reason or superseded_by',
                             field='status')
        return {'status': 'active', 'status_reason': None, 'superseded_by': None}
    reason = updates.get('status_reason')
    if reason not in MEMORY_STATUS_REASONS:
        raise BadRequest(
            'status_reason must be one of ' + ', '.join(MEMORY_STATUS_REASONS),
            field='status_reason')
    if updates.get('superseded_by') is None:
        raise BadRequest(
            f'A {status} lesson needs superseded_by: the item, owner Turn or '
            'evidence that replaced it', field='superseded_by')
    return {'status': status, 'status_reason': reason,
            'superseded_by': normalize_superseded_by(updates['superseded_by'])}


def validate_memory_id(memory_id: Any, *, field: str = 'memory_id') -> str:
    """Validate an opaque basename without imposing a new ID alphabet."""
    value = _bounded_text(
        memory_id, field=field, max_chars=MEMORY_ID_MAX_CHARS,
        allow_blank=False)
    if len(value.encode('utf-8')) > MEMORY_ID_MAX_BYTES:
        raise BadRequest(
            f'{field} exceeds the {MEMORY_ID_MAX_BYTES}-byte UTF-8 limit', field=field)
    if ('\x00' in value or '/' in value or '\\' in value
            or value in ('.', '..')):
        raise BadRequest(f'{field} must be a filesystem basename', field=field)
    return value


def normalize_merge_memory_ids(memory_ids: Any) -> list[str]:
    """Validate a finite, unique merge set before reading the corpus."""
    if (not isinstance(memory_ids, Sequence)
            or isinstance(memory_ids, (str, bytes, bytearray))):
        raise BadRequest('memory_ids must be an array of memory IDs', field='memory_ids')
    if len(memory_ids) < 2:
        raise BadRequest('merge_memories requires at least 2 memory IDs', field='memory_ids')
    if len(memory_ids) > MEMORY_MERGE_MAX_ITEMS:
        raise BadRequest(
            'merge_memories accepts at most '
            f'{MEMORY_MERGE_MAX_ITEMS} memory IDs', field='memory_ids')
    normalized = [
        validate_memory_id(memory_id, field=f'memory_ids[{index}]')
        for index, memory_id in enumerate(memory_ids)
    ]
    if len(set(normalized)) != len(normalized):
        raise BadRequest('memory_ids must be unique', field='memory_ids')
    return normalized


def normalize_memory_search(query: Any, top_k: Any) -> tuple[str, int]:
    """Bound query work and preserve the historical direct-call top-k clamp."""
    normalized_query = _bounded_text(
        query, field='query', max_chars=MEMORY_SEARCH_QUERY_MAX_CHARS)
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise BadRequest('top_k must be an integer', field='top_k')
    normalized_top_k = max(
        MEMORY_SEARCH_TOP_K_MIN,
        min(top_k, MEMORY_SEARCH_TOP_K_MAX),
    )
    return normalized_query, normalized_top_k


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Truncate at a UTF-8 code-point boundary, never in the middle of one."""
    encoded = value.encode('utf-8')
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode('utf-8', errors='ignore')


__all__ = [
    'CONTEXT_SOURCE_CLASSES',
    'MEMORY_BODY_MAX_CHARS',
    'MEMORY_DESCRIPTION_MAX_CHARS',
    'MEMORY_GENERATED_ID_MAX_BYTES',
    'MEMORY_FRONTMATTER_READ_MAX_CHARS',
    'MEMORY_ID_MAX_BYTES',
    'MEMORY_ID_MAX_CHARS',
    'MEMORY_MERGE_MAX_ITEMS',
    'MEMORY_NAME_MAX_CHARS',
    'MEMORY_SEARCH_QUERY_MAX_CHARS',
    'MEMORY_POINTER_MAX_CHARS',
    'MEMORY_SEARCH_BODY_MAX_CHARS',
    'MEMORY_SEARCH_MODES',
    'MEMORY_SEARCH_TOP_K_DEFAULT',
    'MEMORY_SEARCH_TOP_K_MAX',
    'MEMORY_SEARCH_TOP_K_MIN',
    'MEMORY_SOURCE_CLASSES',
    'MEMORY_STATUSES',
    'MEMORY_STATUS_REASONS',
    'MEMORY_SUPERSEDED_BY_SHAPES',
    'MEMORY_TAG_MAX_CHARS',
    'MEMORY_TAG_MAX_ITEMS',
    'MEMORY_TRIGGER_MAX_CHARS',
    'MEMORY_TRUST_CLASSES',
    'context_item_source_class',
    'context_item_trust_class',
    'MEMORY_RECORD_AS_NEW',
    'MEMORY_VERDICTS',
    'MEMORY_VERIFIED_BY_ACCEPTED_RESULT',
    'lesson_trust_class',
    'memory_trust_class',
    'normalize_memory_verdict',
    'normalize_memory_lifecycle',
    'normalize_memory_payload',
    'normalize_memory_search',
    'normalize_memory_tags',
    'normalize_memory_trigger',
    'normalize_memory_updates',
    'normalize_merge_memory_ids',
    'normalize_superseded_by',
    'truncate_utf8',
    'validate_memory_id',
]
