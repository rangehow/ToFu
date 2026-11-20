"""Combined selector, wire-ownership and probe-evidence convergence contracts."""
import copy

import pytest

from lib.model_routing import template_supply

pytestmark = pytest.mark.unit

URL = 'https://synthetic.invalid/v1'


def connection(name):
    return {'connection_id': name, 'provider_access_id': 'access',
            'base_url': URL, 'protocol': 'openai_responses',
            'gateway_namespace': name, 'enabled': True,
            'extra_headers': {'x-context-provider': name}}


def template():
    return {'base_url': URL, 'protocol': 'responses',
            'faces': {name: {'base_url': URL, 'protocol': 'responses',
                             'extra_headers': row['extra_headers']}
                      for name in ('kunlun', 'tokrouter')
                      for row in [connection(name)]},
            'offering_recipes': [{'model_id': 'new-model',
                                  'request_ids': ['shared-wire'],
                                  'request_faces': {'shared-wire': 'tokrouter'}}]}


def document():
    return {'connections': [connection('kunlun'), connection('tokrouter')],
            'offerings': [{'offering_id': 'old', 'provider_access_id': 'access',
                           'identity_state': 'confirmed', 'stale': False,
                           'enabled': True, 'model': {'creator_id': 'creator',
                                                     'model_id': 'old-model'}}],
            'deployments': [{'deployment_id': 'old-dep', 'offering_id': 'old',
                             'connection_id': 'kunlun', 'wire_model_id': 'shared-wire',
                             'probe_status': 'passed'}]}


@pytest.mark.parametrize('old_face, retires', [('kunlun', False), ('tokrouter', True)])
def test_rename_requires_the_same_selector_face(old_face, retires):
    working, access, recipe = _rename_pair()
    old = working['offerings'][0]
    working['deployments'][0]['connection_id'] = old_face
    before = copy.deepcopy(working)
    assert template_supply._supersede_renamed_supply(
        working, access, recipe, {'superseded': []}) is retires
    assert old['stale'] is retires
    if not retires:
        assert working == before


@pytest.mark.parametrize('blocker', ['unrelated-wire', 'disabled-face', 'wrong-selector', 'denied-grant'])
def test_ineligible_replacement_does_not_retire_existing_supply(blocker):
    working, access, recipe = _rename_pair()
    heir = working['deployments'][1]
    if blocker == 'unrelated-wire':
        heir['wire_model_id'] = 'owner-only-wire'
    elif blocker == 'disabled-face':
        working['connections'].append({**working['connections'][1],
                                       'connection_id': 'disabled-heir', 'enabled': False})
        heir['connection_id'] = 'disabled-heir'
        working['credentials'][0]['authorization']['connection_ids'].append('disabled-heir')
    elif blocker == 'wrong-selector':
        heir['extra_headers'] = {'x-context-provider': 'other'}
    else:
        working['credentials'][0]['authorization']['models'][1]['excluded_wire_model_ids'] = ['new-wire']
    before = copy.deepcopy(working)
    assert not template_supply._supersede_renamed_supply(
        working, access, recipe, {'superseded': []})
    assert working == before


def test_mutual_rename_cannot_retire_both_offerings():
    working, access, recipe = _rename_pair()
    # Each recipe authors the other's full pool, but both only have a
    # partial match on their own offering. Neither is a stable successor.
    working['deployments'].extend([
        {**row, 'deployment_id': row['deployment_id'] + '-extra',
         'wire_model_id': wire}
        for row, wire in zip(working['deployments'], ['old-extra', 'new-extra'])])
    recipe['offering_recipes'][0]['request_ids'].append('old-extra')
    recipe['offering_recipes'].append({
        'model_id': 'old-model', 'face': 'tokrouter',
        'request_ids': ['shared-wire', 'new-wire', 'new-extra']})
    before = copy.deepcopy(working)
    for _ in range(2):
        assert not template_supply._supersede_renamed_supply(
            working, access, recipe, {'superseded': []})
    assert working == before


