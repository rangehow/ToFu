"""lib/agent_backends/sse_bridge.py — Translate NormalizedEvents to our frontend SSE protocol.

This is the critical bridge between backend-agnostic NormalizedEvents and
our frontend's SSE event format (delta, tool_start, tool_result, phase, done, etc.).

The frontend doesn't know about backends — it always receives the same SSE
events regardless of which backend (builtin, Claude Code, Codex) produced them.

Inspired by Craft Agent's BaseEventAdapter pattern.
"""

from __future__ import annotations

import json
from typing import Any

from lib.agent_backends.protocol import NormalizedEvent, NormalizedEventKind

__all__ = ['normalized_to_sse']

K = NormalizedEventKind


def normalized_to_sse(event: NormalizedEvent) -> dict[str, Any] | None:
    """Translate a NormalizedEvent to our frontend SSE event dict.

    Args:
        event: A NormalizedEvent from any backend.

    Returns:
        SSE event dict ready for ``append_event(task, event)``,
        or None if the event should be silently skipped.

    The mapping covers all NormalizedEventKind values::

        text_delta       → {type: 'delta', content: '...'}
        thinking_delta   → {type: 'delta', thinking: '...'}
        tool_start       → {type: 'tool_start', tool: '...', toolId: '...'}
        tool_output      → {type: 'tool_result', toolId: '...', streaming: true}
        tool_complete    → {type: 'tool_result', toolId: '...', isError: bool}
        file_change      → {type: 'tool_result', tool: 'file_change', ...}
        phase            → {type: 'phase', phase: 'working', detail: '...'}
        approval_request → {type: 'approval_required', ...}
        done             → {type: 'done', ...}
        error            → {type: 'done', error: '...'}
    """
    kind = event.kind

    if kind == K.TEXT_DELTA:
        if not event.text:
            return None
        return {'type': 'delta', 'content': event.text}

    elif kind == K.THINKING_DELTA:
        if not event.text:
            return None
        return {'type': 'delta', 'thinking': event.text}

    elif kind == K.TOOL_START:
        evt: dict[str, Any] = {
            'type': 'tool_start',
            'tool': event.tool_name,
        }
        if event.tool_id:
            evt['toolId'] = event.tool_id
        if event.tool_input:
            evt['args'] = event.tool_input
        return evt

    elif kind == K.TOOL_OUTPUT:
        evt = {
            'type': 'tool_result',
            'streaming': True,
        }
        if event.tool_id:
            evt['toolId'] = event.tool_id
        if event.tool_output:
            evt['result'] = event.tool_output[:5000]
        return evt

    elif kind == K.TOOL_COMPLETE:
        evt = {
            'type': 'tool_result',
        }
        if event.tool_id:
            evt['toolId'] = event.tool_id
        if event.tool_name:
            evt['tool'] = event.tool_name
        if event.tool_output:
            evt['result'] = event.tool_output[:5000]
        evt['isError'] = event.tool_is_error
        return evt

    elif kind == K.FILE_CHANGE:
        return {
            'type': 'tool_result',
            'tool': 'file_change',
            'result': json.dumps({
                'path': event.file_path,
                'action': event.file_action,
            }, ensure_ascii=False),
        }

    elif kind == K.PHASE:
        return {
            'type': 'phase',
            'phase': 'working',
            'detail': event.text,
        }

    elif kind == K.APPROVAL_REQUEST:
        return {
            'type': 'approval_required',
            'tool': event.tool_name,
            'toolId': event.tool_id,
            'args': event.tool_input,
            'detail': event.text,
        }

    elif kind == K.DONE:
        evt = {'type': 'done'}
        if event.finish_reason:
            evt['finishReason'] = event.finish_reason
        if event.usage:
            evt['usage'] = event.usage
        if event.error_message:
            evt['error'] = event.error_message
        if event.session_id:
            evt['sessionId'] = event.session_id
        return evt

    elif kind == K.ERROR:
        return {
            'type': 'done',
            'error': event.error_message or 'Unknown error',
            'finishReason': 'error',
        }

    # Unknown kind — skip silently
    return None
