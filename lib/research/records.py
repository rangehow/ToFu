"""Owner-scoped Research run snapshots on the existing Sidecar record authority.

Public snapshots are bounded, replace-in-place records, not an event journal.
Stage files remain execution checkpoints; their acknowledged snapshots are
backed up here so explicit resume can reconstruct a lost work directory.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import time
import threading
import uuid

from lib.identity import require_user_id
from lib.storage.errors import StorageError
def _storage(*, write=False):
    from .persistence import _storage as storage
    return storage(write=write)


NAMESPACE = 'research.runs.v1'
INDEX_NAMESPACE = 'research.run_index.v1'
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_PAPERS = 200
ACTIVE = ('pending', 'running')
_resume_lock = threading.RLock()


def _key(task_id, user_id):
    uid = require_user_id(user_id, context='research record owner')
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', task_id):
        raise ValueError('invalid research task id')
    return f'{uid}:{task_id}'


def _put(namespace, key, value):
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > MAX_RECORD_BYTES:
        raise ValueError('research record exceeds 8 MiB')
    result = _storage(write=True).command('record.put', {
        'namespace': namespace, 'key': key, 'value': value,
    }, f'research-record:{uuid.uuid4().hex}')
    if not isinstance(result, dict):
        raise StorageError('database_internal', 'Research record write was not acknowledged')


def persist_record(task, *, event=None, status=None, result=None):
    """Commit before emitting business progress; never swallow write failures."""
    key = _key(task['task_id'], task['user_id'])
    record = deepcopy(task.get('_durable_research_record') or {})
    now = int(time.time() * 1000)
    record.update({
        'schema_version': 1, 'task_id': task['task_id'],
        'direction': task.get('direction', ''), 'lang': task.get('lang', 'en'),
        'status': status or task['status'], 'created_at': record.get('created_at', now),
        'updated_at': now, 'research_progress': deepcopy(task.get('research_progress')),
        'request': {field: deepcopy(task.get(field)) for field in
                    ('n_ideas', 'seed_arxiv_ids', 'model', 'conv_id')},
    })
    papers = record.setdefault('papers', [])
    artifact = result if result is not None else task.get('result')
    if isinstance(artifact, dict):
        record['result'] = deepcopy(artifact)
    if event and event.get('type') == 'paper_done':
        raw = event.get('result') or {}
        paper = {k: str(raw.get(k) or '')[:limit] for k, limit in
                 (('arxivId', 160), ('paperId', 160), ('phash', 128),
                  ('title', 500), ('status', 40), ('error', 500))}
        for k in ('pageCount', 'textLength'):
            paper[k] = max(0, int(raw.get(k) or 0))
        paper['degraded'] = bool(raw.get('degraded'))
        previous = next((i for i, p in enumerate(papers)
                         if p['arxivId'] == paper['arxivId']), None)
        if previous is not None:
            papers[previous] = paper
        elif len(papers) < MAX_PAPERS:
            papers.append(paper)
        else:
            raise ValueError('research paper record ceiling exceeded')
    if event and event.get('type') in ('stage_done', 'stage_skipped',
                                       'stage_checkpoint_invalidated'):
        checkpoint = os.path.join(task['workdir'], 'pipeline_state.json')
        if os.path.isfile(checkpoint):
            if os.path.getsize(checkpoint) > MAX_RECORD_BYTES:
                raise ValueError('research checkpoint exceeds 8 MiB')
            with open(checkpoint, encoding='utf-8') as source:
                record['checkpoint'] = json.load(source)
    _put(NAMESPACE, key, record)
    # A compact index avoids loading checkpoint/model bodies for the landing page.
    summary = {k: deepcopy(record[k]) for k in
               ('schema_version', 'task_id', 'direction', 'lang', 'status',
                'created_at', 'updated_at', 'research_progress')}
    index_key = f"{task['user_id']}:{9999999999999 - record['created_at']:013d}:{task['task_id']}"
    _put(INDEX_NAMESPACE, index_key, summary)
    task['_durable_research_record'] = record
    return record


def _project(record, *, user_id):
    from .runtime import _research_runtime
    value = deepcopy(record)
    live = _research_runtime.get(value['task_id'])
    if live and int(live.get('user_id') or 0) == int(user_id):
        value['status'] = live['status']
    elif value['status'] in ACTIVE:
        value['status'] = 'interrupted'
    value['resumable'] = live is None and value['status'] in ('interrupted', 'error', 'aborted')
    value.pop('checkpoint', None)
    value.pop('request', None)
    return value


def load_record(task_id, *, user_id, internal=False):
    row = _storage().query('record.get', {
        'namespace': NAMESPACE, 'key': _key(task_id, user_id)})
    if not row:
        return None
    record = row['value']
    return deepcopy(record) if internal else _project(record, user_id=user_id)


def list_records(*, user_id, limit=50):
    uid = require_user_id(user_id, context='research records owner')
    rows = _storage().query('record.list', {
        'namespace': INDEX_NAMESPACE, 'prefix': f'{uid}:',
        'limit': max(1, min(200, int(limit))),
    })
    return sorted((_project(row['value'], user_id=uid) for row in rows),
                  key=lambda r: r['updated_at'], reverse=True)


def resume_record(task_id, *, user_id):
    """Serialize same-process admission before handing execution to TaskRuntime."""
    with _resume_lock:
        return _resume_record(task_id, user_id=user_id)


def _resume_record(task_id, *, user_id):
    """Explicit owner action only; never infer process restart auto-execution."""
    from .runtime import _research_runtime, _new_research_task
    from .engine import research_root, run_research_task
    record = load_record(task_id, user_id=user_id, internal=True)
    if record is None:
        raise ValueError('research record not found')
    live = _research_runtime.get(task_id)
    if live is not None:
        if int(live.get('user_id') or 0) != int(user_id):
            raise ValueError('research record not found')
        if live['status'] in ACTIVE:
            return {'task_id': task_id, 'deduped': True}
        # A terminal TaskRuntime identity cannot be revived. The caller can
        # start a new run; registry-loss recovery is the supported resume path.
        raise ValueError('task is still registered; start a new research run')
    if record['status'] not in (*ACTIVE, 'error', 'aborted'):
        raise ValueError('completed research cannot be resumed')
    workdir = os.path.join(research_root(), 'jobs', task_id)
    os.makedirs(workdir, exist_ok=True)
    if record.get('checkpoint'):
        from lib.json_store import write_json_atomic
        write_json_atomic(os.path.join(workdir, 'pipeline_state.json'), record['checkpoint'])
    task = _new_research_task(task_id, direction=record['direction'],
                             lang=record['lang'], workdir=workdir,
                             user_id=user_id, **record.get('request', {}))
    task['_research_record_enabled'] = True
    task['_durable_research_record'] = record
    task['research_progress'] = record.get('research_progress')
    try:
        persist_record(task)
        _research_runtime.spawn(task_id, run_research_task, task)
    except Exception as exc:
        _research_runtime.finish(task_id, error=exc, error_context='research:resume')
        raise
    return {'task_id': task_id, 'deduped': False}
