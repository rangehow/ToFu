"""Measure owned task/project inventory aggregation through production read code.

Run this same file against frozen --repository trees in alternating fresh
processes. Runtime/catalog/history ports are synthetic in-memory fixtures;
authority RPCs, validation, providers, HTTP encoding and cold imports are outside
the clocks. Counts describe repository method calls, not storage transactions.
The existing request page size is 50; the fixture supplies 80% owned tasks and
64 conversations with explicit creation provenance. --baseline refuses changed
fixtures or complete response digests before reporting any timing comparison.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import inspect
import json
from pathlib import Path
import random
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repository', type=Path, required=True)
parser.add_argument('--repeats', type=int, default=9)
parser.add_argument('--baseline', type=Path)
args = parser.parse_args()
if args.repeats < 2:
    parser.error('--repeats must include at least two measured observations')
sys.path.insert(0, str(args.repository.resolve()))
from quart import Quart
from routes.api_v1 import tasks
from lib.conversations import coordinator_conversations as conversations
from lib.project_dispatch.history import PAGE_ROWS


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def measure(call):
    expected = call()
    wall, cpu = [], []
    for _ in range(args.repeats):
        start, cpu_start = time.perf_counter(), time.thread_time()
        result = call()
        cpu.append((time.thread_time() - cpu_start) * 1000)
        wall.append((time.perf_counter() - start) * 1000)
        assert result == expected
    return {'sha256': digest(expected), 'wallMs': wall, 'cpuMs': cpu,
            'wallMedianMs': statistics.median(wall),
            'cpuMedianMs': statistics.median(cpu)}


async def task_probe(size):
    rng = random.Random(217)
    rows = [{'id': f'task-{n}', 'kind': 'motion-video', 'status': 'done',
             'created_at': rng.randrange(size // 4 + 1), 'finished_at': size,
             '_userId': 7 if n % 5 else 9,
             'meta': {'direction': 'scene', 'lang': 'en', 'topic': f'Film {n}'},
             'artifact_quality': {'degraded': True, 'reason': 'fixture'} if n % 11 == 0 else None}
            for n in range(size)]
    calls = Counter()
    def snapshot_owned(*, user_id):
        calls['snapshots'] += 1
        assert user_id == 7
        return [row for row in rows if row['_userId'] == user_id]
    runtime = SimpleNamespace(snapshot_owned=snapshot_owned)
    app = Quart(__name__)
    entry = inspect.unwrap(tasks.list_tasks)
    original = tasks.project_work_unit
    def project(*a, **kw):
        calls['workUnitProjections'] += 1
        return original(*a, **kw)
    with patch.object(tasks, '_registries', lambda: {'motion-video': runtime}), \
            patch.object(tasks, 'request_user_id', lambda: 7), \
            patch.object(tasks, 'api_ok', lambda value: value):
        async with app.test_request_context('/api/v4/tasks?limit=50'):
            result = measure(entry)
            calls.clear()
            with patch.object(tasks, 'project_work_unit', project):
                payload = entry()
    expected = sorted([row for row in rows if row['_userId'] == 7],
                      key=lambda row: row['created_at'], reverse=True)[:50]
    assert [row['id'] for row in payload['tasks']] == [row['id'] for row in expected]
    assert payload['total'] == size * 4 // 5
    return {'rows': size, 'fixtureSha256': digest(rows),
            'returned': len(payload['tasks']), 'total': payload['total'],
            'calls': {name: calls[name] for name in ('snapshots', 'workUnitProjections')}, **result}


def conversation_probe(size):
    rows = [SimpleNamespace(metadata={'id': f'worker-{n}', 'title': f'Worker {n}',
            'updated_at': 30, 'msg_count': 2, 'settings': {'projectPath': '/project',
            '_coordinatorSource': 'coordinator-explicit'}}) for n in range(64)]
    assignments = [{'request': {'recipientId': f'worker-{n % 64}',
                               'coordinatorId': 'coordinator-history'}} for n in range(size)]
    calls = Counter()
    def catalog(**kw):
        calls['catalogPages'] += 1
        assert kw['user_id'] == 7 and kw['limit'] == 64
        return SimpleNamespace(items=rows, has_more=False)
    class Dispatch:
        def __init__(self, owner, project):
            assert (owner, project) == (7, '/project')
        def history_page(self, cursor=None):
            calls['historyPages'] += 1
            offset = cursor or 0
            next_offset = offset + PAGE_ROWS
            return {'rows': assignments[offset:next_offset],
                    'olderCursor': next_offset if next_offset < size else None}
    original = conversations.project_catalog_origins
    def origins(owner, project):
        return original(owner, project, repository_factory=Dispatch)
    call = lambda: conversations.conversation_page(7, '/project',
                          repository=SimpleNamespace(list_catalog_page=catalog))
    with patch.object(conversations, 'project_catalog_origins', origins):
        result = measure(call)
        calls.clear()
        payload = call()
    assert len(payload['conversations']) == 64
    assert {row['coordinatorId'] for row in payload['conversations']} == {'coordinator-explicit'}
    return {'historyRows': size, 'historyPageRows': PAGE_ROWS,
            'fixtureSha256': digest([[row.metadata for row in rows], assignments]),
            'calls': {name: calls[name] for name in ('catalogPages', 'historyPages')}, **result}


result = {'scope': 'Warm aggregation CPU; authority/provider/HTTP encoding excluded',
          'tasks': [asyncio.run(task_probe(size)) for size in (10, 1000, 10000)],
          'conversations': [conversation_probe(size) for size in (2000, 10000)],
          'sources': {name: hashlib.sha256((args.repository / name).read_bytes()).hexdigest()
                      for name in ('routes/api_v1/tasks.py', 'lib/production/work_unit.py',
                                   'lib/conversations/coordinator_conversations.py',
                                   'contracts/project_dispatch_v1.schema.json')}}
if args.baseline:
    before = json.loads(args.baseline.read_text())
    for kind in ('tasks', 'conversations'):
        assert len(before[kind]) == len(result[kind]), f'{kind} fixture population changed'
        for old, new in zip(before[kind], result[kind]):
            assert old['fixtureSha256'] == new['fixtureSha256'], f'{kind} fixture changed'
            assert old['sha256'] == new['sha256'], f'{kind} complete response changed'
print(json.dumps(result, indent=2))
