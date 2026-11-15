"""A stored Model under a creator's publisher namespace re-keys to the bare id.

Older relay imports minted Models whose id kept a creator namespace
(``x-ai/grok-*``, ``thinkingmachines/thinkingmachines/inkling``). Once the
namespace counts as identity decoration, normalization re-keys a sole legacy
row to the creator's bare id and redirects Offering and Credential references;
the provider-owned ``Deployment.wire_model_id`` keeps its exact spelling. A
legacy row beside its bare twin collapses into one identity.
"""
from __future__ import annotations

import copy

import pytest

from lib.model_routing import normalize_document, plan_legacy_migration

pytestmark = pytest.mark.unit


def _relay_document(*wires):
    plan = plan_legacy_migration({'providers': [{
        'id': 'relay', 'base_url': 'https://relay.invalid/v1', 'api_keys': ['placeholder'],
        'models': [{'model_id': wire} for wire in wires]}]})
    return copy.deepcopy(plan.document)


def _respell(document, creator_id, legacy_id):
    """Store the creator's only Model under *legacy_id*, as an older import did."""
    (row,) = [model for model in document['models'] if model['creator_id'] == creator_id]
    old = {'creator_id': creator_id, 'model_id': row['model_id']}
    new = {'creator_id': creator_id, 'model_id': legacy_id}
    row['model_id'] = row['display_name'] = legacy_id
    for offering in document['offerings']:
        if offering.get('model') == old:
            offering['model'] = dict(new)
    for credential in document['credentials']:
        grants = credential['authorization']['models']
        credential['authorization']['models'] = [dict(new) if grant == old else grant
                                                 for grant in grants]
    return row


@pytest.mark.parametrize(('wire', 'creator_id', 'legacy_id', 'bare_id'), [
    ('x-ai/grok-9-test', 'xai', 'x-ai/grok-9-test', 'grok-9-test'),
    ('thinkingmachines/inkling-9-test', 'thinkingmachines',
     'thinkingmachines/thinkingmachines/inkling-9-test', 'inkling-9-test'),
    ('z-ai/glm-9-test', 'zhipu', 'z-ai/glm-9-test', 'glm-9-test'),
])
def test_a_sole_legacy_namespaced_row_rekeys_and_its_references_follow(
        wire, creator_id, legacy_id, bare_id):
    document = _relay_document(wire)
    _respell(document, creator_id, legacy_id)

    normalized = normalize_document(document)

    models = [row for row in normalized['models'] if row['creator_id'] == creator_id]
    assert [row['model_id'] for row in models] == [bare_id]
    ref = {'creator_id': creator_id, 'model_id': bare_id}
    assert [row['model'] for row in normalized['offerings']] == [ref]
    grants = [grant for credential in normalized['credentials']
              for grant in credential['authorization']['models']]
    assert grants == [ref]
    assert [row['wire_model_id'] for row in normalized['deployments']] == [wire]


def test_a_legacy_namespaced_row_beside_its_bare_twin_collapses_into_it():
    document = _relay_document('inkling-9-test')
    bare = copy.deepcopy(document['models'][0])
    legacy = copy.deepcopy(bare)
    legacy['model_id'] = legacy['display_name'] = 'thinkingmachines/inkling-9-test'
    document['models'] = [bare, legacy]

    normalized = normalize_document(document)

    assert [(row['creator_id'], row['model_id']) for row in normalized['models']] == [
        ('thinkingmachines', 'inkling-9-test')]
