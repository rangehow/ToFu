"""Compiler-backed, provider-independent compression candidate specifications."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._summary_routing import select_summary_route
from tests.compaction.test_auxiliary_summary_integration import native as native  # noqa: F401

pytestmark = pytest.mark.unit


def _alternate(native, model, kind='subscription'):
    doc = native.state.document
    old = 'gpt-6-luna'
    for row in doc['models']:
        if row['model_id'] == old:
            row['model_id'] = model
    for row in doc['credentials']:
        for ref in row['authorization']['models']:
            if ref['model_id'] == old:
                ref['model_id'] = model
    for row in doc['offerings']:
        if row['model']['model_id'] == old:
            row['model']['model_id'] = model
    doc['offerings'][2]['enabled'] = False
    doc['credentials'][1]['kind'] = kind
    if kind != 'api_key':
        doc['offerings'][1].pop('actual_pricing', None)
    doc['providers'][1]['brand'] = 'meituan'
    return doc['offerings'][1]


@pytest.fixture
def missing_claude(native):
    """A legacy continuation whose strict scope still has only Codex slots."""
    native.task['model'] = 'claude-opus-5.5'
    native.pool._initialized = True  # The strict picker's configured view is already loaded.
    offering = _alternate(native, 'deepseek-v4-flash', 'api_key')
    offering['actual_pricing'].update(input=.1, output=.5)
    return native


def test_missing_legacy_main_uses_owner_compiled_paid_route_then_codex(missing_claude):
    native = missing_claude
    before = deepcopy(native.task)
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan.get('requestOwned') and plan['modelId'] == 'deepseek-v4-flash', plan
    assert plan['providerId'] == 'provider-b'
    assert plan['routeIdentity']['credentialId'] == native.state.document['credentials'][1]['credential_id']
    assert plan['projectedCostUsd'] == pytest.approx(.001124)
    assert plan['maximumCostUsd'] >= plan['projectedCostUsd']
    assert plan['_parentRouteIdentities'] == ()

    next_plan = select_summary_route(native.task, 1000, 2048,
        excluded_route_identities=(plan['routeIdentity'],))
    assert next_plan.get('requestOwned') and next_plan['modelId'] == native.parent.logical_model
    assert next_plan['selectionBasis']['resourceTier'] == 1
    assert next_plan['projectedCostUsd'] is None
    assert next_plan['routeIdentity']['revision'] == plan['routeIdentity']['revision']
    assert native.task == before and not native.state.resolved
    assert native.pool.slots == [native.parent]


@pytest.mark.parametrize('fault', ['pending_parent', 'provider_hint_scope', 'picker_reload', 'optout',
                                  'main', 'invalid_policy', 'provider_conflict', 'tenant_conflict',
                                  'repository_unavailable', 'no_grants', 'request_authority'])
def test_unresolved_main_does_not_broaden_other_authority(missing_claude, monkeypatch, fault):
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary, bind_request_routing_authority
    from tests.compaction.test_summary_routing import _slot
    native = missing_claude
    if fault == 'pending_parent':
        native.pool.slots.append(_slot(native.task['model'], route_offering_id='not-in-owner-table'))
    elif fault == 'provider_hint_scope':
        native.task['provider_id'] = native.task.pop('_pinned_provider_id')
        native.pool.slots.append(_slot(native.task['model']))
    elif fault == 'picker_reload':
        get = ModelRoutingRepository.get
        reads = 0

        def reload_on_compiler_read(repository, boundary):
            nonlocal reads
            reads += 1
            if reads == 2:
                native.pool.slots.append(_slot(native.task['model']))
            return get(repository, boundary)

        monkeypatch.setattr(ModelRoutingRepository, 'get', reload_on_compiler_read)
    elif fault == 'optout':
        native.task['config']['disableModelFallback'] = True
    elif fault == 'main':
        native.task['config']['compaction']['summaryPolicy'] = 'main'
    elif fault == 'invalid_policy':
        native.task['config']['compaction']['summaryPolicy'] = 'other'
    elif fault == 'provider_conflict':
        native.task['provider_id'] = 'one'
        native.task['config']['providerId'] = 'another'
    elif fault == 'tenant_conflict':
        native.task['_tenant_id'] = 'other-tenant'
    elif fault == 'repository_unavailable':
        def unavailable(*args):
            raise RuntimeError('offline authority')
        monkeypatch.setattr(ModelRoutingRepository, 'get', unavailable)
    elif fault == 'no_grants':
        for credential in native.state.document['credentials']:
            credential['enabled'] = False
    else:
        bind_request_routing_authority(native.task, object(), OwnerBoundary.create(41, 'tenant-a'))
    plan = select_summary_route(native.task, 1000, 2048)
    assert not plan.get('requestOwned') and plan['modelId'] == native.task['model'], plan
    assert not native.state.resolved


def test_missing_main_can_recover_once_picker_supply_is_ready(missing_claude, monkeypatch):
    native = missing_claude
    monkeypatch.setattr(native.pool, 'initialize', lambda: pytest.fail('Selection must be secret-free'))
    native.pool.invalidate_configuration()
    cold = select_summary_route(native.task, 1000, 2048)
    assert not cold.get('requestOwned') and cold['reason'] == 'main_route_unresolved'
    native.pool._initialized = True
    ready = select_summary_route(native.task, 1000, 2048)
    assert ready.get('requestOwned') and ready['modelId'] == 'deepseek-v4-flash'
    assert not native.state.resolved


def test_missing_main_never_qualifies_a_billing_stopped_route(missing_claude, monkeypatch):
    native = missing_claude
    paid_credential = native.state.document['credentials'][1]['credential_id']
    monkeypatch.setattr('lib.key_stats.is_key_refused_for_optional_work',
                        lambda namespace, credential, model: credential == paid_credential)
    plan = select_summary_route(native.task, 1000, 2048)
    assert plan.get('requestOwned') and plan['modelId'] == native.parent.logical_model
    assert plan['_requestRoute']['routeIdentity']['credentialId'] == paid_credential
    assert not native.state.resolved


def test_exact_exclusion_keeps_other_deployments_of_same_credential(missing_claude):
    native = missing_claude
    deployment = native.state.document['deployments'][1]
    native.state.document['deployments'].append(dict(deployment,
        deployment_id='other-deepseek', wire_model_id='other-deepseek-wire'))
    first = select_summary_route(native.task, 1000, 2048)
    second = select_summary_route(native.task, 1000, 2048,
        excluded_route_identities=(first['routeIdentity'],))
    assert first['modelId'] == second['modelId'] == 'deepseek-v4-flash'
    assert first['routeIdentity']['credentialId'] == second['routeIdentity']['credentialId']
    assert first['routeIdentity']['deploymentId'] != second['routeIdentity']['deploymentId']
    assert not native.state.resolved


def test_exact_exclusion_does_not_change_fitting_paid_main_policy(native):
    native.parent.oauth = ''
    native.state.document['offerings'][0]['actual_pricing'] = {
        'input': 10, 'output': 10, 'currency': 'USD'}
    native.state.document['offerings'][1]['actual_pricing'].update(input=.1, output=.5)
    first = select_summary_route(native.task, 1000, 2048)
    assert first.get('requestOwned') and first['providerId'] == 'provider-b', first
    second = select_summary_route(native.task, 1000, 2048,
        excluded_route_identities=(first['routeIdentity'],))
    # The independent subscription for the same auxiliary model still exists,
    # but this fitting paid main never spends the owner's subscription quota.
    assert not second.get('requestOwned') and second['modelId'] == native.task['model']
    assert second['reason'] == 'no_remaining_authorized_auxiliary_route'
    assert not native.state.resolved


@pytest.mark.parametrize('model', ['glm-5.3-flash', 'GEM5.3Flash', 'new-admitted-text-model'])
@pytest.mark.parametrize('main_paid', [False, True])
def test_all_admitted_families_participate_without_aa_or_name_allowlist(native, model, main_paid):
    offering = _alternate(native, model)
    if main_paid:
        native.parent.oauth = ''
        # A paid main that cannot fit the summary output may borrow an
        # authorized subscription auxiliary with its own sufficient capacity;
        # missing main pricing does not block it.
        native.parent.max_output_tokens = 1024
    before = deepcopy(native.task)
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'], plan
    assert plan['modelId'] == model
    assert plan['providerId'] == 'provider-b'
    assert plan['routeIdentity']['offeringId'] == offering['offering_id']
    assert plan['routeIdentity']['credentialId'] == native.state.document['credentials'][1]['credential_id']
    assert plan['projectedCostUsd'] is None
    assert plan['costBasis']['marginalCostKnown'] is False
    assert plan['selectionBasis']['eligibleRoutes'] >= 1
    assert plan['selectionBasis']['source'] != 'Artificial Analysis'
    if model != 'glm-5.3-flash':
        assert plan['selectionBasis']['source'] == 'routing_admission'
        assert plan['selectionBasis']['score'] is None
    assert native.task == before and not native.state.resolved
    assert native.pool.slots == [native.parent]


@pytest.mark.parametrize('model', ['glm-5.3-flash', 'new-admitted-text-model'])
def test_paid_alternate_needs_a_known_price_not_aa_coverage(native, model):
    """A request-name rate card wins; without one the model's maintained price
    applies (owner decision 2026-11-02); with neither the route is unknown."""
    from lib.pricing._tables import pricing_for_model
    offering = _alternate(native, model, 'api_key')
    native.parent.oauth = ''
    native.state.document['offerings'][0]['actual_pricing'] = {
        'input': 10, 'output': 10, 'currency': 'USD'}
    offering['actual_pricing'] = {'input': .1, 'output': .5, 'currency': 'USD'}
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'] and plan['modelId'] == model, plan
    assert plan['costBasis']['source'] == 'declared_offering_pricing'
    assert plan['selectionBasis']['source'] != 'Artificial Analysis'
    offering.pop('actual_pricing')
    fallback = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    if pricing_for_model(model):
        assert fallback['requestOwned'] and fallback['modelId'] == model, fallback
        assert fallback['costBasis']['source'] == 'model_table'
        assert fallback['declaredPricing'] is None
    else:
        assert not fallback.get('requestOwned')


@pytest.mark.parametrize('failure', ['success', 'error', 'incomplete', 'cancel', 'base_exception'])
def test_api_key_backed_subscription_dispatch_has_one_private_route_and_tears_down(native, monkeypatch, failure):
    from lib.llm_dispatch.provider_pin import provider_pin, get_pinned_provider
    from lib.llm.stream_result import ProviderStreamResult
    _alternate(native, 'GEM5.3Flash')
    native.task['aborted'] = False
    seen = []
    def stream(*args, **kwargs):
        assert kwargs['prefer_model'] == 'GEM5.3Flash'
        assert kwargs['strict_model']
        pin = get_pinned_provider()
        assert pin != 'model-route:41:task-a'
        slots = [s for s in native.pool.slots if s.provider_id == pin]
        assert len(slots) == 1
        seen.append(slots[0])
        assert slots[0].api_key == 'offline-fixture' and not slots[0].oauth
        if failure == 'base_exception':
            raise KeyboardInterrupt()
        if failure == 'error':
            raise RuntimeError('offline test')
        if failure == 'cancel':
            native.task['aborted'] = True
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nComplete cross-family summary.'},
            'length' if failure == 'incomplete' else 'stop',
            {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin('model-route:41:task-a'):
        if failure == 'base_exception':
            with pytest.raises(KeyboardInterrupt):
                native.summary._generate_query_aware_summary(native.source, 'continue', task=native.task)
        else:
            result = native.summary._generate_query_aware_summary(native.source, 'continue', task=native.task)
            assert bool(result) == (failure == 'success')
        assert get_pinned_provider() == 'model-route:41:task-a'
    assert len(seen) == 1
    assert seen[0].api_key == '' and seen[0].oauth == ''
    assert native.pool.slots == [native.parent]
    assert native.task['model'] == 'gpt-6.1-sol'


@pytest.mark.parametrize('invalid', ['disabled', 'revoked', 'capability', 'window', 'output'])
def test_unusable_or_unauthorized_alternate_is_refused(native, invalid):
    offering = _alternate(native, 'GEM5.3Flash')
    if invalid == 'disabled':
        offering['enabled'] = False
    elif invalid == 'revoked':
        native.state.document['credentials'][1]['enabled'] = False
    elif invalid == 'capability':
        offering['capabilities'] = ['embedding']
    elif invalid == 'window':
        offering['context_window'] = 2500
    else:
        native.state.document['deployments'][1]['max_output_tokens'] = 1024
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert not plan.get('requestOwned'), plan
    assert not native.state.resolved


def test_null_score_receipt_is_truthful_and_schema_valid(native):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from lib.tasks_pkg.compaction._receipt import _summary_selection_details
    _alternate(native, 'GEM5.3Flash')
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    evidence = _summary_selection_details(plan['selectionBasis'])
    assert evidence['score'] is None and evidence['source'] == 'routing_admission'
    schema = json.loads((Path(__file__).parents[2] / 'contracts/compaction_receipt_v1.schema.json').read_text())
    Draft202012Validator(schema['$defs']['summarySelection']).validate(evidence)
    assert _summary_selection_details({**evidence, 'score': 0}) == {}


@pytest.mark.parametrize('reverse', [False, True])
def test_first_paid_witness_cannot_hide_later_subscription_witness(native, reverse):
    from contextlib import ExitStack
    from lib.tasks_pkg.compaction._auxiliary_route import mint_auxiliary_route
    offering = _alternate(native, 'GEM5.3Flash', 'api_key')
    offering.pop('actual_pricing', None)
    credential = deepcopy(native.state.document['credentials'][1])
    credential.update(credential_id='z-subscription', kind='subscription')
    native.state.document['credentials'].append(credential)
    if reverse:
        native.state.document['credentials'].reverse()
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'], plan
    assert plan['routeIdentity']['credentialId'] == 'z-subscription'
    assert not native.state.resolved
    with ExitStack() as lifecycle:
        group = mint_auxiliary_route(plan, owner=41, tenant='tenant-a', lifecycle=lifecycle)
        slots = [slot for slot in native.pool.slots if slot.provider_id == group.pin_id]
        assert len(slots) == 1
        assert group.candidates[0].credential['credential_id'] == 'z-subscription'
        slot = slots[0]
    assert slot.api_key == '' and native.pool.slots == [native.parent]


@pytest.mark.parametrize('reverse', [False, True])
def test_low_output_first_deployment_cannot_hide_usable_later_deployment(native, reverse):
    _alternate(native, 'GEM5.3Flash')
    first = native.state.document['deployments'][1]
    first['max_output_tokens'] = 1024
    later = deepcopy(first)
    later.update(deployment_id='z-capable-deployment', max_output_tokens=32768,
                 wire_model_id='second-wire-b')
    native.state.document['deployments'].append(later)
    if reverse:
        native.state.document['deployments'].reverse()
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'], plan
    assert plan['routeIdentity']['deploymentId'] == 'z-capable-deployment'
    assert 2048 <= plan['capacityOutputTokens'] <= 32768


@pytest.mark.parametrize('prices', [None, {'input': -1, 'output': .5},
                                  {'input': 0, 'output': 0}, {'input': .1, 'output': .5}])
def test_paid_price_unknown_invalid_and_explicit_zero_are_distinct(native, prices):
    offering = _alternate(native, 'GEM5.3Flash', 'api_key')
    if prices is None:
        offering.pop('actual_pricing', None)
    else:
        offering['actual_pricing'] = {**prices, 'currency': 'USD'}
    # Retired by owner decision 2026-11-02: a legacy zero ceiling is ignored.
    native.task['config']['compaction']['summaryMaxCostUsd'] = 0
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    # GEM5.3Flash has no maintained model price: no rate card means unknown,
    # and an explicit invalid card never falls back to a guess.
    known = prices is not None and prices['input'] >= 0
    assert bool(plan.get('requestOwned')) is known, plan
    if known and prices['input'] == 0:
        assert plan['maximumCostUsd'] == plan['projectedCostUsd'] == 0


@pytest.mark.parametrize('legacy_ceiling', [None, 0, -1, True, 'invalid'])
def test_retired_cost_ceiling_never_blocks_a_long_context_paid_summary(native, legacy_ceiling):
    """Owner decision 2026-11-02: no summary cost ceiling. A 2M-token summary
    input costs more than any small fixed bound, yet must still reach the
    organization-billed route that fits it; summaryMaxCostUsd is ignored."""
    offering = _alternate(native, 'GEM5.3Flash', 'api_key')
    offering.update(context_window=2_200_000, actual_pricing={
        'input': .1, 'output': .5, 'currency': 'USD'})
    for model in native.state.document['models']:
        if model['model_id'] == 'GEM5.3Flash':
            model['context_window'] = 2_200_000
    if legacy_ceiling is not None:
        native.task['config']['compaction']['summaryMaxCostUsd'] = legacy_ceiling
    plan = select_summary_route(native.task, 2_000_000, 2048, capacity_output_tokens=65536)
    assert plan.get('requestOwned') and plan['modelId'] == 'GEM5.3Flash', plan
    assert plan['projectedCostUsd'] > .2
    assert plan['maximumCostUsd'] > plan['projectedCostUsd']
    assert not native.state.resolved


def _bind_parent(slot, suffix):
    slot.route_deployment_id = f'alpha-{suffix}-deployment'
    slot.route_connection_id = f'connection-{suffix}'
    slot.route_credential_id = f'credential-{suffix}'


@pytest.mark.parametrize('parent_kind', ['subscription', 'api_key'])
def test_parent_billing_uses_full_native_credential_not_oauth_transport(native, parent_kind):
    _alternate(native, 'GEM5.3Flash', 'api_key')
    native.parent.oauth = ''
    _bind_parent(native.parent, 'a')
    native.state.document['credentials'][0].update(kind=parent_kind, secret_reference='fixture-api')

    native.parent.max_output_tokens = 128000
    # A known main price cheaper than the alternate: a paid parent requires
    # strict savings, a subscription parent only an affordable maximum quote.
    native.state.document['offerings'][0]['actual_pricing'] = {
        'input': .01, 'output': .01, 'currency': 'USD'}
    native.state.document['offerings'][1]['actual_pricing'] = {
        'input': .1, 'output': .5, 'currency': 'USD'}
    before = deepcopy(native.task)
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert bool(plan.get('requestOwned')) is (parent_kind == 'subscription'), plan
    if parent_kind == 'subscription':
        assert plan['modelId'] == 'GEM5.3Flash'
    else:
        assert plan['modelId'] == 'gpt-6.1-sol'
        assert plan['reason'] == 'no_eligible_cheaper_aa_route'
    assert native.task == before and not native.state.resolved


@pytest.mark.parametrize('native_binding', [False, True])
def test_mixed_parent_can_plan_only_when_native_readiness_and_full_witnesses_hold(native, native_binding):
    from contextlib import nullcontext
    from lib.llm_dispatch.native_context_scope import native_context_scope
    from tests.compaction.test_summary_routing import _slot
    doc = native.state.document
    doc['offerings'][1]['model']['model_id'] = 'gpt-6.1-sol'
    doc['credentials'][1]['kind'] = 'subscription'
    _bind_parent(native.parent, 'a')
    second = _slot('gpt-6.1-sol', route_offering_id='alpha-b',
                   routing_provider_id='provider-b')
    second.model = 'alpha-wire-b'
    _bind_parent(second, 'b')
    native.pool.slots.append(second)
    before = deepcopy(native.task)
    if native_binding:
        native.task['id'] = 'task-a'
    metadata = {'owner_user_id': 41, 'provider_id': 'provider-a',
                'logical_model': 'gpt-6.1-sol', 'wire_model': native.parent.model,
                'context_window': 200000, 'offering_id': 'alpha-a',
                'deployment_id': 'alpha-a-deployment', 'connection_id': 'connection-a',
                'credential_id': 'credential-a'}
    # A single issued native binding cannot authorize a different provider
    # inside its parent group. The independent planner must not bypass it.
    scope = native_context_scope(native.task, metadata, 1024) if native_binding else nullcontext()
    with scope:
        plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert bool(plan.get('requestOwned')) is (not native_binding), plan
    if native_binding:
        assert plan['reason'] == 'native_parent_route_unready'
    else:
        assert plan['modelId'] == 'gpt-6-luna'
    assert not native.state.resolved
    assert native.pool.slots == [native.parent, second]
    assert native.task['model'] == before['model']
