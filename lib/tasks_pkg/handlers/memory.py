# HOT_PATH
"""Memory tool handlers: lesson CRUD and the read-only owner-memory lookup.

Every write a model makes through these tools is an agent report: the handler,
not a model argument, stamps that source class with the conversation and Turn
(``provenance``). ``search_memories`` also answers ``mode='owner'`` and
``memory_id`` through ``lib.memory.owner_memory`` for every role that holds it.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.memory.tools import MEMORY_TOOL_NAMES, UPDATE_MEMORY_TOOL
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry

logger = get_logger(__name__)

# The fields a model may change are exactly its tool schema's; ``sensitive``,
# ``enabled`` and provenance stay with the owner's API and the write path.
_MODEL_UPDATE_FIELDS = frozenset(
    UPDATE_MEMORY_TOOL['function']['parameters']['properties']) - {'memory_id'}


def _lead_role(task):
    """``global_router``/``project_coordinator`` for a project lead, else ``''``.

    A failed role lookup raises: a write must not proceed on an unknown role.
    """
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    scope = coordinator_scope(task or {})
    if not scope:
        return ''
    from lib.router.policy import is_router_scope
    return 'global_router' if is_router_scope(scope) else 'project_coordinator'


def _agent_provenance(task):
    """The write path's provenance for a model's memory tool call.

    Every model write is an agent report bound to its Turn, whoever wrote it;
    a project lead's write also names its role (``recorded_by``). Nothing a
    model passes can make it owner speech.
    """
    from datetime import datetime, timezone
    task = task or {}
    provenance = {'source_class': 'agent_report',
                  'conversation_id': str(task.get('convId') or ''),
                  'turn_id': str(task.get('_turnId') or ''),
                  'at': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}
    role = _lead_role(task)
    if role:
        provenance['recorded_by'] = role
    return provenance


def _refuse_third_party_write(task):
    """Text from other people never becomes a durable lesson (structural taint gate)."""
    from lib.tasks_pkg.execution_profile import is_third_party_text
    if is_third_party_text(task):
        raise ValueError('Memory writes are unavailable while handling third-party text')


# ── Memory operation handlers (registry pattern) ────────────────────

def _nearest_lessons(fn_args, project_path, extra_paths):
    """The ordinary lesson search for the new lesson's own title and description."""
    from lib.memory.contracts import MEMORY_SEARCH_TOP_K_DEFAULT
    from lib.memory.relevance._search import search_memories_scored
    query = f"{fn_args.get('name', '')}\n{fn_args.get('description', '')}"
    return search_memories_scored(query, project_path, top_k=MEMORY_SEARCH_TOP_K_DEFAULT,
                                  extra_paths=extra_paths)


