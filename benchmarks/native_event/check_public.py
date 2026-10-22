"""Actual AgentExecution and Headless replay/SSE routes over native retention.

Quart's in-process ASGI client runs auth, public projection and SSE framing.
No real TCP, durability, model calls or production retention default changes.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from runtime import create, normalized
from check import artifact
from benchmarks.http_runtime import ComputeFixture, TOKEN
from tofu_agent.models import AgentRequest
from tofu_agent.runtime import AgentExecution
from tofu_agent.server import HeadlessServerConfig, create_app

ROOT = Path(__file__).resolve().parents[2]


def normalize_page(page):
    page = normalized(page)
    page.pop('task_id', None)
    return page


def stream_events(raw):
    events = []
    for line in raw.decode().splitlines():
        if line.startswith('data: '):
            events.append(json.loads(line[6:]))
    return normalized({'events': events})['events']


async def observe(variant, count):
    kernel, task = create(variant, count)
    kernel.mark_running(task['id'])
    for index in range(70):
        kernel.append_event(task['id'], {'type': 'delta', 'content': f'event {index} 中文'},
                            deliver_push=False)
    kernel.finish(task['id'], result='offline fixture')
    port = ComputeFixture()
    request = AgentRequest(messages=[{'role': 'user', 'content': 'offline fixture'}])
    execution = AgentExecution(port, task, request, model='fixture', public_provider_id='fixture')
    port.executions[execution.task_id] = execution
    app = create_app(runtime=port, model_routing_setup=object(),
                     config=HeadlessServerConfig(token=TOKEN, auth_mode='token', setup_enabled=False))
    client = app.test_client()
    endpoint = f'/api/v1/tasks/{execution.task_id}'
    assert (await client.get(endpoint+'/events')).status_code == 401
    headers = {'Authorization': f'Bearer {TOKEN}'}
    base = task['_eventBaseSeq']; next_cursor = task['_eventNextSeq']
    results = []
    try:
        for cursor in (0, base, next_cursor, next_cursor+10):
            expected = normalize_page(execution.event_page(cursor=cursor))
            assert not execution._async_waiters
            page = await client.get(endpoint+'/events?cursor='+str(cursor), headers=headers)
            assert page.status_code == 200
            actual = normalize_page(await page.get_json())
            assert actual == expected
            assert actual['next_cursor'] == next_cursor
            if cursor == 0:
                assert actual['cursor']['reset'] == (base > 0)
                assert actual['events'] and actual['events'][-1]['type'] == 'done'
            stream = await client.get(endpoint+'/stream?cursor='+str(cursor), headers=headers)
            assert stream.status_code == 200
            raw = await asyncio.wait_for(stream.get_data(), timeout=5)
            events = stream_events(raw)
            assert events and events[-1]['type'] == 'done'
            assert not execution._async_waiters, 'finished stream left an async observer'
            if cursor < next_cursor:
                assert events == actual['events']
            results.append({'cursor': cursor, 'page': actual, 'stream': events})
    finally:
        port.executions.clear()
        kernel.cleanup_stale(max_age=0)
        assert kernel.task_count == 0
    return results


async def check():
    results = []
    for count in (1, 64):
        expected = await observe('reference', count)
        candidate = await observe('native', count)
        assert candidate == expected, 'public native replay/SSE output or cursor differs'
        results.append({'maximum_events': count, 'cursors': len(expected),
                        'exact_public_page_and_sse_parity': True,
                        'unauthenticated_rejected': True, 'terminal_observers_released': True})
    for variant in ('reference', 'native'):
        kernel, task = create(variant, 64)
        kernel.mark_running(task['id'])
        port = ComputeFixture()
        execution = AgentExecution(port, task,
            AgentRequest(messages=[{'role': 'user', 'content': 'offline pending observer'}]),
            model='fixture', public_provider_id='fixture')
        stream = execution.events_async(cursor=0)
        waiting = asyncio.create_task(anext(stream))
        try:
            deadline = asyncio.get_running_loop().time()+2
            while not execution._async_waiters:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0)
            assert not waiting.done()
            waiting.cancel()
            try:
                await waiting
            except asyncio.CancelledError:
                pass  # Explicitly cancelled this owned pending observer.
            else:
                raise AssertionError('cancelled observer returned an event')
            await stream.aclose()
            assert not execution._async_waiters
            assert task['status'] == 'running' and not task['abort_event'].is_set()
            kernel.append_event(task['id'], {'type': 'delta', 'content': 'after disconnect'},
                                deliver_push=False)
            resumed = execution.events_async(cursor=0)
            try:
                event = await asyncio.wait_for(anext(resumed), timeout=2)
                assert event['seq'] == 0 and event['content'] == 'after disconnect'
            finally:
                await resumed.aclose()
            assert not execution._async_waiters
        finally:
            waiting.cancel()
            try:
                await waiting
            except asyncio.CancelledError:
                pass  # Join this cancelled observer before closing its generator.
            await stream.aclose()
            kernel.finish(task['id'], result='offline pending observer')
            kernel.cleanup_stale(max_age=0)
            assert kernel.task_count == 0
        results.append({'variant': variant, 'pending_observer_cancelled_without_task_abort': True,
                        'reconnect_after_new_event': True, 'async_waiters_released': True})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    names = ('benchmarks/native_event/check_public.py', 'benchmarks/native_event/runtime.py',
             'lib/agent_core/task_runtime.py', 'lib/task_replay/task_replay.py',
             'tofu_agent/runtime.py', 'tofu_agent/server.py')
    report = {'scope': __doc__, 'whole_system_verdict': 'incomplete',
              'python': sys.version, 'native_library_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
              'sources': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names},
              'checks': asyncio.run(check()),
              'limitations': ['in-process ASGI client; no TCP/SSE backpressure or transport timing',
                              'native packaging/adoption and durable recovery unverified',
                              'public reader may use full iterable snapshot; no tail performance claim']}
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print('actual AgentExecution and Headless auth/replay/SSE parity for evicted windows and terminal cursors; observers released')


if __name__ == '__main__':
    main()
