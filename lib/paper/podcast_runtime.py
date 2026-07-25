"""lib/paper/podcast_runtime.py — TaskRuntime for paper-podcast tasks.

Background podcast generation (report → script → TTS → audio) with a
dedup-by-(paper_hash, mode, lang, voice) index so a second request for the
same podcast joins the in-flight task instead of regenerating. Mirrors
lib/paper/report_runtime.py exactly — same lifecycle, same stale-cleanup,
same event shapes (status / delta / done / error / aborted) plus the
podcast-specific ``script`` / ``segment_done`` / ``audio_ready`` events
the engine emits.
"""

from __future__ import annotations

import threading
import time
import uuid

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)

_podcast_runtime = TaskRuntime('paper-podcast', ttl=3600,
                               push_channel='paper',
                               error_source='routes.paper:podcast')
# Compatibility shims (legacy code in paper.py / tests references these names).
_podcast_tasks = _podcast_runtime._tasks       # type: ignore[attr-defined]
_podcast_tasks_lock = _podcast_runtime._lock   # type: ignore[attr-defined]
_podcast_dedup_index: dict[tuple[str, str, str, str], str] = {}
# (paper_hash, mode, lang, voice) -> task_id


def _podcast_index_get(paper_hash, mode, lang, voice):
    """Return a live task_id for the dedup key, pruning stale entries."""
    key = (paper_hash, mode, lang, voice)
    tid = _podcast_dedup_index.get(key)
    if not tid:
        return None
    with _podcast_tasks_lock:
        t = _podcast_tasks.get(tid)
        if t and t.get('status') in ('pending', 'running'):
            return tid
    _podcast_dedup_index.pop(key, None)
    return None


def _podcast_index_register(paper_hash, mode, lang, voice, task_id):
    _podcast_dedup_index[(paper_hash, mode, lang, voice)] = task_id


def _new_podcast_task(task_id, paper_hash, mode, lang, voice, model):
    """Create + register a pending podcast task (runtime.create registers it
    in the shared registry so poll can find it), augmented with the
    legacy-field shape the worker and poll route read."""
    task = _podcast_runtime.create(
        task_id=task_id,
        meta={'paper_hash': paper_hash, 'mode': mode, 'lang': lang,
              'voice': voice, 'model': model},
    )
    task.update({
        'task_id': task_id,
        'paper_hash': paper_hash,
        'mode': mode,
        'lang': lang,
        'voice': voice,
        'model': model,
        'status': 'pending',
        'script': None,
        'script_meta': None,
        'audio_url': '',
        'duration_sec': 0.0,
        'script_only': False,
        'progress': {'done': 0, 'total': 0},
        'updated_at': time.time(),
    })
    return task


def _append_podcast_event(task, event):
    """Append one event (monotonic seq + WS push, like the report worker)."""
    _podcast_runtime.append_event(task['task_id'], event)
    task['updated_at'] = time.time()


def _cleanup_stale_podcast_tasks():
    """Drop finished/error/aborted tasks past TTL; prune dedup entries."""
    now = time.time()
    with _podcast_tasks_lock:
        stale = [tid for tid, t in _podcast_tasks.items()
                 if t.get('status') in ('done', 'error', 'aborted')
                 and now - t.get('updated_at', now) > _podcast_runtime.ttl]
        for tid in stale:
            _podcast_tasks.pop(tid, None)
    for key, tid in list(_podcast_dedup_index.items()):
        with _podcast_tasks_lock:
            if tid not in _podcast_tasks:
                _podcast_dedup_index.pop(key, None)
    if stale:
        logger.info('[Paper:Podcast] Cleaned %d stale task(s)', len(stale))


def _podcast_task_id():
    return f'podcast_{uuid.uuid4().hex[:16]}'


__all__ = [
    '_podcast_runtime',
    '_podcast_tasks',
    '_podcast_tasks_lock',
    '_podcast_dedup_index',
    '_podcast_index_get',
    '_podcast_index_register',
    '_new_podcast_task',
    '_append_podcast_event',
    '_cleanup_stale_podcast_tasks',
    '_podcast_task_id',
]