def _rename_pair():
    from tests.model_routing.test_model_routing_contract import _full_document

    seed = _full_document()
    access = {**seed['provider_accesses'][0], 'provider_access_id': 'access'}
    working = {**seed, **document(), 'provider_accesses': [access]}
    working['deployments'][0]['connection_id'] = 'tokrouter'
    working['deployments'].append({
        **working['deployments'][0], 'deployment_id': 'new-dep',
        'offering_id': 'new', 'wire_model_id': 'new-wire'})
    working['offerings'].append({**working['offerings'][0], 'offering_id': 'new'})
    refs = []
    for index, model_id in enumerate(('old-model', 'new-model')):
        model = working['models'][index]
        model['model_id'] = model_id
        ref = {'creator_id': model['creator_id'], 'model_id': model_id}
        working['offerings'][index].update(
            model=ref, capabilities=['text'], context_window=32768)
        refs.append(dict(ref))
    working['credentials'] = [{
        **seed['credentials'][0], 'provider_access_id': 'access',
        'authorization': {'connection_ids': ['tokrouter'], 'models': refs}}]
    spec = template()
    spec['offering_recipes'][0].update(
        face='tokrouter', request_ids=['shared-wire', 'new-wire'])
    return working, access, spec


@pytest.mark.parametrize('target_state, retires', [
    ('disabled', False), ('revoked', False), ('concurrent-revoke', False), ('usable', True),
])
def test_retirement_checks_replacement_after_backfill(monkeypatch, target_state, retires):
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository

    working, access, recipe = _rename_pair()
    for model in working['models']:
        model.update(
            capabilities=['text'], registration_state='ready',
            registration_metadata={'release_date': '2026-01-01', 'checked_at': '2026-01-01',
                                   'source_urls': ['https://synthetic.invalid/card']},
            list_pricing={'input': 1, 'output': 1, 'currency': 'USD',
                          'unit': 'per_million_tokens'},
            inference_profile={'reasoning': {'support': 'unsupported'}})
    # Both old and new are served on an enabled historical Connection with
    # the exact request headers. Backfill moves only the heir to TokRouter's
    # exact namespaced row, where the owner can have disabled/revoked it.
    target = working['connections'][1]
    target['enabled'] = target_state != 'disabled'
    working['connections'].append({**target, 'connection_id': 'legacy',
                                   'gateway_namespace': 'legacy', 'enabled': True})
    for row in working['deployments']:
        row.update(connection_id='legacy', identity_confidence='high')
    grants = working['credentials'][0]['authorization']['connection_ids']
    grants.append('legacy')
    if target_state == 'revoked':
        grants.remove('tokrouter')
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(117)
    repository.compare_and_swap(boundary, working, expected_revision=0)
    monkeypatch.setattr(template_supply, '_find_template', lambda *_: recipe)

    def unexpected_secret(*_):
        pytest.fail('backfill/retirement cannot read a credential secret')

    monkeypatch.setattr(repository, 'resolve_secret', unexpected_secret)
    attempted = []
    if target_state == 'concurrent-revoke':
        original_cas = repository.compare_and_swap

        def revoke_before_first_cas(owner, candidate, *, expected_revision):
            attempted.append(next(row['stale'] for row in candidate['offerings']
                                  if row['offering_id'] == 'old'))
            if len(attempted) == 1:
                latest = repository.get(owner)
                latest.document['credentials'][0]['authorization']['connection_ids'].remove('tokrouter')
                original_cas(owner, latest.document, expected_revision=latest.revision)
            return original_cas(owner, candidate, expected_revision=expected_revision)

        monkeypatch.setattr(repository, 'compare_and_swap', revoke_before_first_cas)
    stats = template_supply.sync_template_supply(
        boundary=boundary, repository=repository, backfill_only=True)
    current = repository.get(boundary).document
    assert next(row for row in current['deployments']
                if row['deployment_id'] == 'new-dep')['connection_id'] == 'tokrouter'
    assert next(row for row in current['offerings']
                if row['offering_id'] == 'old')['stale'] is retires
    assert stats['superseded_offerings'] == int(retires)
    if target_state == 'concurrent-revoke':
        assert attempted == [True, False]
        assert stats['repaired_deployments'] == 1


