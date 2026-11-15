"""Maintained official media facts put media Models in the registry.

docs/MODEL_REGISTRATION.md, "Model kinds and media facts": the world catalogue
is the registry of Models that exist. An exact identity with maintained
official media facts (lib/model_info/media_model_facts.json) is a developer
directory Model whether or not any template offers it, and its maintained kind
wins over a relay recipe that seeds it as a chat route. Maintained media facts
are exact knowledge, so a maintained release date publishes the Model without
owner evidence; without one it stays withheld, and with no live catalogue
listing it is never a release-watch candidate.
"""
from __future__ import annotations

import pytest

from lib.model_info import media_facts
from lib.model_info.capability_taxonomy import model_kind

pytestmark = pytest.mark.unit

OMNI = ('google', 'gemini-omni-1.2-flash')
LYRIA = ('google', 'lyria-3-pro-preview')
SYNTHETIC = ('google', 'gemini-omni-9-test')
RELEASE_GAP = 'registration_metadata.release_date + checked_at + source_urls'


@pytest.fixture
def templates():
    """Shipped package templates; the suite keeps the live feed overlay off."""
    from lib.provider.provider_template_recipes import load_provider_templates
    return load_provider_templates()


def _rows(templates):
    from lib.model_routing.world_catalog import _world_catalog_rows
    _creators, models = _world_catalog_rows(templates)
    return {(row['creator_id'], row['model_id']): row for row in models}


def _withheld(templates):
    from lib.model_routing.world_catalog import withheld_catalog_models
    return {(row['creator_id'], row['model_id']): row
            for row in withheld_catalog_models(templates)}


def _published(templates, monkeypatch):
    from lib.model_routing import OwnerBoundary
    from lib.model_routing.domain import public_projection
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.world_catalog import sync_world_model_catalog

    monkeypatch.setattr('lib.provider.provider_template_recipes.load_provider_templates',
                        lambda: templates)
    repository, boundary = InMemoryModelRoutingRepository(), OwnerBoundary.create(1)
    sync_world_model_catalog(boundary=boundary, repository=repository)
    return {(row['creator_id'], row['model_id']): row
            for row in public_projection(repository.get(boundary).document)['models']}


def _relay(*recipes):
    from lib.provider.provider_template_recipes import RECIPE_VERSION, normalize_provider_template
    return normalize_provider_template({
        'recipe_version': RECIPE_VERSION, 'key': 'relay-media-test', 'name': 'Relay',
        'category': 'relay', 'base_url': 'https://relay.example/v1',
        'offering_recipes': list(recipes)})


def test_media_identities_compile_without_their_creators_template(templates):
    from lib.model_routing.world_catalog import _world_catalog_rows

    relay_only = [row for row in templates if row['key'] == 'openrouter']
    creators, models = _world_catalog_rows(relay_only)

    named = {row['creator_id'] for row in creators}
    maintained = set(media_facts.maintained_media_identities())
    assert maintained <= {(row['creator_id'], row['model_id']) for row in models}
    assert {creator for creator, _model in maintained} <= named
    assert _withheld(relay_only) is not None


def test_official_templates_agree_with_the_maintained_media_kind(templates):
    from lib.model_routing.model_defaults import official_model_directory

    for row in official_model_directory(templates):
        facts = media_facts.maintained_media_facts(row['creator_id'], row['model_id'])
        if facts:
            assert model_kind(row['capabilities']) == facts['kind'], row['model_id']


def test_a_maintained_media_model_no_template_offers_is_published(templates, monkeypatch):
    assert OMNI in _rows(templates)
    assert OMNI not in _withheld(templates)

    row = _published(templates, monkeypatch)[OMNI]

    assert model_kind(row['capabilities']) == 'video_generation'
    assert row['release_date']
    assert {rate['unit'] for rate in row['official_media_pricing']['rates']} == {'per_second'}
    assert row['official_media_profile']['output_modalities'][0] == 'video'
    # Media kinds compile no reasoning controls.
    assert row['inference_profile']['reasoning']['support'] == 'unsupported'


def test_the_maintained_media_kind_wins_over_a_relay_chat_seed(templates):
    relay = _relay({'model_id': 'google/lyria-3-pro-preview', 'capabilities': ['text', 'vision']})

    row = _rows([*templates, relay])[LYRIA]

    assert model_kind(row['capabilities']) == 'audio_generation'
    missing = _withheld([*templates, relay]).get(LYRIA, {}).get('missing', [])
    assert not any(field.startswith('list_pricing') for field in missing)


