"""Memory CRUD, structured My Context, and legacy profile compatibility.

MEMORY ONLY. Skill packages (a different noun — user-installed capability
packs) are served by ``routes/api_v1/skills.py`` under ``/api/v1/skills``.

Routes:
  GET    /api/v1/memory                       — list memories (no packages)
  POST   /api/v1/memory                       — create
  GET    /api/v1/memory/<id>                  — fetch one
  PUT    /api/v1/memory/<id>                  — update, including lifecycle and sensitivity
  DELETE /api/v1/memory/<id>                  — delete
  POST   /api/v1/memory/merge                 — merge multiple
  POST   /api/v1/memory/<id>/toggle           — enable/disable
  GET    /api/v1/owner-memory                 — what Tofu knows about the owner
  GET    /api/v1/owner-memory/<id>            — why one item is believed
  GET    /api/v1/profile                      — personal-preference profile
  PUT    /api/v1/profile                      — hand-edit the profile
  POST   /api/v1/profile/pending/<id>         — confirm/dismiss a proposal

All routes require authentication; mutations don't need ``admin`` scope
because memories are user-owned and the cookie-auth UI uses them
intensively (settings panel + profile editor).
"""

from __future__ import annotations

import os

from quart import Blueprint, request

from lib.quart_sync import request_json
from lib.error_envelope import BadRequest

