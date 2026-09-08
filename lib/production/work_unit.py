"""lib/production/work_unit.py — WorkUnit projection for production tasks.

Pure projector: ``(task dict, retained events) → workUnit``. The browser
renders the projection verbatim; it never derives lifecycle state itself
(docs/API_CONTRACT.md §2.18, docs/EVENTS.md §1 stage-event grammar).

Lifecycle enum (the ONLY cross-surface status vocabulary):

    queued | running | retrying | done | degraded | failed | aborted

Mapping from the runtime axis (``task['status']``) is total and one-way:
``pending→queued``, ``running→running``, ``done→done``, ``error→failed``,
``aborted→aborted``; ``done`` + ``artifact_quality.degraded`` → ``degraded``
(product axis outranks the delivery axis — see lib/agent_core/task_runtime.py).
An unknown runtime status projects to ``failed``: a status outside the
contract IS a failure mode worth surfacing, never silently relabeled.

Stage grammar (the registered production event types):
``stage_started`` / ``stage_done`` / ``stage_retry`` / ``stage_skipped`` /
``stage_failed`` — all emitted by :func:`lib.production.stages.run_stages`.
``stage_checkpoint_invalidated`` and per-capability extras stay private and
are ignored here.
"""

from __future__ import annotations

from typing import Any, Optional

WORK_UNIT_STATUSES = (
    'queued', 'running', 'retrying', 'done', 'degraded', 'failed', 'aborted')

_STATUS_MAP = {
    '': 'queued',
    'pending': 'queued',
    'running': 'running',
    'done': 'done',
    'error': 'failed',
    'aborted': 'aborted',
}

STAGE_EVENT_TYPES = frozenset((
    'stage_started', 'stage_done', 'stage_retry', 'stage_skipped',
    'stage_failed'))

_STAGE_TERMINAL = ('done', 'skipped')

#: Per-kind public artifact download paths. A kind absent here simply omits
#: the artifact slot — never invent a URL the capability did not publish.
_DOWNLOAD_PATHS = {
    'slides-deck': ('/api/v1/slides/{id}/file', 'pptx'),
}


def _map_status(task: dict) -> str:
    status = str(task.get('status') or '')
    mapped = _STATUS_MAP.get(status, 'failed')
    if mapped == 'done':
        quality = task.get('artifact_quality')
        if isinstance(quality, dict) and quality.get('degraded'):
            return 'degraded'
    return mapped


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def project_stages(events: Optional[list]) -> tuple[list, int]:
    """Rebuild the stage table from retained stage events.

    Returns ``(stages, total)`` where each stage is
    ``{name, index, state, attempt?, elapsedS?, lastErrors?}`` with
    ``state ∈ pending|active|done|skipped|failed``. Only observed stages are
    listed; ``total`` (the max ``total``/``index`` seen) lets the client
    render the not-yet-started remainder as unnamed slots.
    """
    order: list[str] = []
    by_name: dict[str, dict] = {}
    total = 0
    for event in events or []:
        if not isinstance(event, dict):
            continue
        etype = event.get('type')
        if etype not in STAGE_EVENT_TYPES:
            continue
        name = str(event.get('stage') or '')[:80]
        if not name:
            continue
        index = _int(event.get('index'), 0)
        total = max(total, index, _int(event.get('total'), 0))
        stage = by_name.get(name)
        if stage is None:
            stage = {'name': name, 'index': index or (len(order) + 1),
                     'state': 'pending'}
            by_name[name] = stage
            order.append(name)
        if etype == 'stage_skipped':
            stage['state'] = 'skipped'
            stage['resumed'] = bool(event.get('resumed'))
        elif etype == 'stage_started':
            stage['state'] = 'active'
            stage['attempt'] = max(1, _int(event.get('attempt'), 1))
        elif etype == 'stage_retry':
            stage['state'] = 'active'
            stage['attempt'] = _int(event.get('attempt'), 1) + 1
            errors = event.get('errors')
            if isinstance(errors, list):
                stage['lastErrors'] = [str(e)[:160] for e in errors[:4]]
        elif etype == 'stage_failed':
            stage['state'] = 'failed'
            stage['detail'] = str(event.get('detail') or '')[:240]
            errors = event.get('errors')
            if isinstance(errors, list):
                stage['lastErrors'] = [str(e)[:160] for e in errors[:4]]
        else:  # stage_done
            stage['state'] = 'done'
            stage.pop('lastErrors', None)
            try:
                stage['elapsedS'] = round(float(event.get('elapsed_s')), 2)
            except (TypeError, ValueError):
                pass
    stages = sorted((by_name[n] for n in order), key=lambda s: s['index'])
    return stages, total


