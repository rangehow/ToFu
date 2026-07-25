"""lib/motion_video/runtime.py — TaskRuntime for motion-video tasks.

Background video generation (SRT → storyboard → narrate → render → concat
→ mux) with a dedup index so a second identical request joins the
in-flight task instead of regenerating. Mirrors
:mod:`lib.paper.podcast_runtime` exactly — same lifecycle, same
stale-cleanup; events: ``phase`` / ``scene_done`` / ``final`` / ``done`` /
``error`` / ``aborted``.
"""

from __future__ import annotations

import time
import uuid

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)

_motion_runtime = TaskRuntime('motion-video', ttl=3600,
                              push_channel='motion',
                              error_source='routes.api_v1.motion')
_motion_tasks = _motion_runtime._tasks       # type: ignore[attr-defined]
_motion_tasks_lock = _motion_runtime._lock   # type: ignore[attr-defined]
_motion_dedup_index: dict[tuple, str] = {}
# (srt_sha, voice, alignment, aspect, narration) -> task_id


def _motion_index_get(key: tuple):
    """Return a live task_id for the dedup key, pruning stale entries."""
    tid = _motion_dedup_index.get(key)
    if not tid:
        return None
    with _motion_tasks_lock:
        t = _motion_tasks.get(tid)
        if t and t.get('status') in ('pending', 'running'):
            return tid
    _motion_dedup_index.pop(key, None)
    return None


def _motion_index_register(key: tuple, task_id: str) -> None:
    _motion_dedup_index[key] = task_id


def _new_motion_task(task_id: str, *, srt_path: str, workdir: str,
                     voice: str, speed, alignment: str, narration: bool,
                     quality: str, parallel: int, width: int, height: int,
                     scenes_path: str = ''):
    """Create + register a pending motion task with the engine's field shape."""
    task = _motion_runtime.create(
        task_id=task_id,
        meta={'srt_path': srt_path, 'voice': voice, 'alignment': alignment,
              'narration': narration, 'quality': quality,
              'aspect': f'{width}x{height}'},
    )
    task.update({
        'task_id': task_id,
        'srt_path': srt_path,
        'scenes_path': scenes_path,
        'workdir': workdir,
        'voice': voice,
        'speed': speed,
        'alignment': alignment,
        'narration': narration,
        'quality': quality,
        'parallel': parallel,
        'width': width,
        'height': height,
        'status': 'pending',
        'result': None,
        'updated_at': time.time(),
    })
    return task


def _append_motion_event(task, event):
    """Append one event (monotonic seq + WS push)."""
    _motion_runtime.append_event(task['task_id'], event)
    task['updated_at'] = time.time()


def _cleanup_stale_motion_tasks():
    """Drop finished/error/aborted tasks past TTL; prune dedup entries."""
    now = time.time()
    with _motion_tasks_lock:
        stale = [tid for tid, t in _motion_tasks.items()
                 if t.get('status') in ('done', 'error', 'aborted')
                 and now - t.get('updated_at', now) > _motion_runtime.ttl]
        for tid in stale:
            _motion_tasks.pop(tid, None)
    for key, tid in list(_motion_dedup_index.items()):
        with _motion_tasks_lock:
            if tid not in _motion_tasks:
                _motion_dedup_index.pop(key, None)
    if stale:
        logger.info('[MotionVideo] cleaned %d stale task(s)', len(stale))


def _motion_task_id():
    return f'motion_{uuid.uuid4().hex[:16]}'


__all__ = [
    '_motion_runtime',
    '_motion_tasks',
    '_motion_tasks_lock',
    '_motion_dedup_index',
    '_motion_index_get',
    '_motion_index_register',
    '_new_motion_task',
    '_append_motion_event',
    '_cleanup_stale_motion_tasks',
    '_motion_task_id',
]