from lib.api_response import (
    api_bad_request, api_conflict, api_created, api_error, api_forbidden,
    api_not_found, api_ok,
)
from lib.log import get_logger
from lib.memory.contracts import (
    MEMORY_BODY_MAX_CHARS,
    MEMORY_DESCRIPTION_MAX_CHARS,
    MEMORY_NAME_MAX_CHARS,
    MEMORY_TAG_MAX_CHARS,
    MEMORY_TAG_MAX_ITEMS,
)
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_memory_bp = Blueprint('api_v1_memory', __name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def _project_path() -> str:
    """Resolve the project_path from the request, falling back to root.

    The query-string branch is decoded-until-stable via the shared
    ``decode_proxy_path_arg`` seam: a reverse proxy (VS Code ``/proxy/<port>/``)
    can double-encode an already-encoded path, so a GET ``?project_path=…``
    would otherwise arrive as a literal ``%2F…`` string and mis-scope the
    memory list. The JSON-body branch is unaffected (bodies aren't URL-encoded)
    so it takes precedence unchanged.
    """
    explicit = None
    if request.is_json:
        # Parse the body ONCE (each get_json is a cross-thread hop to the loop
        # under the sync shim); the JSON branch still takes precedence.
        explicit = (request_json(silent=True) or {}).get('project_path')
    if not explicit:
        from lib.request_parser import decode_proxy_path_arg
        explicit = decode_proxy_path_arg('project_path')
    return explicit or _PROJECT_ROOT


def _write_provenance() -> dict:
    """The write path's provenance for a memory write through this API.

    Only the owner acting in the Tofu UI states anything as the owner; an API
    key, script or agent writing here is an agent report (the same evidence
    as ``auth.human_ui_provenance``).
    """
    from datetime import datetime, timezone
    from .auth import request_provenance
    human = request_provenance().get('kind') == 'human-ui'
    return {'source_class': 'owner_stated' if human else 'agent_report',
            'via': 'api',
            'at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}


# ── Local Memory CRUD ────────────────────────────────────────────────

@api_v1_memory_bp.route('/api/v1/memory', methods=['GET'])
@require_auth
@api_meta(
    summary='List memories',
    description=(
        'Returns ``{memories: [...]}`` — flat memories ONLY (installed '
        'skill packages are a different noun, served by '
        '``/api/v1/skills``). Use ``?scope=all|project|global`` to filter. '
        'Pass ``?summary=1`` to omit the (potentially large) ``body`` of '
        'each memory — list UIs fetch the body lazily via '
        '``GET /api/v1/memory/<id>`` when a card is expanded.'
    ),
    tags=['memory'],
)
def list_memories_v1():
    from lib.memory.storage import list_memories
    scope = request.args.get('scope', 'all')
    summary = request.args.get('summary', '').lower() in ('1', 'true', 'yes')
    memories = [m for m in list_memories(
        project_path=_project_path(), scope=scope, include_body=not summary)
                if not m.get('is_package')]
    for m in memories:
        m.pop('filepath', None)
        if summary:
            m.pop('body', None)
    return api_ok({'memories': memories})


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['GET'])
@require_auth
@api_meta(summary='Get one memory', tags=['memory'])
def get_memory_v1(memory_id):
    from lib.memory.storage import get_memory
    mem = get_memory(memory_id, project_path=_project_path())
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return api_ok(mem)


@api_v1_memory_bp.route('/api/v1/memory', methods=['POST'])
@require_auth
@api_meta(
    summary='Create a memory',
    tags=['memory'],
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'type': 'object', 'required': ['name'], 'properties': {
            'name': {'type': 'string', 'minLength': 1,
                     'maxLength': MEMORY_NAME_MAX_CHARS},
            'description': {'type': 'string',
                            'maxLength': MEMORY_DESCRIPTION_MAX_CHARS},
            'body': {'type': 'string',
                     'maxLength': MEMORY_BODY_MAX_CHARS},
            'tags': {'type': 'array', 'maxItems': MEMORY_TAG_MAX_ITEMS,
                     'uniqueItems': True,
                     'items': {'type': 'string', 'minLength': 1,
                               'maxLength': MEMORY_TAG_MAX_CHARS}},
            'scope': {'type': 'string', 'enum': ['global', 'project']}}}}}},
)
def create_memory_v1():
    from lib.memory.storage import create_memory
    data = parse_body(force=True, exact=('name', 'description', 'body', 'tags', 'scope'))
    name = data.get('name', 'Untitled')
    logger.info('[Memory.v1] creating %r (scope=%s)', name,
                data.get('scope', 'global'))
    try:
        mem = create_memory(
            name=name,
            description=data.get('description', ''),
            body=data.get('body', ''),
            tags=data.get('tags'),
            scope=data.get('scope', 'global'),
            project_path=_project_path(),
            provenance=_write_provenance(),
        )
    except BadRequest as e:
        return api_bad_request(e)
    logger.info('[Memory.v1] created %s', mem.get('id', '?'))
    mem.pop('filepath', None)
    return api_created(mem)


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['PUT'])
@require_auth
@api_meta(
    summary='Update a memory',
    description=(
        'Replaces the given fields. Lifecycle: ``status`` ``active``, ``disproved`` or '
        '``superseded``; a non-active status needs ``status_reason`` and exactly one '
        '``superseded_by`` pointer (``{item_id}``, ``{conversation_id, turn_id, at}`` or '
        '``{evidence}``); the lesson stays on disk and is never injected. '
        '``sensitive: true`` withholds the lesson from every role. The server '
        'records who made the change in ``provenance``.'),
    tags=['memory'],
)
def update_memory_v1(memory_id):
    from lib.memory.storage import MemoryRevisionConflict, update_memory
    data = parse_body(force=True)
    try:
        mem = update_memory(memory_id, data, project_path=_project_path(),
                            provenance=_write_provenance())
    except MemoryRevisionConflict as e:
        return api_conflict(e)
    except BadRequest as e:
        # Payload bounds/package authority failures are actionable 400s.
        return api_bad_request(e)
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return api_ok(mem)


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete a memory', tags=['memory'])
def delete_memory_v1(memory_id):
    from lib.memory.storage import MemoryRevisionConflict, delete_memory
    logger.warning('[Memory.v1] deleting %s', memory_id)
    try:
        ok = delete_memory(memory_id, project_path=_project_path())
    except MemoryRevisionConflict as e:
        return api_conflict(e)
    except BadRequest as e:
        # Package guard: uninstall skill packages via /api/v1/skills.
        return api_bad_request(e)
    if not ok:
        logger.warning('[Memory.v1] %s not found for deletion', memory_id)
        return api_not_found('Memory not found', deleted=False)
    return api_ok(deleted=True)