def test_without_a_maintained_release_date_a_media_model_stays_withheld(templates, monkeypatch):
    import lib.model_routing.domain as domain

    catalog = media_facts._catalog()
    monkeypatch.setattr(media_facts, '_catalog', lambda: {**catalog, SYNTHETIC: catalog[OMNI]})

    withheld = _withheld(templates)[SYNTHETIC]

    assert withheld['missing'] == [RELEASE_GAP]
    # No live catalogue lists it, so the release watch never reports it.
    assert withheld['supply'] == []

    dated = domain._release_date
    monkeypatch.setattr(domain, '_release_date',
                        lambda model_id: '2026-12' if model_id == SYNTHETIC[1] else dated(model_id))
    assert SYNTHETIC not in _withheld(templates)
    assert model_kind(_published(templates, monkeypatch)[SYNTHETIC]['capabilities']) == (
        'video_generation')


# ── Stale chat drafts of media identities ───────────────────────────────

def _relay_install(*seeds):
    """An owner authority whose relay supplies *seeds* (legacy cutover)."""
    from lib.model_routing import (
        InMemoryModelRoutingRepository, OwnerBoundary, execute_migration, plan_legacy_migration)

    repository, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(4343)
    plan = plan_legacy_migration({'providers': [{
        'id': 'relay', 'base_url': 'https://relay.invalid/v1', 'api_keys': ['placeholder'],
        'models': list(seeds)}]})
    assert execute_migration(repository, owner, plan).enabled
    return repository, owner


def _stage(repository, owner, model_id, **fields):
    current = repository.get(owner)
    row = next(model for model in current.document['models'] if model['model_id'] == model_id)
    for key, value in fields.items():
        if value is None:
            row.pop(key, None)
        else:
            row[key] = value
    repository.compare_and_swap(owner, current.document, expected_revision=current.revision)


def _model_row(repository, owner, model_id):
    return next(row for row in repository.get(owner).document['models']
                if row['model_id'] == model_id)


def _offering(repository, owner, model_id):
    return next(row for row in repository.get(owner).document['offerings']
                if (row.get('model') or {}).get('model_id') == model_id)


CHAT_CAPS = ['text', 'thinking', 'vision']


def test_a_supply_seeded_chat_draft_converges_and_its_inheriting_offering_follows():
    """The live case: a gateway listing seeded Nano Banana 2 as a chat draft."""
    from lib.model_routing.world_catalog import sync_world_model_catalog

    repository, owner = _relay_install({'model_id': 'gemini-3.1-flash-image'})
    assert 'capabilities' in _offering(repository, owner, 'gemini-3.1-flash-image')[
        'inherited_model_fields']
    _stage(repository, owner, 'gemini-3.1-flash-image', capabilities=CHAT_CAPS,
           registration_state='pending_metadata', inference_profile=None)

    summary = sync_world_model_catalog(boundary=owner, repository=repository)

    assert summary['media_kinds_converged'] == 1
    row = _model_row(repository, owner, 'gemini-3.1-flash-image')
    assert row['capabilities'] == ['image_gen']
    assert row['inference_profile']['reasoning']['support'] == 'unsupported'
    assert _offering(repository, owner, 'gemini-3.1-flash-image')['capabilities'] == ['image_gen']
    again = sync_world_model_catalog(boundary=owner, repository=repository)
    assert again['revision'] == summary['revision']


@pytest.mark.parametrize('case', ['explicit_offering_claim', 'registration_evidence', 'admitted'])
def test_owner_facts_and_explicit_supply_claims_keep_the_draft(case):
    from lib.model_routing.world_catalog import sync_world_model_catalog

    seed = {'model_id': 'lyria-3-pro-preview'}
    if case == 'explicit_offering_claim':
        seed['capabilities'] = ['text', 'vision']
    repository, owner = _relay_install(seed)
    staged = {'capabilities': ['text', 'vision'], 'registration_state': 'pending_metadata',
              'inference_profile': {'reasoning': {'support': 'unsupported'}}}
    if case == 'registration_evidence':
        staged['registration_metadata'] = {
            'release_date': '2026-03-25', 'checked_at': '2026-11-12',
            'source_urls': ['https://docs.example.invalid/owner-chat-claim']}
    if case == 'admitted':
        staged['registration_state'] = 'ready'
        staged['list_pricing'] = {'input': 1, 'output': 2, 'currency': 'USD',
                                  'unit': 'per_million_tokens'}
    _stage(repository, owner, 'lyria-3-pro-preview', **staged)

    summary = sync_world_model_catalog(boundary=owner, repository=repository)

    assert summary.get('media_kinds_converged', 0) == 0
    assert _model_row(repository, owner, 'lyria-3-pro-preview')['capabilities'] == ['text', 'vision']


def test_a_legacy_unmarked_model_is_never_converged():
    """An unmarked row predates admission and may be an active legacy route."""
    from lib.model_routing.model_defaults import converge_media_kinds

    row = {'creator_id': 'google', 'model_id': 'lyria-3-pro-preview', 'capabilities': ['text']}
    document = {'models': [row], 'offerings': []}

    assert converge_media_kinds(document) == []
    assert row['capabilities'] == ['text']
    row['registration_state'] = 'pending_metadata'
    assert converge_media_kinds(document) == ['lyria-3-pro-preview']
    assert row['capabilities'] == ['audio_gen']