def _memory_create(fn_args, project_path, extra_paths=None, *, task=None):
    """Write a lesson as new or as a repeat, after the writer saw the nearest ones.

    Without ``repeat_of`` and with matching lessons, nothing is written: the
    result shows them and asks for the decision. ``repeat_of='new'`` (or no
    match) writes a new lesson to the PRIMARY project_path; a lesson id writes
    the text as that lesson's new version and counts the repeat in its store.
    """
    from lib.memory.contracts import MEMORY_RECORD_AS_NEW
    from lib.memory.storage import create_memory, supersede_memory
    _refuse_third_party_write(task)
    repeat_of = fn_args.get('repeat_of')
    if not repeat_of:
        nearest = _nearest_lessons(fn_args, project_path, extra_paths)
        if nearest:
            from lib.memory.relevance._search import format_memory_matches
            from lib.memory.contracts import MEMORY_SEARCH_TOP_K_DEFAULT
            content = (
                'Nothing saved yet: existing lessons match this one. Decide, then call '
                'create_memory again with the same fields plus repeat_of: '
                f'"{MEMORY_RECORD_AS_NEW}" if none describes this problem, or the id of the '
                'lesson whose mistake happened again (your complete text becomes its new '
                'version and its repeat count rises). To only fix a lesson without a '
                'repeat, use update_memory.\n\n'
                + format_memory_matches(nearest, MEMORY_SEARCH_TOP_K_DEFAULT))
            return content, '🔎 check repeats', f"🔎 Memory: {fn_args.get('name', '?')}"
    provenance = _agent_provenance(task)
    if repeat_of and repeat_of != MEMORY_RECORD_AS_NEW:
        updates = {key: fn_args[key] for key in ('name', 'description', 'body', 'tags')
                   if fn_args.get(key) is not None}
        result = supersede_memory(repeat_of, updates, project_path, extra_paths,
                                  provenance={**provenance, 'recorded_as': 'repeat'},
                                  recurrence=True)
        if result is None:
            raise ValueError(f'No lesson has id {repeat_of!r}; use an id the check showed, '
                             f'or repeat_of="{MEMORY_RECORD_AS_NEW}"')
        successor, _old = result
        repeats = len(successor['recurrences'])
        return (f"Recorded as a repeat of {repeat_of}: its mistake has now happened {repeats} "
                f"time(s) after it was recorded. Current version: **{successor['name']}** "
                f"(id: {successor['id']}, scope: {successor['scope']}); {repeat_of} is kept "
                'as superseded history.', '🔁 repeat', f"🔁 Memory: {successor['name']}")
    mem = create_memory(
        name=fn_args.get('name', 'Untitled Memory'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        tags=fn_args.get('tags', []),
        scope=(fn_args.get('scope')
               or ('project' if project_path else 'global')),
        project_path=project_path,
        provenance={**provenance, 'recorded_as': 'new'},
    )
    content = (
        f"Memory created: **{mem['name']}** "
        f"(id: {mem['id']}, scope: {mem['scope']})\n\n"
        f"This memory will be available in future conversations when Memory mode is enabled."
    )
    return content, '💡 saved', f"💡 Memory: {fn_args.get('name', '?')}"


_CLAIM_FIELDS = ('name', 'description', 'body')
_LIFECYCLE_FIELDS = ('status', 'status_reason', 'superseded_by')


def _memory_verdict(fn_args, project_path, extra_paths, task):
    """Record a pending verdict; it settles when this Worker's result is accepted."""
    from lib.memory.verification import record_verdict
    from lib.project_dispatch.admission import is_dispatch_task
    task = task or {}
    others = [key for key in _MODEL_UPDATE_FIELDS - {'verdict'} if fn_args.get(key) is not None]
    if others:
        raise ValueError('verdict is recorded alone; change ' + ', '.join(sorted(others))
                         + ' in a separate update_memory call')
    if not is_dispatch_task(task) or _lead_role(task):
        raise ValueError('A verdict settles only when a reviewer accepts a project Worker '
                         'result; outside one, disprove with status=disproved and evidence')
    lesson = record_verdict(fn_args.get('memory_id', ''), fn_args['verdict'],
                            project_path=project_path, extra_paths=extra_paths,
                            conversation_id=str(task.get('convId') or ''),
                            turn_id=str(task.get('_turnId') or ''))
    effect = ('verified' if fn_args['verdict']['result'] == 'confirmed' else 'disproved')
    return (f"Verdict recorded for **{lesson['name']}** (id: {lesson['id']}): it becomes "
            f"{effect} when a reviewer accepts this result; until then nothing changes.",
            '🧾 verdict', f"🧾 Memory: {lesson['name']}")


def _memory_update(fn_args, project_path, extra_paths=None, *, task=None):
    from lib.memory.storage import supersede_memory, update_memory
    _refuse_third_party_write(task)
    if fn_args.get('verdict') is not None:
        return _memory_verdict(fn_args, project_path, extra_paths, task)
    sid = fn_args.get('memory_id', '')
    updates = {k: v for k, v in fn_args.items() if k in _MODEL_UPDATE_FIELDS and v is not None}
    if any(key in updates for key in _CLAIM_FIELDS):
        if any(key in updates for key in _LIFECYCLE_FIELDS):
            raise ValueError('A correction (name, description or body) is saved as a new '
                             'version; change status in a separate update_memory call')
        result = supersede_memory(sid, updates, project_path, extra_paths,
                                  provenance=_agent_provenance(task))
        if result is None:
            logger.warning('[Memory] supersede_memory found no memory_id=%s', sid)
            return f"Memory not found: {sid}", '❌ not found', f"✏️ Memory: {sid}"
        successor, _old = result
        return (f"Correction saved as a new version: **{successor['name']}** "
                f"(id: {successor['id']}). {sid} is kept as superseded history and is no "
                'longer injected; use the new id from now on.', '✏️ new version',
                f"✏️ Memory: {successor['name']}")
    mem = update_memory(
        memory_id=sid,
        updates=updates,
        project_path=project_path,
        extra_paths=extra_paths,
        provenance=_agent_provenance(task),
    )
    if mem is None:
        logger.warning('[Memory] update_memory returned None for memory_id=%s', sid)
        return f"Memory not found: {sid}", '❌ not found', f"✏️ Memory: {sid}"
    status = mem.get('status') or 'active'
    suffix = '' if status == 'active' else f'; status: {status}, no longer injected'
    return (f"Memory updated: **{mem['name']}** (id: {mem['id']}{suffix})", '✏️ updated',
            f"✏️ Memory: {mem['name']}")


def _memory_delete(fn_args, project_path, extra_paths=None, *, task=None):
    from lib.memory.storage import delete_memory
    _refuse_third_party_write(task)
    sid = fn_args.get('memory_id', '')
    deleted = delete_memory(memory_id=sid, project_path=project_path,
                            extra_paths=extra_paths)
    if deleted:
        return f"Memory deleted: {sid}", '🗑️ deleted', f"🗑️ Memory: {sid}"
    return f"Memory not found: {sid}", '❌ not found', f"🗑️ Memory: {sid}"


def _memory_merge(fn_args, project_path, extra_paths=None, *, task=None):
    from lib.memory.storage import merge_memories
    _refuse_third_party_write(task)
    result = merge_memories(
        provenance=_agent_provenance(task),
        memory_ids=fn_args.get('memory_ids', []),
        name=fn_args.get('name', 'Merged Memory'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        # ``None`` means derive the union from source memories; an explicit
        # empty list intentionally clears tags on the replacement.
        tags=fn_args.get('tags'),
        scope=(fn_args.get('scope')
               or ('project' if project_path else 'global')),
        project_path=project_path,
        extra_paths=extra_paths,
    )
    merged = result['merged_memory']
    n_del = len(result['deleted_ids'])
    failed = result.get('failed_ids') or []
    content = (
        f"Merged {n_del} memories → **{merged['name']}** (id: {merged['id']}, scope: {merged['scope']})"
    )
    if failed:
        content += (
            f"\n\n⚠️ {len(failed)} source memory(ies) could NOT be deleted and "
            f"remain as duplicates of the merged memory: {', '.join(failed)}. "
            f"Delete them manually if the merge should replace them."
        )
    return (
        content,
        f'🔀 merged {n_del}' + (f' ({len(failed)} kept)' if failed else ''),
        f"🔀 Merge → {fn_args.get('name', '?')}",
    )


def _owner_lookup_scope(task) -> tuple[str | None, str]:
    """My Context scope this task may read, or ``(None, reason)``.

    Mirrors the composer's ``user_context`` rules: the owner scope stamped at
    task creation, and the owner's preference switch.
    """
    from lib.agent_core.personal_scope import resolve_preferences_enabled
    task = task or {}
    if not resolve_preferences_enabled(task.get('config') or {}):
        return None, 'preferences_disabled'
    scope = task.get('_profileScope')
    return (str(scope), '') if scope else (None, 'profile_scope_missing')


def _identity_audience(task) -> bool:
    """Whether this role may see My Context identity items.

    Delegated Workers and sub-agents never receive them (design record,
    Decision 3). A failed role lookup withholds them.
    """
    task = task or {}
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(task) or task.get('_parentTaskId'):
        return False
    try:
        from lib.project_dispatch.admission import is_dispatch_task
        if not is_dispatch_task(task):
            return True
        from lib.tasks_pkg.coordinator_policy import coordinator_scope
        return bool(coordinator_scope(task))
    except Exception:
        logger.warning('[Memory] role lookup failed; identity items withheld', exc_info=True)
        return False


def _conversation_turns(task):
    """This conversation's Turns, newest first, for the usage half of a lookup."""
    task = task or {}
    if not task.get('convId') or (task.get('config') or {}).get('_storageFreeRuntime'):
        return None
    from lib.tasks_pkg.manager import task_user_id
    from lib.turn_lifecycle import iter_turns_newest_first
    return iter_turns_newest_first(task['convId'], user_id=task_user_id(task))


def _memory_search(fn_args, project_path, extra_paths=None, *, task=None):
    from lib.memory.contracts import (
        MEMORY_SEARCH_MODES, MEMORY_SEARCH_TOP_K_DEFAULT, normalize_memory_search,
    )
    memory_id = fn_args.get('memory_id')
    mode = fn_args.get('mode') or 'lessons'
    if mode not in MEMORY_SEARCH_MODES:
        raise ValueError('mode must be lessons or owner')
    query, top_k = normalize_memory_search(
        fn_args.get('query') or '', fn_args.get('top_k', MEMORY_SEARCH_TOP_K_DEFAULT))
    if memory_id or mode == 'owner':
        from lib.tasks_pkg.execution_profile import is_third_party_text
        if is_third_party_text(task):
            raise ValueError('The owner-memory lookup is unavailable while handling '
                             'third-party text')
        from lib.memory import owner_memory
        scope, reason = _owner_lookup_scope(task)
        identity = _identity_audience(task)
        if memory_id:
            try:
                turns = _conversation_turns(task)
            except Exception:
                logger.warning('[Memory] conversation Turns unavailable for usage', exc_info=True)
                turns = None
            result = owner_memory.explain_memory_item(
                memory_id, scope=scope, project_path=project_path, extra_paths=extra_paths,
                identity=identity, turns=turns, usage_limit=top_k)
            return (owner_memory.format_explanation(result, memory_id), '🔍 looked up',
                    f'🔍 Memory: {memory_id[:80]}')
        if not query.strip():
            return 'Please provide search keywords.', '🔍 searched', '🔍 Owner memory: (empty)'
        result = owner_memory.search_owner_memory(
            query, scope=scope, project_path=project_path, extra_paths=extra_paths,
            top_k=top_k, identity=identity, context_reason=reason)
        return (owner_memory.format_owner_search(result), '🔍 searched',
                f'🔍 Owner memory: {query[:80]}')
    from lib.memory.relevance import search_memories
    result = search_memories(
        query=query,
        project_path=project_path,
        top_k=top_k,
        extra_paths=extra_paths,
    )
    snippet = query[:80] if query else '(empty)'
    return result, '🔍 searched', f'🔍 Memory search: {snippet}'


# Module-level dispatch table — maps memory fn_name → handler.
_MEMORY_OP_DISPATCH = {
    'create_memory': _memory_create,
    'update_memory': _memory_update,
    'delete_memory': _memory_delete,
    'merge_memories': _memory_merge,
    'search_memories': _memory_search,
}


@tool_registry.tool_set(MEMORY_TOOL_NAMES, category='memory',
                        description='Create, update, delete, or merge memories')
def _handle_memory_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    _proj = ((cfg or {}).get('canonicalProjectPath') or project_path) if project_enabled else None
    # Memory belongs to the configured project, not its task-owned checkout.
    _extra_paths = []
    if _proj:
        _all_paths = ((cfg or {}).get('canonicalProjectPaths')
                      or (cfg or {}).get('projectPaths') or [])
        _extra_paths = list(dict.fromkeys(
            p for p in _all_paths if p and p != _proj))
    memory_ok = False
    try:
        handler = _MEMORY_OP_DISPATCH.get(fn_name)
        if handler is None:
            raise ValueError(f'Unknown memory operation: {fn_name}')
        tool_content, badge_ok, title = handler(fn_args, _proj, _extra_paths, task=task)
        memory_ok = True
    except ValueError as e:
        # Invalid model arguments are a settled tool result, not an executor
        # crash. Keep one actionable warning without a misleading traceback;
        # unexpected implementation/storage failures below retain exc_info.
        logger.warning('[Executor] memory operation %s rejected: %s',
                       fn_name, e)
        tool_content = f"Failed to {fn_name.replace('_', ' ')}: {e}"
        badge_ok = '❌ failed'
        title = f"❌ {fn_name}: error"
    except Exception as e:
        logger.warning('[Executor] memory operation %s failed: %s',
                       fn_name, e, exc_info=True)
        tool_content = f"Failed to {fn_name.replace('_', ' ')}: {e}"
        badge_ok = '❌ failed'
        title = f"❌ {fn_name}: error"

    meta = _build_simple_meta(
        fn_name, tool_content, source='Memory',
        title=title,
        snippet=(fn_args.get('description', '') or fn_args.get('memory_id', ''))[:120],
        badge=badge_ok if memory_ok else '❌ failed',
        extra={'memoryOk': memory_ok},
    )
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