def test_authored_wire_alone_cannot_retire_an_owner_bound_offering():
    working = document()
    working['deployments'][0]['connection_id'] = 'tokrouter'
    before = copy.deepcopy(working)
    assert not template_supply._supersede_renamed_supply(
        working, {'provider_access_id': 'access'}, template(), {'superseded': []})
    assert working == before


def test_transplant_reports_different_face_access_wire_conflict_without_edits():
    working = document()
    before = copy.deepcopy(working)
    bundle = {'connections': [connection('tokrouter')], 'models': [], 'creators': [],
              'offerings': [{'offering_id': 'new', 'pending_model_id': 'new-model'}],
              'deployments': [{'offering_id': 'new', 'connection_id': 'tokrouter',
                               'wire_model_id': 'shared-wire'}]}
    stats = {'wire_conflicts': [], 'added_offerings': 0, 'added_deployments': 0,
             'accesses': {}}
    assert not template_supply._supersede_renamed_supply(
        working, {'provider_access_id': 'access'}, template(), {'superseded': []})
    assert not template_supply._transplant(
        working, {'provider_access_id': 'access'}, bundle,
        {'new-model': 'ok'}, stats, [])
    assert working == before
    assert stats['wire_conflicts'] == ['access:shared-wire']


@pytest.mark.parametrize('failed', ['not_found', 'unknown', 'rate_limited', 'exception'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('same_wire', [False, True])
def test_sibling_probe_success_does_not_pass_failed_tokrouter_cell(
        monkeypatch, failed, reverse, same_wire):
    from lib.provider import provider_probe

    native_wire = 'tokrouter.wire' if same_wire else 'native-wire'
    work = [(index, 'synthetic-key', 'new-model', wire, [], URL, 'openai_responses',
             {'extra_headers': connection(face)['extra_headers']})
            for index, (wire, face) in enumerate([
                (native_wire, 'kunlun'), ('tokrouter.wire', 'tokrouter')])]
    if reverse:
        work.reverse()
    monkeypatch.setattr(provider_probe, 'build_probe_work', lambda *a, **kw: work)
    def probe(url, key, wire, headers, *args, on_prepared_headers, **kwargs):
        if failed not in ('unknown', 'exception') or headers.get('x-context-provider') != 'tokrouter':
            on_prepared_headers(headers)
        if headers.get('x-context-provider') != 'tokrouter':
            return 'ok', ''
        if failed == 'exception':
            raise RuntimeError('synthetic cell failure')
        return failed, ''

    monkeypatch.setattr(provider_probe, 'probe_one_cell', probe)
    verdicts = template_supply._live_prober(
        base_url=URL, protocol='responses', faces={}, api_key='synthetic-key',
        extra_headers={}, recipes=[])
    working = {'connections': [connection('kunlun'), connection('tokrouter')],
               'offerings': [], 'deployments': []}
    bundle = {'connections': working['connections'], 'models': [], 'creators': [],
              'offerings': [{'offering_id': 'new', 'pending_model_id': 'new-model'}],
              'deployments': [{'offering_id': 'new', 'connection_id': face,
                               'wire_model_id': wire}
                              for wire, face in (
                                  [('tokrouter.wire', 'tokrouter')] if same_wire else
                                  [('native-wire', 'kunlun'), ('tokrouter.wire', 'tokrouter')])]}
    assert template_supply._transplant(
        working, {'provider_access_id': 'access'}, bundle, verdicts,
        {'added_offerings': 0, 'added_deployments': 0, 'accesses': {}}, [])
    expected = {'tokrouter.wire': 'unprobed'}
    if not same_wire:
        expected['native-wire'] = 'passed'
    assert {row['wire_model_id']: row['probe_status'] for row in working['deployments']} == expected


def test_recipe_only_success_does_not_attest_unreported_cells():
    working = {'connections': [connection('kunlun'), connection('tokrouter')],
               'offerings': [], 'deployments': []}
    bundle = {'connections': working['connections'], 'models': [], 'creators': [],
              'offerings': [{'offering_id': 'new', 'pending_model_id': 'new-model'}],
              'deployments': [{'offering_id': 'new', 'connection_id': face,
                               'wire_model_id': wire}
                              for wire, face in [('native-wire', 'kunlun'),
                                                 ('tokrouter.wire', 'tokrouter')]]}
    assert template_supply._transplant(
        working, {'provider_access_id': 'access'}, bundle, {'new-model': 'ok'},
        {'added_offerings': 0, 'added_deployments': 0, 'accesses': {}}, [])
    assert {row['probe_status'] for row in working['deployments']} == {'unprobed'}


def test_backfill_conflicting_alias_never_duplicates_or_moves_other_offering():
    working = document()
    working['connections'] = [connection('kunlun')]
    working['offerings'].append({
        'offering_id': 'new', 'provider_access_id': 'access',
        'identity_state': 'confirmed',
        'model': {'creator_id': 'creator', 'model_id': 'new-model'}})
    before = copy.deepcopy(working)
    stats = {'wire_conflicts': []}
    assert not template_supply._backfill_request_faces(
        working, {'provider_access_id': 'access'}, template(), stats)
    assert working == before
    assert stats['wire_conflicts'] == ['access:shared-wire']


@pytest.mark.parametrize('revoke', [False, True])
def test_first_offering_preserves_concurrent_model_grant_revocation(monkeypatch, revoke):
    from lib import llm_dispatch
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.provider import provider_template_recipes

    working, _, spec = _rename_pair()
    new_offering = working['offerings'].pop()
    new_deployment = working['deployments'].pop()
    for dep in [*working['deployments'], new_deployment]:
        dep['identity_confidence'] = 'high'
    ref = new_offering['model']
    spec.update(key='synthetic', offering_recipes=[{'model_id': 'new-model',
                                                   'request_ids': ['new-wire']}])
    grants = working['credentials'][0]['authorization']['models']
    if not revoke:
        grants.remove(ref)
    bundle = {'connections': [connection('tokrouter')], 'models': [], 'creators': [],
              'offerings': [new_offering], 'deployments': [new_deployment]}
    repository = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(117)
    repository.compare_and_swap(owner, working, expected_revision=0)
    monkeypatch.setattr(template_supply, '_find_template', lambda *_: spec)
    monkeypatch.setattr(template_supply, '_resolve_api_key', lambda *a: ('synthetic', {}))
    monkeypatch.setattr(provider_template_recipes, 'compile_provider_template_bundle',
                        lambda *a, **kw: bundle)
    monkeypatch.setattr(template_supply, 'audit_log', lambda *a, **kw: None)
    monkeypatch.setattr(llm_dispatch, 'reset_dispatcher', lambda: None)
    original_cas = repository.compare_and_swap
    attempts = []

    def cas(boundary, candidate, *, expected_revision):
        attempts.append(expected_revision)
        if revoke and len(attempts) == 1:
            latest = repository.get(boundary)
            latest.document['credentials'][0]['authorization']['models'].remove(ref)
            original_cas(boundary, latest.document, expected_revision=latest.revision)
        return original_cas(boundary, candidate, expected_revision=expected_revision)

    monkeypatch.setattr(repository, 'compare_and_swap', cas)
    stats = template_supply.sync_template_supply(
        boundary=owner, repository=repository, templates=[spec],
        prober=lambda **kw: {'new-model': 'ok'})
    current = repository.get(owner).document
    assert len(attempts) == (2 if revoke else 1)
    assert stats['added_offerings'] == stats['added_deployments'] == 1
    assert any(dep['wire_model_id'] == 'new-wire' for dep in current['deployments'])
    assert (ref in current['credentials'][0]['authorization']['models']) is not revoke