@api_v1_memory_bp.route('/api/v1/memory/merge', methods=['POST'])
@require_auth
@api_meta(
    summary='Merge multiple memories',
    description='Body: ``{memory_ids, name, description, body, tags?, scope?}``',
    tags=['memory'],
)
def merge_memories_v1():
    from lib.memory.storage import MemoryRevisionConflict, merge_memories
    data = parse_body(force=True, exact=('memory_ids', 'name', 'description', 'body', 'tags', 'scope'))
    logger.info('[Memory.v1] merging %s → %s',
                data.get('memory_ids', []), data.get('name', '?'))
    try:
        result = merge_memories(
            memory_ids=data.get('memory_ids', []),
            name=data.get('name', 'Merged Memory'),
            description=data.get('description', ''),
            body=data.get('body', ''),
            tags=data.get('tags'),
            scope=data.get('scope', 'project'),
            project_path=_project_path(),
            provenance=_write_provenance(),
        )
    except MemoryRevisionConflict as e:
        return api_conflict(e)
    except BadRequest as e:
        logger.debug('[Memory.v1] merge validation error: %s', e)
        return api_bad_request(e)
    result['merged_memory'].pop('filepath', None)
    return api_created(result)


@api_v1_memory_bp.route('/api/v1/memory/<memory_id>/toggle',
                         methods=['POST'])
@require_auth
@api_meta(summary='Enable / disable a memory', tags=['memory'])
def toggle_memory_v1(memory_id):
    from lib.memory.storage import MemoryRevisionConflict, toggle_memory
    data = parse_body(exact=('enabled',))
    try:
        mem = toggle_memory(memory_id, enabled=data.get('enabled'),
                            project_path=_project_path())
    except MemoryRevisionConflict as e:
        return api_conflict(e)
    except BadRequest as e:
        return api_bad_request(e)
    if not mem:
        return api_not_found('Memory not found')
    mem.pop('filepath', None)
    return api_ok(mem)


@api_v1_memory_bp.route('/api/v1/memory/actions/clear', methods=['GET'])
@require_auth
@api_meta(summary='Preview clearing visible experience memories', tags=['memory'])
def preview_clear_memories_v1():
    from lib.auth_mode import is_multi_user
    if is_multi_user():
        return api_forbidden('Bulk memory clearing is disabled in multi-user mode')
    from lib.memory.storage import clear_memories
    return api_ok(clear_memories(project_path=_project_path(), dry_run=True))


@api_v1_memory_bp.route('/api/v1/memory/actions/clear', methods=['POST'])
@require_auth
@api_meta(summary='Clear visible experience memories', tags=['memory'])
def clear_memories_v1():
    from lib.auth_mode import is_multi_user
    if is_multi_user():
        return api_forbidden('Bulk memory clearing is disabled in multi-user mode')
    data = parse_body(exact=('confirm',))
    if data.get('confirm') is not True:
        return api_bad_request('confirm=true is required')
    from lib.memory.storage import clear_memories
    result = clear_memories(project_path=_project_path(), dry_run=False)
    logger.warning('[Memory.v1] cleared %d visible memories (%d failed)',
                   len(result['deleted_ids']), len(result['failed_ids']))
    return api_ok(result)


# ── Structured "My Context" CRUD ───────────────────────────────────

def _context_scope() -> str:
    import lib.memory.user_profile as up
    from .auth import current_auth
    return up.resolve_profile_scope(current_auth())


# ── Owner memory: what Tofu knows about the owner, and why ─────────

def _top_k_arg():
    from lib.memory.contracts import MEMORY_SEARCH_TOP_K_DEFAULT
    raw = request.args.get('top_k')
    if raw is None:
        return MEMORY_SEARCH_TOP_K_DEFAULT
    try:
        return int(raw)
    except ValueError:
        raise BadRequest('top_k must be an integer', field='top_k') from None


