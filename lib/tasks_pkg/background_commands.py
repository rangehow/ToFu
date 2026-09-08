"""Bounded process-local control registry for explicit background commands.

Conversation completions use the existing durable queue + inbox handoff lane.
Headless commands retain queryable results until capacity eviction or process
exit; running entries are never evicted. Controls are scoped to their owner.
"""
from __future__ import annotations

import contextvars
import threading
import time
import uuid
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)
MAX_BACKGROUND_COMMANDS = 32
_registry: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


class BackgroundCommandTask(dict):
    """Only execution identity/policy crosses the detached lifetime boundary.

    Parent Stop and PID/interrupt state must not leak into an explicitly
    independent command. Neither transcript nor tool-round objects are retained.
    """

    def __init__(self, parent: dict[str, Any]):
        super().__init__({key: parent[key] for key in (
            'id', 'convId', '_userId', '_unattended', '_via_agent_run',
            '_compat_openai', '_compat_anthropic', '_api_key_id',
        ) if key in parent})
        self['_background_isolated'] = True
        self['_background_detached'] = True
        self['aborted'] = False


def background_command_result(command: str, command_id: str, *,
                              has_conversation: bool = True) -> str:
    delivery = (
        'Its completion will be delivered automatically; do not poll. '
        if has_conversation else
        'Use check_background_command with this ID to retrieve its result. '
    )
    return (f'$ {command}\n\n[Command moved to background: {command_id}. '
            f'{delivery}Use kill_background_command to stop it.]')


def start_background_command(*, task: dict[str, Any], command: str,
                             execute: Any, config: dict | None = None) -> str:
    """Reserve capacity before starting exactly one detached worker.

    Callers must supply an execution closure that does not retain foreground
    presentation/task objects. A failed thread start releases its reservation.
    """
    from lib.tasks_pkg.handlers._background_command import _delivery_config

    command_id = f'bg_{uuid.uuid4().hex[:12]}'
    bg_task = BackgroundCommandTask(task)
    delivery_task = {key: bg_task.get(key) for key in ('convId', '_userId')}
    delivery_config = _delivery_config(config)
    done = threading.Event()
    state: dict[str, Any] = {
        'command': command, 'result': None, 'error': None,
        'finished': False, 'started_at': time.time(), 'finished_at': None,
        'delivery_status': ('pending' if delivery_task.get('convId')
                            and delivery_task.get('_userId') else 'not_applicable'),
    }
    # The execution context carries command restrictions/credentials. It is
    # owned only by the worker, not retained in the completed registry entry.
    copied_context = contextvars.copy_context()
    entry = {'done': done, 'state': state, 'task': bg_task,
             'user_id': task.get('_userId'), 'conv_id': task.get('convId') or ''}

    def _worker() -> None:
        error = None
        try:
            result = execute(bg_task)
        except BaseException as exc:
            error = f'{type(exc).__name__}: {exc}'
            result = (f'$ {command}\n\nBackground execution failed with '
                      f'{error}\n[exit code: -1]')
        # Publish a complete result atomically before attempting delivery. No
        # exception/traceback (and thus parent frames) lives in the registry.
        with _lock:
            state.update(result=str(result or ''), error=error, finished=True,
                         finished_at=time.time())
            entry['task'] = None
            done.set()
        if delivery_task.get('convId') and delivery_task.get('_userId'):
            from lib.tasks_pkg.handlers._background_command import _queue_completion
            published = _queue_completion(
                task=delivery_task, config=delivery_config,
                command_id=command_id, command=command, result=state['result'])
            with _lock:
                state['delivery_status'] = 'queued' if published else 'failed'
        logger.info('[BackgroundCommand:%s] finished', command_id)

    with _lock:
        if len(_registry) >= MAX_BACKGROUND_COMMANDS:
            _evict_finished()
            if len(_registry) >= MAX_BACKGROUND_COMMANDS:
                raise RuntimeError('Background command capacity exhausted '
                                   f'({MAX_BACKGROUND_COMMANDS})')
        _registry[command_id] = entry
    try:
        worker = threading.Thread(
            target=lambda: copied_context.run(_worker),
            name=f'bg-cmd-{command_id[3:]}', daemon=True)
        worker.start()
    except BaseException:
        with _lock:
            _registry.pop(command_id, None)
        raise
    return command_id


def _evict_finished() -> None:
    """Evict the oldest completed entry only, with _lock held."""
    finished = [(cid, entry) for cid, entry in _registry.items()
                if entry['done'].is_set()]
    if finished:
        cid, _ = min(finished, key=lambda item: item[1]['state']['finished_at'])
        del _registry[cid]


def _owned(entry: dict | None, user_id: Any) -> bool:
    # None is the explicit anonymous/headless scope, never a privileged bypass.
    return entry is not None and entry['user_id'] == user_id


def _snapshot(command_id: str, entry: dict) -> dict[str, Any]:
    state = entry['state']
    finished = entry['done'].is_set()
    return {
        'command_id': command_id, 'command': state['command'],
        'conv_id': entry.get('conv_id', ''),
        'status': 'completed' if finished else 'running',
        'result': state['result'] if finished else None,
        'error': state['error'], 'started_at': state['started_at'],
        'delivery_status': state['delivery_status'],
        'elapsed_s': round((state['finished_at'] or time.time())
                           - state['started_at'], 1),
    }


def check_background_command(command_id: str, *, user_id: Any = None) -> dict | None:
    with _lock:
        entry = _registry.get(command_id)
        return _snapshot(command_id, entry) if _owned(entry, user_id) else None


def kill_background_command(command_id: str, *, user_id: Any = None) -> bool:
    """Signal only this owner's live command; never inherit or kill parent PID."""
    with _lock:
        entry = _registry.get(command_id)
        if not _owned(entry, user_id) or entry['done'].is_set():
            return False
        entry['task']['aborted'] = True
    return True


def list_background_commands(*, user_id: Any = None) -> list[dict[str, Any]]:
    with _lock:
        result = []
        for cid, entry in _registry.items():
            if not _owned(entry, user_id):
                continue
            item = _snapshot(cid, entry)
            item.pop('result')
            item.pop('error')
            item['command'] = item['command'][:200]
            result.append(item)
        return result


__all__ = ['start_background_command', 'background_command_result',
           'check_background_command', 'kill_background_command',
           'list_background_commands']
