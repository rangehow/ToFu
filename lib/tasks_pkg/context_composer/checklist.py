"""Required checklist projection for composition, admission and retry requests.

Authority stays in ``lib.tools.todo`` and the task's accepted state. This owner
only renders detached active state into the append-only composer lane. It does
not read storage, change a checklist, or restore withdrawn tool permissions.
"""

from __future__ import annotations

import hashlib
import json

from lib.tasks_pkg.context_composer._models import ComposeRequest, ContextBlock
from lib.tasks_pkg.context_composer.registry import context_block
from lib.tools.todo import _normalize_todos, render_todo_list, todo_state_from_task

CHECKLIST_HEADING = '## Active Task Checklist'
_STATE_PREFIX = '<!-- chatui-todo-state:v1:'


def checklist_context_block(content: str) -> ContextBlock:
    """Use one identity and lossless budget for every checklist ingress."""
    # The domain bounds IDs/steps/depth; the registry forbids truncation.
    return context_block('active_checklist', content)


def _message_text(message: dict) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    return '\n'.join(
        block['text'] for block in content or []
        if isinstance(block, dict) and isinstance(block.get('text'), str)
    ) if isinstance(content, list) else ''


def _visible_current(messages: list[dict], body: str, canonical: list[dict],
                     *, versioned: bool) -> bool:
    for message in reversed(messages):
        # Source files and tool results can quote the heading/marker. They
        # are evidence, never runtime continuity carriers. Check full text,
        # not a leading fingerprint that can survive tail truncation.
        if message.get('_contextComposer') and message.get('role') == 'user':
            text = _message_text(message)
            if CHECKLIST_HEADING in text and _STATE_PREFIX in text:
                return body in text
        if versioned or message.get('role') != 'assistant':
            continue
        # Legacy flat-list callers have no frame/revision metadata. A visible
        # exact, explicit-ID call is sufficient for their existing contract;
        # a versioned attempted update never proves accepted runtime state.
        for call in reversed(message.get('tool_calls') or []):
            if not isinstance(call, dict):
                continue
            fn = call.get('function') or {}
            if fn.get('name') != 'todo_write':
                continue
            args = fn.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    return False
            return isinstance(args, dict) and args.get('todos') == canonical
    return False


def build_checklist_context_block(messages: list[dict], task: dict | None
                                  ) -> ContextBlock | None:
    """Return the missing current state, never a truncated optional reminder."""
    if not task or not (task.get('_todoState') or task.get('_todos')):
        return None
    raw_state = task.get('_todoState')
    versioned = isinstance(raw_state, dict)
    # Replan history is audit data, not restoration input. Avoid copying it
    # every round; the domain still normalizes a detached stack snapshot.
    carrier = task
    if versioned:
        carrier = {'_todoState': {**raw_state, 'history': []},
                   '_todos': task.get('_todos')}
    state = todo_state_from_task(carrier)
    stack = state.get('stack') or []
    active = stack[-1] if stack else None
    canonical = _normalize_todos(
        active.get('todos') if active else [] if versioned else task.get('_todos'))
    if not canonical and not active:
        return None

    # Legacy migration synthesizes frame IDs on reads; exclude those from the
    # content identity so an unchanged flat list stays byte-stable.
    frames = ([{'checklist_id': frame['checklist_id'],
                'revision': frame['revision'],
                'parent_todo_id': frame['parent_todo_id']}
               for frame in stack] if versioned else [])
    breadcrumb = ['Root task']
    for index, frame in enumerate(stack[1:], start=1):
        item = next((row for row in stack[index - 1]['todos']
                     if row['id'] == frame['parent_todo_id']), {})
        breadcrumb.append(item.get('content') or frame['parent_todo_id'] or 'Child task')
    identity = {'frames': frames, 'path': breadcrumb, 'todos': canonical}
    fingerprint = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')).hexdigest()[:16]
    lines = [
        CHECKLIST_HEADING, f'{_STATE_PREFIX}{fingerprint} -->',
        'This is the authoritative checklist preserved by the task runtime. '
        'Continue from its current state; do not recreate or restart the plan '
        'merely because earlier todo_write calls were compacted.',
        'Restoration is not a checklist update. Use only currently available '
        'tools when a real transition is needed.',
    ]
    if frames:
        lines.append(f'Active checklist: {json.dumps(active["checklist_id"])} '
                     f'(revision {active["revision"]}, depth {len(stack)}).')
    if len(frames) > 1:
        lines.append('Checklist path: ' + ' > '.join(breadcrumb))
        lines.append('Parent item ID: ' + json.dumps(active['parent_todo_id'], ensure_ascii=False))
        lines.append('Ancestor frames: ' + json.dumps(frames[:-1], ensure_ascii=False,
                                                     separators=(',', ':')))
    lines.append(render_todo_list(canonical) or 'No active checklist items.')
    body = '\n'.join(lines)
    if _visible_current(messages, body, canonical, versioned=versioned):
        return None
    return checklist_context_block('<system-reminder>\n' + body + '\n</system-reminder>')


def restore_checklist_context(messages: list[dict], *, task: dict | None,
                              model: str = '') -> None:
    """Restore once after any destructive projection and before accounting."""
    block = build_checklist_context_block(messages, task)
    if block is None:
        return
    from lib.tasks_pkg.context_composer import append_context_blocks
    append_context_blocks(messages, [block], ComposeRequest(
        task=task, model=model, conv_id=str((task or {}).get('convId') or ''),
    ))
