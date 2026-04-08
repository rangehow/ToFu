# HOT_PATH
"""Memory management tool handlers: create, update, delete, merge memories."""

from __future__ import annotations

from lib.log import get_logger
from lib.memory import MEMORY_TOOL_NAMES
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry

logger = get_logger(__name__)


# ── Memory operation handlers (registry pattern) ────────────────────

def _memory_create(fn_args, project_path):
    from lib.memory import create_memory
    mem = create_memory(
        name=fn_args.get('name', 'Untitled Memory'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        tags=fn_args.get('tags', []),
        scope=fn_args.get('scope', 'project'),
        project_path=project_path,
    )
    content = (
        f"✅ Memory created: **{mem['name']}** "
        f"(id: {mem['id']}, scope: {mem['scope']})\n\n"
        f"This memory will be available in future conversations when Memory mode is enabled."
    )
    return content, '💡 saved', f"💡 Memory: {fn_args.get('name', '?')}"


def _memory_update(fn_args, project_path):
    from lib.memory import update_memory
    sid = fn_args.get('memory_id', '')
    mem = update_memory(
        memory_id=sid,
        updates={k: v for k, v in fn_args.items() if k not in ('memory_id',) and v is not None},
        project_path=project_path,
    )
    if mem is None:
        logger.warning('[Memory] update_memory returned None for memory_id=%s', sid)
        return f"❌ Memory not found: {sid}", '❌ not found', f"✏️ Memory: {sid}"
    return f"✅ Memory updated: **{mem['name']}** (id: {mem['id']})", '✏️ updated', f"✏️ Memory: {mem['name']}"


def _memory_delete(fn_args, project_path):
    from lib.memory import delete_memory
    sid = fn_args.get('memory_id', '')
    deleted = delete_memory(memory_id=sid, project_path=project_path)
    if deleted:
        return f"✅ Memory deleted: {sid}", '🗑️ deleted', f"🗑️ Memory: {sid}"
    return f"❌ Memory not found: {sid}", '❌ not found', f"🗑️ Memory: {sid}"


def _memory_merge(fn_args, project_path):
    from lib.memory import merge_memories
    result = merge_memories(
        memory_ids=fn_args.get('memory_ids', []),
        name=fn_args.get('name', 'Merged Memory'),
        description=fn_args.get('description', ''),
        body=fn_args.get('body', ''),
        tags=fn_args.get('tags', []),
        scope=fn_args.get('scope', 'project'),
        project_path=project_path,
    )
    merged = result['merged_memory']
    n_del = len(result['deleted_ids'])
    return (
        f"✅ Merged {n_del} memories → **{merged['name']}** (id: {merged['id']}, scope: {merged['scope']})",
        f'🔀 merged {n_del}',
        f"🔀 Merge → {fn_args.get('name', '?')}",
    )


# Module-level dispatch table — maps memory fn_name → handler.
_MEMORY_OP_DISPATCH = {
    'create_memory': _memory_create,
    'update_memory': _memory_update,
    'delete_memory': _memory_delete,
    'merge_memories': _memory_merge,
}


@tool_registry.tool_set(MEMORY_TOOL_NAMES, category='memory',
                        description='Create, update, delete, or merge memories')
def _handle_memory_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    _proj = project_path if project_enabled else None
    memory_ok = False
    try:
        handler = _MEMORY_OP_DISPATCH.get(fn_name)
        if handler is None:
            raise ValueError(f'Unknown memory operation: {fn_name}')
        tool_content, badge_ok, title = handler(fn_args, _proj)
        memory_ok = True
    except Exception as e:
        logger.warning('[Executor] memory operation %s failed: %s', fn_name, e, exc_info=True)
        tool_content = f"❌ Failed to {fn_name.replace('_', ' ')}: {e}"
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
