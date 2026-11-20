"""A recovered Swarm's logical round never grants authority to a retry."""
from types import SimpleNamespace

import pytest
import requests

from tests.goals.test_goal_invocation_integration import runtime  # noqa: F401
from tests.llm_dispatch.test_dispatch_stream import _FakeDispatcher, _make_slot
__all__ = ['runtime']

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('status, picks', [(None, 1), (500, 1), (429, 2), ('desktop', 1)],
                         ids=['first-send', 'same-slot-retry', 'rotated-slot-retry', 'unfenced-desktop'])
def test_recovery_checks_authority_for_each_physical_send(runtime, monkeypatch, status, picks):
    from lib.llm.llm_errors import ModelAdmissionRefused
    from lib.llm.request import current_physical_model_dispatch_guard
    from lib.llm_dispatch import api
    from lib.storage.errors import StorageError
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.manager import _stream as stream_manager

    events = []
    refused = []
    revoked = status is None
    conflict = StorageError('database_conflict', 'fixture recovery authority revoked')
    slots = [_make_slot(model='fixture-model', key=key) for key in ('first', 'second')]
    for slot in slots:
        slot.base_url = 'https://fixture.invalid/v1'
    dispatcher = _FakeDispatcher(slots)
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr('lib.desktop.egress.route_request',
        lambda *a, **kw: 'fixture-desktop' if status == 'desktop' else 'direct')
    monkeypatch.setattr('lib.desktop.egress.open_stream',
        lambda *a, **kw: pytest.fail('unfenced desktop command was enqueued'))
    monkeypatch.setattr('lib.llm.stream.abortable_sleep', lambda *_: None)

    def send(url, **kwargs):
        nonlocal revoked
        assert url.startswith('https://fixture.invalid/')
        assert not revoked, 'provider called after recovery authority was revoked'
        events.append('provider_send')
        revoked = True
        response = requests.Response()
        response.status_code = status
        response._content = b'{"error":{"message":"fixture retryable provider error"}}'
        response._content_consumed = True
        return response

    monkeypatch.setattr('lib.llm.stream.get_sync_session', lambda: SimpleNamespace(post=send))

    def dispatch(*args, **kwargs):
        try:
            return api.dispatch_stream(*args, **kwargs)
        except ModelAdmissionRefused as error:
            refused.append(error)
            raise

    monkeypatch.setattr(stream_manager, 'dispatch_stream', dispatch)
    agent = SubAgent(SubTaskSpec(role='general', objective='Check retry authority'),
                     parent_task=runtime.parent, all_tools=[], model='fixture-model')

    def checkpoint(*, final=False):
        if not final and agent.result.checkpoint_phase == 'model_in_flight':
            events.append('intent_ack')
        return True

    def guard():
        events.append('authority_check')
        if revoked:
            raise conflict

    monkeypatch.setattr(agent, '_checkpoint', checkpoint)
    agent._recovery_dispatch_guard = guard
    result = agent.run()

    if status == 'desktop':
        assert events == ['intent_ack']
        assert len(refused) == 1 and 'desktop relay is unsupported' in str(refused[0])
    else:
        expected = ['intent_ack', 'authority_check']
        if status is not None:
            expected += ['provider_send', 'authority_check']
        assert events == expected
        assert len(refused) == 1 and refused[0].__cause__ is conflict
    assert dispatcher.picks == picks
    assert all(slot.inflight == 0 for slot in slots)
    assert result.status == 'failed'
    assert current_physical_model_dispatch_guard() is None


def test_managed_sync_route_rechecks_before_safe_failover(monkeypatch):
    from lib.http_transport.outbound import RequestNetworkPolicy, send_routed_request, wire_send_authority
    from lib.llm.llm_errors import ModelAdmissionRefused
    from lib.network_routes import Route

    request = requests.Request('POST', 'https://fixture.invalid/v1', data=b'sealed').prepare()
    routes = [Route('first', 'first', 'direct'), Route('second', 'second', 'direct')]
    monkeypatch.setattr('lib.proxy.network_route_candidates', lambda *a, **kw: routes)
    monkeypatch.setattr('lib.proxy.report_network_route', lambda *a, **kw: None)
    sends = []

    def send(*a, **kw):
        sends.append('first')
        raise requests.ConnectTimeout('no bytes sent')

    def guard():
        if sends:
            raise ModelAdmissionRefused('revoked during failover')

    with wire_send_authority(guard), pytest.raises(ModelAdmissionRefused, match='during failover'):
        send_routed_request(send, request, policy=RequestNetworkPolicy())
    assert sends == ['first']


def test_managed_async_route_rechecks_before_safe_failover(monkeypatch):
    import asyncio
    import httpx
    from lib.http_transport.outbound_async import RoutingAsyncTransport
    from lib.llm.llm_errors import ModelAdmissionRefused
    from lib.network_routes import Route

    routes = [Route('first', 'first', 'direct'), Route('second', 'second', 'direct')]
    sends, releases = [], []

    async def candidates(*a, **kw):
        return routes

    async def send(*a, **kw):
        sends.append('first')
        raise httpx.ConnectError('no bytes sent')

    async def acquire(*a, **kw):
        return len(releases), SimpleNamespace(handle_async_request=send)

    async def release(key):
        releases.append(key)

    def guard():
        if sends:
            raise ModelAdmissionRefused('revoked during failover')

    async def run():
        transport = RoutingAsyncTransport()
        monkeypatch.setattr(transport, '_acquire', acquire)
        monkeypatch.setattr(transport, '_release', release)
        with pytest.raises(ModelAdmissionRefused, match='during failover'):
            await transport.handle_async_request(httpx.Request('POST', 'https://fixture.invalid/v1',
                extensions={'tofu_wire_send_authority': guard}))

    monkeypatch.setattr('lib.proxy.async_network_route_candidates', candidates)
    monkeypatch.setattr('lib.proxy.report_network_route', lambda *a, **kw: None)
    asyncio.run(run())
    assert sends == ['first'] and releases == [0, 1]
