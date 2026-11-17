"""A remote observer controls the existing owner; delivery is not settlement."""
import asyncio
import json
import struct
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def test_control_round_trip_checks_incarnation_owner_and_exact_call():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call, CommandControlUnavailable
    calls = []
    def apply(method, task_id, *, user_id, deadline_unix_ms, **params):
        assert deadline_unix_ms > 0
        calls.append((method, task_id, user_id, params))
        return {'backgrounded': False, 'reason': 'handoff_pending'}
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        try:
            result = await asyncio.to_thread(call, endpoint, 'background', 'task', user_id=41,
                params={'round_num': 2, 'tool_call_id': 'exact'})
            assert result == {'backgrounded': False, 'reason': 'handoff_pending'}
            for changed in ({'token': 'wrong'}, {'serviceId': 'old'}):
                with pytest.raises(CommandControlUnavailable):
                    await asyncio.to_thread(call, {**endpoint, **changed}, 'interrupt', 'task', user_id=41)
            assert calls == [('background', 'task', 41, {'round_num': 2, 'tool_call_id': 'exact'})]
        finally:
            await server.close()
        assert not server.active_count
    asyncio.run(run())


@pytest.mark.parametrize('changed', [
    {'ownerId': True}, {'ownerId': 0}, {'method': 'kill'}, {'taskId': ''},
    {'deadlineUnixMs': 0}, {'params': {'unexpected': True}}, {'extra': True},
    {'method': 'swarm_abort', 'taskId': ''}, {'method': 'swarm_agent_stop'},
    {'method': 'swarm_agent_stop', 'params': {'agent_id': ''}},
    {'method': 'swarm_status', 'taskId': ''}, {'method': 'swarm_status', 'params': {'agent_id': 'a'}},
    {'method': 'background_activity'},  # An owner method names no task.
    {'method': 'background_activity', 'taskId': '', 'params': {'agent_id': 'a'}},
    {'method': 'task_view', 'params': {'agent_id': 'a'}}, {'method': 'task_abort', 'taskId': ''},
    {'method': 'guidance_answer'}, {'method': 'guidance_answer', 'params': {'response': ' '}},
    {'method': 'guidance_answer', 'params': {'response': 3}},
    {'method': 'guidance_answer', 'params': {'response': 'x' * 8001}},
    {'method': 'guidance_answer', 'params': {'response': 'ok', 'agent_id': 'a'}},
])
def test_invalid_or_expired_wire_request_never_reaches_the_owner(changed):
    from lib.tasks_pkg.command_control_transport import CommandControlServer
    async def run():
        server = CommandControlServer('incarnation', lambda *args, **kwargs: pytest.fail('invalid control entered'))
        endpoint = await server.start()
        reader, writer = await asyncio.open_connection(endpoint['host'], endpoint['port'])
        try:
            request = dict(format=endpoint['format'], serviceId=endpoint['serviceId'],
                           token=endpoint['token'], requestId='exact', ownerId=41,
                           method='interrupt', taskId='task', params={},
                           deadlineUnixMs=int(time.time() * 1000) + 2000)
            request.update(changed)
            encoded = json.dumps(request).encode()
            writer.write(struct.pack('!I', len(encoded)) + encoded)
            await writer.drain()
            length = struct.unpack('!I', await reader.readexactly(4))[0]
            response = json.loads(await reader.readexactly(length))
            assert response['result'] is None
            assert response['error'] in {'control_refused', 'control_unavailable'}
        finally:
            writer.close()
            await writer.wait_closed()
            await server.close()
    asyncio.run(run())


def test_listener_shutdown_joins_entered_callback_and_closes_admission():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call, CommandControlUnavailable
    entered, release = threading.Event(), threading.Event()
    def apply(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'interrupted': True, 'pid': 123}
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        request = asyncio.create_task(asyncio.to_thread(call, endpoint, 'interrupt', 'task', user_id=41))
        assert await asyncio.to_thread(entered.wait, 5)
        close = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        assert not close.done()
        try:
            with pytest.raises(CommandControlUnavailable):
                await asyncio.to_thread(call, endpoint, 'interrupt', 'task', user_id=41)
        finally:
            release.set()
        assert await request == {'interrupted': True, 'pid': 123}
        await close
        assert server.active_count == 0
    asyncio.run(run())


