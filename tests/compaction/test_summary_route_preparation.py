"""Recover an unchanged summary only from a proven unsent route failure."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
from lib.model_routing import ModelRoutingError
from tests.compaction.test_auxiliary_summary_integration import native as native
from tests.compaction.test_global_auxiliary_candidates import missing_claude as missing_claude
from tests.compaction.test_summary_credential_resilience import _complete, resilient as resilient

pytestmark = pytest.mark.unit
_PARENT = 'model-route:41:task-a'


@pytest.fixture(autouse=True)
def real_dispatch(native, monkeypatch):
    from lib import llm_dispatch
    from lib.llm_dispatch import _api_stream

    monkeypatch.setattr(llm_dispatch, 'dispatch_stream', _api_stream.dispatch_stream)
    monkeypatch.setattr(_api_stream, 'get_dispatcher', lambda: native.pool)
    monkeypatch.setattr('lib.llm_dispatch.cache_settle.settle_before_send', lambda *a, **k: None)
    monkeypatch.setattr(native.pool, 'reserve_shared_contention_probe',
                        lambda *a, **k: SimpleNamespace(delay_s=0.0, admitted=True))


@pytest.mark.parametrize('failure', ('missing_secret', 'route_changed', 'unknown', 'quoted'))
def test_summary_replans_only_after_qualified_route_is_refused_before_send(resilient, monkeypatch, failure):
    """Real owner compiler and private mint; no real provider or credential."""
    from lib import llm, model_routing
    from lib.tasks_pkg.compaction import _auxiliary_route

    # Give the API an established, cheaper paid route. Codex is the next
    # authorized candidate. Keep the parent slot unchanged throughout.
    resilient.state.document['credentials'][1]['kind'] = 'api_key'
    resilient.state.document['offerings'][1]['actual_pricing'].update(input=.1, output=.5)
    original = deepcopy(resilient.source)
    calls = []
    minted = []
    mint = _auxiliary_route.mint_auxiliary_route
    resolve = model_routing.ModelRoutingRepository.resolve_secret

    def resolve_secret(repo, boundary, ref):
        if ref == 'fixture-api' and failure in {'missing_secret', 'quoted'}:
            raise ModelRoutingError('sealed secret missing', kind='credential_secret_missing')
        return resolve(repo, boundary, ref)

    def prepare(route, **kwargs):
        if route['providerId'] == 'provider-b':
            if failure == 'route_changed':
                resilient.state.revision += 1
            if failure == 'unknown':
                raise RuntimeError('unknown preparation fault')
        group = mint(route, **kwargs)
        minted.append(group)
        return group

    def transport(body, **kwargs):
        slot, = (slot for slot in resilient.pool.slots if slot.provider_id == get_pinned_provider())
        calls.append(slot.routing_provider_id)
        assert slot.routing_provider_id == 'provider-a'
        return _complete(slot)

    monkeypatch.setattr(model_routing.ModelRoutingRepository, 'resolve_secret', resolve_secret)
    monkeypatch.setattr(_auxiliary_route, 'mint_auxiliary_route', prepare)
    monkeypatch.setattr(llm, 'stream_chat', transport)
    usage = {}
    with provider_pin(_PARENT):
        quote = _quote(resilient) if failure == 'quoted' else None
        result = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage, expected_route=quote)
        assert get_pinned_provider() == _PARENT
    assert resilient.source == original and resilient.pool.slots == [resilient.parent]
    assert all(group.disposed for group in minted)
    if failure == 'missing_secret':
        assert result == '### Objective\nPreserve the authorized work.'
        assert calls == ['provider-a']
        assert [(row['providerId'], row['providerReached']) for row in usage['_summaryAttempts']] == [
            ('provider-a', True)]
        assert '_summaryFailure' not in usage
    else:
        assert result is None and calls == []
        assert usage['_summaryFailure']['code'] == {
            'route_changed': 'summary_route_revalidation_failed',
            'unknown': 'summary_dispatch_failed',
            'quoted': 'summary_route_unavailable',
        }[failure]


def _quote(native):
    from lib.tasks_pkg.compaction._constants import _SUMMARY_MAX_TOKENS, _SUMMARY_DISPATCH_MAX_TOKENS
    from lib.tasks_pkg.compaction._summary_routing import select_summary_route
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

    tokens = _estimate_total_tokens([
        {'role': 'system', 'content': native.summary._SUMMARY_SYSTEM_PROMPT},
        {'role': 'user', 'content': 'fixed input'}])
    route = select_summary_route(native.task, tokens, _SUMMARY_MAX_TOKENS,
                                 capacity_output_tokens=_SUMMARY_DISPATCH_MAX_TOKENS)
    assert route.get('requestOwned') and route['providerId'] == 'provider-b'
    return route


@pytest.mark.parametrize('paid', [True, False])
@pytest.mark.parametrize('cold', [True, False])
def test_no_legacy_claude_slot_still_sends_one_authorized_summary(missing_claude, monkeypatch, paid, cold):
    from lib import llm

    native = missing_claude
    native.state.document['offerings'][1]['enabled'] = paid
    native.task.update(id='old-claude-summary', convId='old-claude-conversation', aborted=False)
    source, calls = deepcopy(native.source), []
    if cold:
        native.pool.invalidate_configuration()
        monkeypatch.setattr(native.pool, 'initialize', lambda: type(native.pool).initialize(native.pool))
        monkeypatch.setattr(native.pool, '_build_slots', lambda: ([], {}))
        monkeypatch.setattr(native.pool, '_load_benchmark_data', lambda slots: None)

    def transport(body, **kwargs):
        slot, = native.pool.slots.scope(get_pinned_provider())
        calls.append((slot.routing_provider_id, slot.logical_model, slot.oauth))
        return _complete(slot)

    monkeypatch.setattr(llm, 'stream_chat', transport)
    usage = {}
    with provider_pin(_PARENT):
        if cold:
            # Price/capacity preflight does not load configured credentials.
            from lib.tasks_pkg.compaction._summary_routing import select_summary_route
            assert not select_summary_route(native.task, 1000, 2048).get('requestOwned')
            assert native.pool.loaded_model_match(native.task['model'], pin=_PARENT) is None
        assert native.summary._generate_query_aware_summary(
            native.source, 'continue', task=native.task, usage_out=usage) == (
                '### Objective\nPreserve the authorized work.')
        assert get_pinned_provider() == _PARENT
    assert calls == ([('provider-b', 'deepseek-v4-flash', '')] if paid else [('provider-a', 'gpt-6.1-sol', 'codex')])
    assert native.source == source and native.task['model'] == 'claude-opus-5.5'
    assert native.pool.slots == [native.parent]
    assert len(usage['_summaryCalls']) == 1


def test_legacy_missing_main_summary_reports_no_route_without_provider_request(native, monkeypatch):
    from lib import llm

    native.task['config']['compaction']['summaryPolicy'] = 'main'
    native.task['model'] = 'unavailable-main'
    native.task.update(id='summary-missing-main-task', convId='summary-missing-main-conv', aborted=False)
    transport = Mock(side_effect=AssertionError('must not call the provider'))
    monkeypatch.setattr(llm, 'stream_chat', transport)
    usage = {}
    with provider_pin(_PARENT):
        assert native.summary._generate_query_aware_summary(
            native.source, 'continue', task=native.task, usage_out=usage) is None
    transport.assert_not_called()
    assert usage['_summaryFailure'] == {'code': 'summary_route_unavailable', 'attempts': 0}
