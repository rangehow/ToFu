"""lib/longform/runtime.py — TaskRuntime for long-form report tasks.

The third production capability (P7), deliberately built on the substrate
as it stands today so that whatever it has to DUPLICATE becomes the evidence
for what P6 should extract. Mirrors :mod:`lib.motion_video.runtime` and
:mod:`lib.paper.podcast_runtime` — same lifecycle, same dedup index, same
stale cleanup.

Events: ``stage`` (from the stage graph) / ``phase`` / ``done`` / ``error``.
"""

from __future__ import annotations

import time
import uuid

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)

_longform_runtime = TaskRuntime('longform-report', ttl=3600,
                                push_channel='longform',
                                error_source='lib.longform.engine')
_longform_tasks = _longform_runtime._tasks       # type: ignore[attr-defined]
_longform_tasks_lock = _longform_runtime._lock   # type: ignore[attr-defined]
_longform_dedup_index: dict[tuple, str] = {}


def _longform_index_get(key: tuple):
    """Return a live task_id for the dedup key, pruning stale entries."""
    tid = _longform_dedup_index.get(key)
    if not tid:
        return None
    with _longform_tasks_lock:
        t = _longform_tasks.get(tid)
        if t and t.get('status') in ('pending', 'running'):
            return tid
    _longform_dedup_index.pop(key, None)
    return None


def _longform_index_register(key: tuple, task_id: str) -> None:
    _longform_dedup_index[key] = task_id


def _new_longform_task(task_id: str, *, topic: str, workdir: str, lang: str,
                       depth: str, conv_id: str = ''):
    """Create + register a pending long-form task with the engine's shape."""
    task = _longform_runtime.create(
        task_id=task_id,
        meta={'topic': topic, 'lang': lang, 'depth': depth},
    )
    task.update({
        'task_id': task_id,
        'topic': topic,
        'workdir': workdir,
        'lang': lang,
        'depth': depth,
        'conv_id': conv_id,
        'status': 'pending',
        'result': None,
        'updated_at': time.time(),
    })
    return task


def _append_longform_event(task, event):
    _longform_runtime.append_event(task['task_id'], event)
    task['updated_at'] = time.time()


def _cleanup_stale_longform_tasks():
    """Drop finished/error/aborted tasks past TTL; prune dedup entries."""
    now = time.time()
    with _longform_tasks_lock:
        stale = [tid for tid, t in _longform_tasks.items()
                 if t.get('status') in ('done', 'error', 'aborted')
                 and now - t.get('updated_at', now) > _longform_runtime.ttl]
        for tid in stale:
            _longform_tasks.pop(tid, None)
    for key, tid in list(_longform_dedup_index.items()):
        with _longform_tasks_lock:
            if tid not in _longform_tasks:
                _longform_dedup_index.pop(key, None)
    if stale:
        logger.info('[Longform] cleaned %d stale task(s)', len(stale))


def _longform_task_id():
    return f'longform_{uuid.uuid4().hex[:16]}'


__all__ = [
    '_longform_runtime', '_longform_tasks', '_longform_tasks_lock',
    '_longform_index_get', '_longform_index_register', '_new_longform_task',
    '_append_longform_event', '_cleanup_stale_longform_tasks',
    '_longform_task_id',
]