@pytest.mark.parametrize('method,task_id,result', [
    ('interrupt', 'task', {}), ('interrupt', 'task', {'interrupted': 'yes'}),
    ('interrupt', 'task', {'backgrounded': True}),
    ('swarm_abort', 'swarm', {'aborted': 'yes'}), ('swarm_abort', 'swarm', {'stopped': True}),
    ('swarm_agent_stop', 'run', {'stopped': True, 'status': 3}),
    ('swarm_agent_stop', 'run', {'stopped': False, 'pid': 9}),
    # A status is a resident session: known, with typed verdicts and agent rows.
    ('swarm_status', 'conv', {}), ('swarm_status', 'conv', {'active': True, 'known': False}),
    ('swarm_status', 'conv', {'active': None, 'known': True}),
    ('swarm_status', 'conv', {'active': True, 'known': True, 'terminated': 'no'}),
    ('swarm_status', 'conv', {'active': True, 'known': True, 'agents': {'a': {}}}),
    ('swarm_status', 'conv', {'active': True, 'known': True, 'agents': ['a']}),
    # An owner view is never "no such task", and each list holds rows.
    ('background_activity', '', None), ('background_activity', '', {'swarms': []}),
    ('background_activity', '', {'swarms': [1], 'backgroundCommands': []}),
    # Process metrics are never "no such task"; every series is complete and typed.
    ('storage_rpc_metrics', '', None), ('storage_rpc_metrics', '', {'swarms': []}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {}, 'buckets': [1.0], 'counts': [1]}]}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {'kind': 3}, 'buckets': [1.0],
                                                 'counts': [1], 'sum': 0.5, 'count': 1}]}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {}, 'buckets': [1.0, 2.0],
                                                 'counts': [1], 'sum': 0.5, 'count': 1}]}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {}, 'buckets': [1.0],
                                                 'counts': [-1], 'sum': 0.5, 'count': 1}]}),
    # A work-unit view is the public task plus its block; never a bare or partial task.
    ('task_view', 'task', {'task': 'running', 'workUnit': None}), ('task_view', 'task', {'task': {}}),
    ('task_view', 'task', {'task': {}, 'workUnit': 3}),
    ('task_abort', 'task', {'aborting': 'yes'}), ('task_abort', 'task', {'aborting': True, 'status': 3}),
    ('guidance_answer', 'hg', {'resolved': 1}), ('guidance_answer', 'hg', {'resolved': True, 'reason': 'x'}),
    # An MCP reconcile is queued or refused; never absent, never an activity view.
    ('mcp_reconcile', '', None), ('mcp_reconcile', '', {'scheduled': 'yes'}),
    ('mcp_reconcile', '', {'swarms': [], 'backgroundCommands': []}),
])
def test_remote_response_requires_the_requested_operation_result(method, task_id, result):
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call, CommandControlUnavailable
    params = ({'agent_id': 'a'} if method == 'swarm_agent_stop'
              else {'response': 'ok'} if method == 'guidance_answer'
              else {'server': 'chaos', 'action': 'connect'} if method == 'mcp_reconcile' else {})
    async def run():
        server = CommandControlServer('incarnation', lambda *args, **kwargs: result)
        endpoint = await server.start()
        try:
            with pytest.raises(CommandControlUnavailable):
                await asyncio.to_thread(call, endpoint, method, task_id, user_id=41, params=params)
        finally:
            await server.close()
    asyncio.run(run())


def test_swarm_controls_and_owner_view_round_trip_exact_arguments():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    calls = []
    results = {'swarm_abort': {'aborted': True},
               'swarm_agent_stop': {'stopped': False, 'reason': 'not_running', 'status': 'pending'},
               'background_activity': {'swarms': [{'swarm_id': 'run'}], 'backgroundCommands': []}}
    def apply(method, task_id, *, user_id, deadline_unix_ms, **params):
        calls.append((method, task_id, user_id, params))
        return results[method]
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        try:
            for method, task_id, params in (('swarm_abort', 'swarm', {}),
                                            ('swarm_agent_stop', 'run', {'agent_id': 'a'}),
                                            ('background_activity', '', {})):
                assert await asyncio.to_thread(call, endpoint, method, task_id, user_id=41,
                                               params=params) == results[method]
        finally:
            await server.close()
    asyncio.run(run())
    assert calls == [('swarm_abort', 'swarm', 41, {}),
                     ('swarm_agent_stop', 'run', 41, {'agent_id': 'a'}),
                     ('background_activity', '', 41, {})]


