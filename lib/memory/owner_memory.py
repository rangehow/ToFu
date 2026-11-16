"""Read-only owner-memory lookup: what Tofu knows about the owner, and why.

Responsibility: one query over the two stores that hold owner-relevant memory,
returning every lifecycle state with its provenance and trust class (design
record "Owner understanding", Decisions 2 and 4):

* My Context (``lib.memory.user_profile``): active items plus the document's
  ``inactive_items`` (needs confirmation, superseded);
* lessons (``lib.memory.storage``): active, disproved and superseded.

Every record carries ``trust`` (``lib.memory.contracts.memory_trust_class``):
owner-stated, verified, inferred or disproved, derived from the write path's
source class and the status, never from a model field.

``explain_memory_item`` answers "why do you think that?" for one item: its
record, the supersession chain in both directions, and the Turns that used it,
read from Turn provenance (``preferencesApplied.ids``, ``memoryPrefetch``).

Consumers: the ``search_memories`` tool (``mode='owner'`` and ``memory_id``,
lib/tasks_pkg/handlers/memory.py) for every role, and the owner's provenance
API (``/api/v1/owner-memory``, routes/api_v1/memory.py). Nothing here writes
or ranks for injection.

Audience rules, chosen by the caller: a role (``sensitive=False``) never sees
a sensitive lesson, only their count; the owner's API (``sensitive=True``)
sees them, flagged. ``identity=False`` (delegated Workers and sub-agents)
withholds My Context ``identity`` items, which the design keeps from those
roles. The ``format_*`` renderings are model-facing and redact credential
shapes stored before the write-time refusal existed
(``lib.memory.secret_scan``); the records themselves stay unredacted for the
owner's API.

Entry points: ``search_owner_memory``, ``explain_memory_item``,
``turns_using``, ``format_owner_search``, ``format_explanation``.
Dependencies: ``lib.memory.user_profile``, ``lib.memory.storage``,
``lib.memory.relevance``; Turn reads are supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from lib.memory.contracts import (
    MEMORY_SEARCH_BODY_MAX_CHARS,
    context_item_source_class,
    memory_trust_class,
)
from lib.memory.secret_scan import redact_secret_shapes

TRUST_LEGEND = (
    'Trust: owner-stated = the owner said it; verified = the owner confirmed or edited it, or '
    'an audit tied it to an owner Turn; inferred = an agent report or unrecorded source, a '
    'hypothesis to check before relying on it; disproved = no longer believed, kept to say why.')


def _trust(status: str, source_class: str, provenance: Mapping) -> str:
    return memory_trust_class(status, source_class, provenance.get('edited_source_class'),
                              provenance.get('verified_by'))


def _context_record(item: Mapping, *, score: float = 0.0) -> dict:
    provenance = dict(item.get('provenance') or {})
    status = item.get('status') or 'active'
    source_class = context_item_source_class(item)
    record = {
        'kind': 'context',
        'id': item.get('id', ''),
        'type': item.get('type', ''),
        'status': status,
        'status_reason': item.get('status_reason', ''),
        'superseded_by': dict(item.get('superseded_by') or {}),
        'source_class': source_class,
        'trust': _trust(status, source_class, provenance),
        'provenance': provenance,
        'created_at': item.get('created_at', ''),
        'updated_at': item.get('updated_at', ''),
        'score': round(float(score), 4),
    }
    if item.get('type') == 'work_rule':
        record.update(condition=item.get('condition', ''), action=item.get('action', ''))
    else:
        record['text'] = item.get('text', '')
    return record


def _lesson_record(memory: Mapping, *, score: float = 0.0, body: bool = False) -> dict:
    provenance = dict(memory.get('provenance') or {})
    status = memory.get('status') or 'active'
    source_class = provenance.get('source_class') or 'unrecorded'
    record = {
        'kind': 'lesson',
        'id': memory.get('id', ''),
        'name': memory.get('name', ''),
        'description': memory.get('description', ''),
        'scope': memory.get('scope', ''),
        'tags': list(memory.get('tags') or []),
        'status': status,
        'status_reason': memory.get('status_reason', ''),
        'superseded_by': dict(memory.get('superseded_by') or {}),
        'source_class': source_class,
        'trust': _trust(status, source_class, provenance),
        'sensitive': bool(memory.get('sensitive')),
        'provenance': provenance,
        'supersedes': str(provenance.get('supersedes') or ''),
        'recurrences': [{'turn': key, 'at': value}
                        for key, value in sorted((memory.get('recurrences') or {}).items(),
                                                 key=lambda row: (row[1], row[0]))],
        'created': memory.get('created', ''),
        'updated': memory.get('updated', ''),
        'score': round(float(score), 4),
    }
    if body:
        record['body'] = memory.get('body', '')
    return record


def _context_text(item: Mapping) -> str:
    if item.get('type') == 'work_rule':
        return f'{item.get("condition", "")} {item.get("action", "")}'
    return str(item.get('text') or '')


def _event_text(item: Mapping) -> str:
    """The text ``preferencesApplied.items`` recorded before ids existed."""
    if item.get('type') == 'work_rule':
        return f'When {item.get("condition", "")} → {item.get("action", "")}'
    return str(item.get('text') or '')


def _context_document(scope: str | None) -> dict | None:
    if scope is None:
        return None
    from lib.memory.user_profile import load_context
    return load_context(scope)


def _scores(query: str, texts: list[str]) -> list[float]:
    from lib.memory.relevance._score import _score_token_documents
    from lib.memory.relevance._tokenize import _tokenize
    scores = [0.0] * len(texts)
    if query.strip():
        for index, score in _score_token_documents(query, (_tokenize(text) for text in texts)):
            scores[index] = float(score)
    return scores


def search_owner_memory(query: str, *, scope: str | None, project_path=None,
                        extra_paths=None, top_k: int, identity: bool = True,
                        sensitive: bool = False, context_reason: str = '') -> dict:
    """Every My Context item and the matching lessons, each with provenance.

    My Context is the owner's own, content-sized store, so every item is
    returned (query matches first): a lookup never hides an owner directive.
    Lessons are ranked like default search but include inactive lessons.
    ``scope=None`` means this caller may not read My Context; ``context_reason``
    says why. ``sensitive=True`` is the owner's own audience: sensitive lessons
    are ranked on their name and description (their body is never loaded here).
    """
    context: dict[str, Any] = {'available': False, 'reason': context_reason or 'profile_scope_missing',
                               'items': [], 'inactive_items': [], 'withheld_identity': 0}
    document = _context_document(scope)
    if document is not None:
        entries = list(document['items']) + list(document['inactive_items'])
        if not identity:
            context['withheld_identity'] = sum(1 for item in entries if item.get('type') == 'identity')
            entries = [item for item in entries if item.get('type') != 'identity']
        scores = _scores(query, [_context_text(item) for item in entries])
        order = sorted(range(len(entries)), key=lambda index: (-scores[index], index))
        for index in order:
            record = _context_record(entries[index], score=scores[index])
            context['items' if record['status'] == 'active' else 'inactive_items'].append(record)
        context.update(available=True, reason='')

    from lib.memory.relevance._search import _score_corpus
    from lib.memory.storage import iter_lesson_records
    withheld = []

    def visible_lessons():
        for memory in iter_lesson_records(project_path, extra_paths=extra_paths,
                                          body_char_limit=MEMORY_SEARCH_BODY_MAX_CHARS):
            if memory.get('sensitive') and not sensitive:
                withheld.append(memory.get('id'))
                continue
            yield memory

    ranked = [(score, memory) for score, memory in _score_corpus(
        query, visible_lessons(), include_body=True, retain_body=False) if score > 0]
    lessons: dict[str, Any] = {'items': [], 'inactive_items': [], 'total_matches': len(ranked),
                               'sensitive_withheld': len(withheld)}
    for score, memory in ranked[:top_k]:
        record = _lesson_record(memory, score=score)
        lessons['items' if record['status'] == 'active' else 'inactive_items'].append(record)
    return {'query': query, 'context': context, 'lessons': lessons}


def _find_context_item(document: dict | None, item_id: str) -> dict | None:
    if document is None:
        return None
    return next((item for item in list(document['items']) + list(document['inactive_items'])
                 if item.get('id') == item_id), None)


def _find_lesson(item_id: str, project_path, extra_paths) -> dict | None:
    from lib.memory.contracts import validate_memory_id
    from lib.memory.storage import get_memory
    try:
        validate_memory_id(item_id)
    except ValueError:
        return None
    memory = get_memory(item_id, project_path=project_path, extra_paths=extra_paths)
    if not memory or memory.get('is_package'):
        return None
    return memory


def _summary(record: Mapping) -> str:
    if record['kind'] == 'lesson':
        return record['name']
    if 'condition' in record:
        return f'When {record["condition"]} → {record["action"]}'
    return record.get('text', '')


def explain_memory_item(item_id: str, *, scope: str | None, project_path=None,
                        extra_paths=None, identity: bool = True, sensitive: bool = False,
                        turns: Iterable[Mapping] | None = None,
                        usage_limit: int) -> dict | None:
    """One item's record, supersession chain and the Turns that used it.

    ``turns`` is a newest-first Turn iterable of one conversation (the caller's
    owner-scoped read); ``None`` skips usage. Returns ``None`` when no visible
    item has this id. For a role (``sensitive=False``) a sensitive lesson
    answers only that it is withheld.
    """
    document = _context_document(scope)
    resolved: dict[str, dict] = {}

    def resolve(target_id: str) -> dict | None:
        if target_id in resolved:
            return resolved[target_id]
        item = _find_context_item(document, target_id)
        if item is not None:
            record = (None if not identity and item.get('type') == 'identity'
                      else _context_record(item))
        else:
            memory = _find_lesson(target_id, project_path, extra_paths)
            if memory is None:
                record = None
            elif memory.get('sensitive') and not sensitive:
                record = {'kind': 'lesson', 'id': target_id, 'withheld': True}
            else:
                record = _lesson_record(memory, body=True)
        resolved[target_id] = record
        return record

    record = resolve(item_id)
    if record is None:
        return None
    if record.get('withheld'):
        return {'item': record, 'chain': [], 'supersedes': [], 'used_in': None}

    chain, seen = [], {item_id}
    current = record
    while current and current.get('superseded_by'):
        pointer = current['superseded_by']
        successor = None
        if pointer.get('item_id') and pointer['item_id'] not in seen:
            seen.add(pointer['item_id'])
            successor = resolve(pointer['item_id'])
        visible = successor is not None and not successor.get('withheld')
        chain.append({'from': current['id'], 'status': current['status'],
                      'status_reason': current.get('status_reason', ''),
                      'pointer': dict(pointer),
                      'successor': ({key: successor[key] for key in ('kind', 'id', 'status', 'trust')}
                                    | {'summary': _summary(successor)}) if visible else None})
        current = successor if visible else None

    supersedes = [
        {'kind': 'context', 'id': item.get('id', ''), 'status': item.get('status', ''),
         'status_reason': item.get('status_reason', ''), 'summary': _summary(_context_record(item))}
        for item in (document or {}).get('inactive_items', [])
        if (item.get('superseded_by') or {}).get('item_id') == item_id
        and (identity or item.get('type') != 'identity')]
    # A lesson version names its predecessor (``provenance.supersedes``), so
    # the earlier versions are walked back without a corpus scan.
    predecessor, seen = record.get('supersedes') if record['kind'] == 'lesson' else '', {item_id}
    while predecessor and predecessor not in seen:
        seen.add(predecessor)
        earlier = resolve(predecessor)
        if earlier is None or earlier.get('withheld'):
            break
        supersedes.append({'kind': 'lesson', 'id': earlier['id'], 'status': earlier['status'],
                           'status_reason': earlier.get('status_reason', ''),
                           'summary': _summary(earlier)})
        predecessor = earlier.get('supersedes')

    used_in = None
    if turns is not None:
        used_in = turns_using(record, turns, limit=usage_limit,
                              legacy_text=(_event_text(record) if record['kind'] == 'context'
                                           else record.get('name', '')))
    return {'item': record, 'chain': chain, 'supersedes': supersedes, 'used_in': used_in}


def _epoch(value: Any) -> float | None:
    """Seconds since the epoch of a millisecond/second number or an ISO-8601 string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return value / 1000 if value > 10**11 else float(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def turns_using(record: Mapping, turns: Iterable[Mapping], *, limit: int,
                legacy_text: str = '') -> dict:
    """The newest Turns whose context provenance names this item.

    ``preferencesApplied.ids`` and ``memoryPrefetch.memories[].id`` are exact.
    Turns recorded before ids existed are matched by the exact text
    (``preferencesApplied.items``) or name (``memoryPrefetch``) they did record.
    A Turn older than the item cannot have used it and is skipped; the walk
    does not stop there, because branch lanes follow the main lane.
    """
    item_id = record['id']
    created = _epoch(record.get('created_at') or record.get('created'))
    field = 'preferencesApplied' if record['kind'] == 'context' else 'memoryPrefetch'
    found: list[dict] = []
    complete = True
    for turn in turns:
        at = _epoch(turn.get('createdAt'))
        if created is not None and at is not None and at < created:
            continue
        projection = turn.get('projection') if isinstance(turn.get('projection'), Mapping) else {}
        value = projection.get(field)
        if not isinstance(value, Mapping):
            continue
        if field == 'preferencesApplied':
            ids = value.get('ids') if isinstance(value.get('ids'), list) else None
            hit = (item_id in ids) if ids is not None else (
                bool(legacy_text) and legacy_text in (value.get('items') or []))
        else:
            hit = any(isinstance(row, Mapping) and (
                row.get('id') == item_id if row.get('id') else
                bool(legacy_text) and row.get('name') == legacy_text)
                for row in value.get('memories') or [])
        if hit:
            if len(found) >= limit:
                complete = False
                break
            found.append({'conversation_id': turn.get('conversationId', ''),
                          'turn_id': turn.get('turnId', ''),
                          'at': (datetime.fromtimestamp(at, timezone.utc).isoformat(timespec='seconds')
                                 .replace('+00:00', 'Z') if at is not None else ''),
                          'field': field})
    return {'turns': found, 'complete': complete}


# ── Model-facing rendering ─────────────────────────────────────────────

def _pointer_text(pointer: Mapping) -> str:
    if pointer.get('item_id'):
        return f'item {pointer["item_id"]}'
    if pointer.get('turn_id'):
        return (f'owner Turn {pointer.get("turn_id")} of {pointer.get("conversation_id", "?")}'
                f' ({pointer.get("at", "")})')
    if pointer.get('evidence'):
        return f'evidence: {pointer["evidence"]}'
    return 'unrecorded'


def _origin_text(record: Mapping) -> str:
    provenance = record.get('provenance') or {}
    parts = [record.get('source_class') or 'unrecorded']
    at = provenance.get('at') or record.get('created_at') or record.get('created') or ''
    if at:
        parts.append(str(at)[:10])
    if provenance.get('turn_id'):
        parts.append(f'Turn {provenance["turn_id"]} of {provenance.get("conversation_id", "?")}')
    if provenance.get('edited_source_class'):
        parts.append(f'last edited by {provenance["edited_source_class"]}'
                     + (f' {str(provenance.get("edited_at", ""))[:10]}' if provenance.get('edited_at') else ''))
    return ', '.join(parts)


def _status_text(record: Mapping) -> str:
    if record['status'] == 'active':
        return 'active'
    reason = f' ({record["status_reason"]})' if record.get('status_reason') else ''
    pointer = record.get('superseded_by') or {}
    return (record['status'] + reason
            + (f'; superseded by {_pointer_text(pointer)}' if pointer else ''))


def _context_line(record: Mapping) -> str:
    return (f'- [{record["id"]}] {record["trust"].upper()} ({record["type"]}) {_summary(record)} — '
            f'{_status_text(record)}; {_origin_text(record)}'
            + (' ← matches' if record.get('score') else ''))


def _lesson_line(record: Mapping) -> str:
    description = f' — {record["description"]}' if record.get('description') else ''
    repeats = f'; mistake repeated {len(record["recurrences"])}x' if record.get('recurrences') else ''
    return (f'- [{record["id"]}] {record["trust"].upper()}: {record["name"]}{description} '
            f'({record["scope"]}; {_status_text(record)}; {_origin_text(record)}{repeats})')


def format_owner_search(result: Mapping) -> str:
    query = result['query']
    context, lessons = result['context'], result['lessons']
    lines = [f'Owner memory for "{query}" (read-only). Cite the item ids you rely on.',
             TRUST_LEGEND, '']
    if not context['available']:
        lines.append(f'My Context: unavailable to this task ({context["reason"]}).')
    elif not (context['items'] or context['inactive_items']):
        lines.append('My Context: no items.')
    else:
        lines.append('My Context — the owner\u2019s own store; only active items are applied to turns:')
        lines.extend(_context_line(record) for record in context['items'])
        if context['inactive_items']:
            lines.append('Not applied (needs confirmation or superseded):')
            lines.extend(_context_line(record) for record in context['inactive_items'])
    if context.get('withheld_identity'):
        lines.append(f'{context["withheld_identity"]} identity item(s) are not shown to delegated roles.')
    lines.append('')
    if not lessons['total_matches']:
        lines.append(f'Lessons: none match "{query}".')
    else:
        shown = len(lessons['items']) + len(lessons['inactive_items'])
        lines.append(f'Lessons (project/global; only active lessons are injected) — '
                     f'{lessons["total_matches"]} match, {shown} shown:')
        lines.extend(_lesson_line(record) for record in lessons['items'])
        lines.extend(_lesson_line(record) for record in lessons['inactive_items'])
    if lessons['sensitive_withheld']:
        lines.append(f'{lessons["sensitive_withheld"]} sensitive lesson(s) are withheld: only the owner '
                     'sees them, in the Memory settings.')
    lines.extend(['', 'Open one item with its full text, why it is believed and the Turns that used '
                  'it: search_memories(memory_id="<id>").'])
    return redact_secret_shapes('\n'.join(lines))


def format_explanation(result: Mapping | None, item_id: str) -> str:
    if result is None:
        return (f'No owner-memory item has id "{item_id}". Use search_memories(mode="owner", '
                'query=...) to find ids.')
    record = result['item']
    if record.get('withheld'):
        return (f'Lesson {item_id} is marked sensitive: its content is withheld from every role. '
                'Only the owner sees it, in the Memory settings.')
    if record['kind'] == 'context':
        lines = [f'[{record["id"]}] My Context item ({record["type"]}) — trust: {record["trust"]}; '
                 f'status: {_status_text(record)}',
                 f'Text: {_summary(record)}']
    else:
        lines = [f'[{record["id"]}] Lesson ({record["scope"]}) — trust: {record["trust"]}; '
                 f'status: {_status_text(record)}',
                 f'Name: {record["name"]}', f'Description: {record["description"]}']
    lines.append(f'Provenance: {_origin_text(record)}')
    extra = {key: value for key, value in (record.get('provenance') or {}).items()
             if key not in ('source_class', 'at', 'turn_id', 'conversation_id', 'supersedes')
             and not key.startswith('edited_')}
    if extra:
        lines.append('Provenance details: ' + ', '.join(f'{key}={value}' for key, value in extra.items()))
    for link in result['chain']:
        successor = link.get('successor')
        lines.append(f'Chain: {link["from"]} is {link["status"]}'
                     + (f' ({link["status_reason"]})' if link['status_reason'] else '')
                     + f', superseded by {_pointer_text(link["pointer"])}'
                     + (f' → {successor["id"]} ({successor["trust"]}): {successor["summary"]}'
                        if successor else ''))
    for row in result['supersedes']:
        lines.append(f'Supersedes: {row["id"]} ({row["status"]}'
                     + (f', {row["status_reason"]}' if row['status_reason'] else '')
                     + f'): {row["summary"]}')
    if record.get('recurrences'):
        lines.append(f'Mistake repeated {len(record["recurrences"])} time(s) after it was '
                     'recorded: ' + ', '.join(f'{row["turn"]} ({row["at"][:10]})'
                                              for row in record['recurrences']))
    used = result.get('used_in')
    if used is not None:
        if used['turns']:
            lines.append('Used in this conversation: ' + ', '.join(
                f'Turn {row["turn_id"]} ({row["at"]})' for row in used['turns'])
                + ('' if used['complete'] else ' (newest only)'))
        else:
            lines.append('Not used in any Turn of this conversation.')
    lines.append(TRUST_LEGEND)
    if record['kind'] == 'lesson':
        lines.extend(['', record.get('body', '')])
    return redact_secret_shapes('\n'.join(lines))


__all__ = ['TRUST_LEGEND', 'explain_memory_item', 'format_explanation', 'format_owner_search',
           'search_owner_memory', 'turns_using']
