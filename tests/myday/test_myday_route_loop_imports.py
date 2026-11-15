"""My Day requests never import a module on the serving event loop.

Measured on production 2026-11-09 12:39:53: the first GET /api/v4/myday/goals
imported lib.myday.service inside the request handler. The pinned import
source's os.stat on DolphinFS took about 6 s, so the API host's event loop
stalled for 5.9 s and requests already in flight waited 16-22 s. A first
import costs that once per process, on whichever request comes first.

The scenario runs in a fresh process that first loads what server boot loads,
then records every module import attempted while an event loop runs on the
importing thread (work moved to a worker thread may import).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

_SCENARIO = r'''
import asyncio
import json
import sys
from types import SimpleNamespace

import server  # noqa: F401  the boot import set
from quart import Quart, request
import lib.llm_dispatch.api as dispatch_api
import lib.storage as storage
import routes.api_v1.auth as auth
from routes.api_v1 import myday as route
from tests.support.myday_records import Records

records = Records()
storage.get_storage_client = lambda **kwargs: records
auth.current_auth = lambda: SimpleNamespace(is_authenticated=True)
route.request_user_id = lambda: 7


def no_model(*args, **kwargs):
    raise RuntimeError('no model in this scenario')
dispatch_api.dispatch_chat = no_model


class LoopImports:
    def __init__(self):
        self.names = []

    def find_spec(self, name, path=None, target=None):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        self.names.append(name)
        return None


app = Quart(__name__)
app.register_blueprint(route.api_v1_myday_bp)


@app.route('/warm', methods=['GET', 'POST'])
async def warm():
    if request.method == 'POST':
        await request.get_json()
    return 'ok'


async def payload(response):
    body = await response.get_json()
    return body.get('data', body)  # the bare v1 blueprint answers without the v4 data wrapper


async def scenario():
    client = app.test_client()
    await client.get('/warm')  # the test client and Quart load their own parts first
    await client.post('/warm', json={'warm': True})
    recorder = LoopImports()
    sys.meta_path.insert(0, recorder)
    statuses = []
    try:
        goals = '/api/v1/myday/goals'
        statuses.append((await client.get(goals)).status_code)
        created = await client.post(goals, json={'text': 'Plan the offsite'})
        statuses.append(created.status_code)
        goal = f"{goals}/{(await payload(created))['goal']['id']}"
        added = await client.post(goal + '/items', json={'text': 'Book the venue'})
        statuses.append(added.status_code)
        item = (await payload(added))['goal']['items'][0]['id']
        statuses.append((await client.patch(f'{goal}/items/{item}', json={'status': 'completed'})).status_code)
        statuses.append((await client.patch(goals + '/g-0000000000000000', json={'text': 'x'})).status_code)
        statuses.append((await client.post(goal + '/suggestions', json={'decision': 'accept'})).status_code)
        statuses.append((await client.post(goal + '/decompose')).status_code)
        statuses.append((await client.get('/api/v1/myday/requests')).status_code)
        statuses.append((await client.delete(f'{goal}/items/{item}')).status_code)
        statuses.append((await client.patch(goal, json={'state': 'archived'})).status_code)
    finally:
        sys.meta_path.remove(recorder)
    return statuses, recorder.names


statuses, names = asyncio.run(scenario())
print(json.dumps({'statuses': statuses, 'loopImports': sorted(set(names))}))
'''


def test_my_day_requests_never_import_on_the_serving_loop():
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    proc = subprocess.run([sys.executable, '-c', _SCENARIO], cwd=ROOT, env=env, timeout=240,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # read, create, add, check off, unknown goal, accept, failed model, requests, remove, archive
    assert result['statuses'] == [200, 200, 200, 200, 404, 200, 502, 200, 200, 200], result
    assert result['loopImports'] == [], result