@api_v1_memory_bp.route('/api/v1/owner-memory', methods=['GET'])
@require_auth
@api_meta(
    summary='Search what Tofu knows about the owner',
    description=(
        'Returns ``{query, context, lessons}``. ``context`` holds every My Context item: '
        '``items`` are active, ``inactive_items`` need confirmation or are superseded. '
        '``lessons`` holds the lessons matching ``?query=`` (active ``items``, disproved '
        'or superseded ``inactive_items``), sensitive ones included and flagged. Each '
        'record has ``trust`` (owner-stated, verified, inferred, disproved), '
        '``source_class``, ``status``, ``status_reason``, ``superseded_by`` and '
        '``provenance``. ``?top_k=`` bounds the lessons.'),
    tags=['memory'],
)
def search_owner_memory_v1():
    from lib.memory.contracts import normalize_memory_search
    from lib.memory.owner_memory import search_owner_memory
    try:
        query, top_k = normalize_memory_search(request.args.get('query', ''), _top_k_arg())
    except BadRequest as e:
        return api_bad_request(e)
    return api_ok(search_owner_memory(
        query, scope=_context_scope(), project_path=_project_path(), top_k=top_k,
        identity=True, sensitive=True))


@api_v1_memory_bp.route('/api/v1/owner-memory/<item_id>', methods=['GET'])
@require_auth
@api_meta(
    summary='Why Tofu believes one owner-memory item',
    description=(
        'Returns ``{item, chain, supersedes, used_in}`` for a My Context item or lesson: '
        'its record with ``trust`` and ``provenance``, the supersession chain it starts, '
        'the items it superseded, and, with ``?conversation_id=``, the newest Turns of '
        'that conversation whose context applied it (``used_in``; ``?top_k=`` bounds '
        'them). 404 when no item has this id.'),
    tags=['memory'],
)
def explain_owner_memory_v1(item_id):
    from lib.memory.contracts import normalize_memory_search
    from lib.memory.owner_memory import explain_memory_item
    from .auth import request_user_id
    try:
        _query, top_k = normalize_memory_search('', _top_k_arg())
    except BadRequest as e:
        return api_bad_request(e)
    turns = None
    conversation_id = (request.args.get('conversation_id') or '').strip()
    if conversation_id:
        from lib.turn_lifecycle import iter_turns_newest_first
        turns = iter_turns_newest_first(conversation_id, user_id=request_user_id())
    result = explain_memory_item(
        item_id, scope=_context_scope(), project_path=_project_path(), identity=True,
        sensitive=True, turns=turns, usage_limit=top_k)
    if result is None:
        return api_not_found('Owner-memory item not found')
    return api_ok(result)


@api_v1_memory_bp.route('/api/v1/context', methods=['GET'])
@require_auth
@api_meta(summary='Get durable user context', tags=['memory'])
def get_user_context_v1():
    import lib.memory.user_profile as up
    return api_ok(up.context_status(_context_scope()))


@api_v1_memory_bp.route('/api/v1/context', methods=['PUT'])
@require_auth
@api_meta(summary='Replace durable user context', tags=['memory'])
def put_user_context_v1():
    import lib.memory.user_profile as up
    data = parse_body(exact=('items',))
    try:
        result = up.save_context_items(data.get('items'), _context_scope())
    except up.ContextValidationError as exc:
        return api_bad_request(str(exc), cap=up.CONTEXT_CHAR_CAP)
    return api_ok(result)


@api_v1_memory_bp.route('/api/v1/context', methods=['POST'])
@require_auth
@api_meta(summary='Create one durable user-context item', tags=['memory'])
def create_user_context_v1():
    import lib.memory.user_profile as up
    data = parse_body(exact=('id', 'type', 'source', 'created_at', 'updated_at', 'condition', 'action', 'text'))
    try:
        result = up.create_context_item(data, _context_scope(), source='manual')
    except up.ContextValidationError as exc:
        return api_bad_request(str(exc), cap=up.CONTEXT_CHAR_CAP)
    return api_created(result)


@api_v1_memory_bp.route('/api/v1/context/<item_id>', methods=['PUT'])
@require_auth
@api_meta(summary='Update one durable user-context item', tags=['memory'])
def update_user_context_v1(item_id):
    import lib.memory.user_profile as up
    try:
        result = up.update_context_item(item_id, parse_body(exact=('id', 'type', 'source', 'created_at', 'updated_at', 'condition', 'action', 'text')), _context_scope())
    except up.ContextValidationError as exc:
        return api_bad_request(str(exc), cap=up.CONTEXT_CHAR_CAP)
    if result is None:
        return api_not_found('Context item not found')
    return api_ok(result)