def test_swarm_status_crosses_as_the_resident_session_or_its_absence():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    live = {'active': True, 'known': True, 'terminated': False, 'awaitingContinue': False,
            'task_id': 'conv', 'agents': [{'id': 'a', 'status': 'running', 'toolCalls': []}]}
    calls = []
    def apply(method, task_id, *, user_id, deadline_unix_ms, **params):
        calls.append((method, task_id, user_id, params))
        return live if task_id == 'conv' else None
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        try:
            return [await asyncio.to_thread(call, endpoint, 'swarm_status', task_id, user_id=41)
                    for task_id in ('conv', 'other')]
        finally:
            await server.close()
    assert asyncio.run(run()) == [live, None]
    assert calls == [('swarm_status', 'conv', 41, {}), ('swarm_status', 'other', 41, {})]


def test_work_unit_controls_round_trip_exact_arguments():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    calls = []
    results = {'task_view': {'task': {'id': 'slides_one'}, 'workUnit': None},
               'task_abort': {'aborting': False, 'status': 'done'},
               'guidance_answer': {'resolved': True}}
    def apply(method, task_id, *, user_id, deadline_unix_ms, **params):
        calls.append((method, task_id, user_id, params))
        return results[method]
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        try:
            for method, task_id, params in (('task_view', 'slides_one', {}),
                                            ('task_abort', 'slides_one', {}),
                                            ('guidance_answer', 'hg_slides_one_outline',
                                             {'response': '改成 8 页'})):
                assert await asyncio.to_thread(call, endpoint, method, task_id, user_id=41,
                                               params=params) == results[method]
        finally:
            await server.close()
    asyncio.run(run())
    assert calls == [('task_view', 'slides_one', 41, {}), ('task_abort', 'slides_one', 41, {}),
                     ('guidance_answer', 'hg_slides_one_outline', 41, {'response': '改成 8 页'})]


def test_execution_storage_histogram_round_trips_from_the_owning_process():
    """The real control owner answers with this process's storage RPC series."""
    from lib import observability
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    from lib.tasks_pkg.command_controls import apply_command_control
    store = observability._STORE
    with store.lock:  # shared with neighbors on this xdist worker
        saved = dict(store.histograms)
        store.histograms.clear()
    observability.record_storage_rpc('query', 'turn.get', 0.003)
    observability.record_storage_rpc('query', 'turn.get', 0.2)
    observability.record_storage_rpc('command', 'turn.event.record', 0.04, outcome='error')

    async def run():
        server = CommandControlServer('incarnation', apply_command_control)
        endpoint = await server.start()
        try:
            return await asyncio.to_thread(call, endpoint, 'storage_rpc_metrics', '',
                                           user_id=41, params={})
        finally:
            await server.close()
    try:
        result = asyncio.run(run())
    finally:
        with store.lock:
            store.histograms.clear()
            store.histograms.update(saved)
    rows = {(row['labels']['kind'], row['labels']['operation'], row['labels']['outcome']): row
            for row in result['histograms']}
    assert set(rows) == {('query', 'turn.get', 'success'), ('command', 'turn.event.record', 'error')}
    read = rows[('query', 'turn.get', 'success')]
    assert read['count'] == 2 and read['sum'] == pytest.approx(0.203)
    assert dict(zip(read['buckets'], read['counts']))[0.005] == 1
    assert read['counts'][-1] == 2 and read['counts'] == sorted(read['counts'])


def test_repeated_close_cancellation_still_joins_the_callback():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def apply(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        exited.set()
        return {'interrupted': True, 'pid': 123}
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        request = asyncio.create_task(asyncio.to_thread(call, endpoint, 'interrupt', 'task', user_id=41))
        assert await asyncio.to_thread(entered.wait, 5)
        close = asyncio.create_task(server.close())
        try:
            for _ in range(3):
                await asyncio.sleep(0)
                close.cancel()
            await asyncio.sleep(0)
            assert not close.done()
            assert not exited.is_set()
        finally:
            release.set()
            await request
            with pytest.raises(asyncio.CancelledError):
                await close
        assert exited.is_set()
        assert server.active_count == 0
    asyncio.run(run())


def test_mcp_reconcile_round_trips_its_scheduling_verdict():
    """Found by the chaos suite: the owner-method branch accepted only the
    activity shape, so every real reconcile read as unacknowledged."""
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    calls = []

    def apply(method, task_id, *, user_id, deadline_unix_ms, **params):
        calls.append((method, task_id, user_id, params))
        return {'scheduled': True}

    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        try:
            return await asyncio.to_thread(call, endpoint, 'mcp_reconcile', '', user_id=41,
                                           params={'server': 'chaos', 'action': 'tools'})
        finally:
            await server.close()
    assert asyncio.run(run()) == {'scheduled': True}
    assert calls == [('mcp_reconcile', '', 41, {'server': 'chaos', 'action': 'tools'})]