def _settle_leftover_stages(stages: list, unit_status: str) -> None:
    """A terminal unit must not display a forever-spinning stage.

    The failed stage's last observed event is a start/retry; on a failed unit
    that leftover ``active`` becomes ``failed`` (diagnosis honesty), on any
    other terminal unit it becomes ``skipped`` (e.g. aborted mid-stage — the
    unit chip already carries the aborted truth).
    """
    if unit_status not in ('failed', 'done', 'degraded', 'aborted'):
        return
    for stage in stages:
        if stage['state'] != 'active':
            continue
        stage['state'] = 'failed' if unit_status == 'failed' else 'skipped'


def project_work_unit(task: dict, *, events: Optional[list] = None,
                      compact: bool = False) -> Optional[dict]:
    """Project one production task into its workUnit block.

    ``compact=True`` is the list-view shape (no stages/links/artifacts).
    Returns None for kinds that are not work units (chat).
    """
    if not isinstance(task, dict):
        return None
    kind = str(task.get('kind') or '')
    if not kind or kind == 'chat':
        return None
    status = _map_status(task)
    meta = task.get('meta') if isinstance(task.get('meta'), dict) else {}
    title = (meta.get('topic') or task.get('topic') or meta.get('title')
             or meta.get('direction') or task.get('direction') or '')
    unit: dict[str, Any] = {
        'kind': kind,
        'title': str(title)[:200],
        'status': status,
        'failureKind': None,
    }
    quality = task.get('artifact_quality')
    if isinstance(quality, dict) and quality.get('degraded'):
        unit['quality'] = {'degraded': True,
                           'reason': str(quality.get('reason') or '')[:400]}
    if status == 'failed':
        error = task.get('error')
        if isinstance(error, dict):
            unit['failureKind'] = str(error.get('kind') or '')[:80] or None
            unit['failure'] = {
                'message': str(error.get('message') or '')[:400],
                'detail': str(error.get('detail') or '')[:600],
                'hint': str(error.get('hint') or '')[:400],
            }
    if compact:
        return unit

    task_id = str(task.get('id') or '')
    stages, total = project_stages(
        events if events is not None else task.get('events'))
    _settle_leftover_stages(stages, status)
    unit['stages'] = stages
    unit['stageTotal'] = total
    if any(s['state'] == 'active' and s.get('attempt', 1) > 1
           for s in stages):
        unit['status'] = 'retrying' if status == 'running' else status
    links = {
        'events': f'/api/v1/tasks/{task_id}/events',
        'abort': f'/api/v1/tasks/{task_id}/abort',
    }
    artifacts: list[dict] = []
    download = _DOWNLOAD_PATHS.get(kind)
    if download and status in ('done', 'degraded'):
        path, label = download
        result = task.get('result')
        artifact: dict[str, Any] = {
            'label': label,
            'href': path.format(id=task_id),
        }
        if isinstance(result, dict):
            if result.get('bytes'):
                artifact['bytes'] = _int(result.get('bytes'), 0)
            if result.get('pages'):
                artifact['pages'] = _int(result.get('pages'), 0)
        artifacts.append(artifact)
    unit['artifacts'] = artifacts
    unit['links'] = links
    conv_id = str(task.get('conv_id') or '')[:120]
    if conv_id:
        unit['convId'] = conv_id
    return unit


__all__ = [
    'STAGE_EVENT_TYPES',
    'WORK_UNIT_STATUSES',
    'project_stages',
    'project_work_unit',
]