@api_v1_memory_bp.route('/api/v1/context/<item_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete one durable user-context item', tags=['memory'])
def delete_user_context_v1(item_id):
    import lib.memory.user_profile as up
    if not up.delete_context_item(item_id, _context_scope()):
        return api_not_found('Context item not found')
    return api_ok(deleted=True)


@api_v1_memory_bp.route('/api/v1/context/changes/<change_id>/undo',
                         methods=['POST'])
@require_auth
@api_meta(summary='Undo an assistant-learned context change', tags=['memory'])
def undo_user_context_change_v1(change_id):
    import lib.memory.user_profile as up
    try:
        result = up.undo_context_change(change_id, _context_scope())
    except up.ContextConflictError as exc:
        return api_conflict(str(exc))
    if result.get('not_found'):
        return api_not_found('Context change not found')
    return api_ok(result)


# ── Legacy profile compatibility ────────────────────────────────────

@api_v1_memory_bp.route('/api/v1/profile', methods=['GET'])
@require_auth
@api_meta(
    summary='Get the legacy profile projection',
    description=('Returns ``{body, items, chars, cap, over_cap, pending}`` — '
                 'a compatibility projection of structured My Context. '
                 '``items`` is the structured per-preference view '
                 '(``[{header, text}]``) the settings UI edits; ``body`` is '
                 'the raw markdown. ``pending`` is retained for back-compat '
                 '(now usually empty — new preferences auto-apply).'),
    tags=['memory'],
)
def get_user_profile_v1():
    import lib.memory.user_profile as up
    from .auth import current_auth
    scope = up.resolve_profile_scope(current_auth())
    body = up.load_profile(scope)
    return api_ok({
        'body': body,
        'items': up.parse_items(body),
        'chars': len(body),
        'cap': up.USER_PROFILE_CHAR_CAP,
        'over_cap': up.profile_over_cap(body, scope),
        'pending': up.load_pending(scope),
    })


@api_v1_memory_bp.route('/api/v1/profile', methods=['PUT'])
@require_auth
@api_meta(
    summary='Edit My Context through the legacy profile shape',
    description=('Body: ``{items: [{header, text}]}`` (structured, preferred) '
                 'OR ``{body}`` (raw markdown). An empty items list / body '
                 'clears the profile. Returns the save-result plus the '
                 're-parsed ``items``.'),
    tags=['memory'],
)
def put_user_profile_v1():
    import lib.memory.user_profile as up
    from .auth import current_auth
    scope = up.resolve_profile_scope(current_auth())
    data = parse_body(exact=('body', 'items',))
    if isinstance(data.get('items'), list):
        res = up.save_items(data['items'], scope)
    else:
        res = up.save_profile(data.get('body', ''), scope)
    res['items'] = up.parse_items(scope=scope)
    return api_ok(res)


@api_v1_memory_bp.route('/api/v1/profile/pending/<pending_id>',
                         methods=['POST'])
@require_auth
@api_meta(
    summary='Confirm or dismiss a staged preference proposal',
    description=('Body: ``{accept: bool, text?: str}``. On accept the '
                 '(optionally edited) preference is written into the profile; '
                 'either way the proposal is removed from the pending list. '
                 'This is the propose-then-confirm gate — new preferences are '
                 'NEVER written silently.'),
    tags=['memory'],
)
def resolve_profile_pending_v1(pending_id):
    import lib.memory.user_profile as up
    from .auth import current_auth
    scope = up.resolve_profile_scope(current_auth())
    data = parse_body(exact=('accept', 'text',))
    res = up.resolve_pending(pending_id, accept=bool(data.get('accept')),
                             edited_text=data.get('text'), scope=scope)
    if res.get('busy'):
        return api_error('Pending proposal is already being resolved',
                         status=409)
    if res.get('error'):
        return api_error('Pending proposal could not be resolved', status=500)
    if not res.get('resolved'):
        return api_not_found('Pending proposal not found')
    return api_ok(res)


__all__ = ['api_v1_memory_bp']
