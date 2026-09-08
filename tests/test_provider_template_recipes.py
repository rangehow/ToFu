"""Executable contract for catalog-backed provider onboarding recipes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lib.provider_template_recipes import (
    RECIPE_VERSION,
    ProviderTemplateRecipeError,
    compile_provider_template_bundle,
    normalize_provider_template,
    offering_recipes,
    provider_from_template,
)
pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'static' / 'provider_templates'


def _recipe(**changes):
    row = {
        'recipe_version': RECIPE_VERSION,
        'key': 'relay',
        'name': 'Relay',
        'base_url': 'https://relay.example/v1',
        'offering_recipes': [{
            'model_id': 'shared-model',
            'request_ids': ['vendor/shared-model-v2'],
            'capabilities': ['text', 'thinking'],
            'rpm': 60,
        }],
    }
    row.update(changes)
    return row


def test_authored_recipe_preserves_exact_logical_and_wire_identity():
    normalized = normalize_provider_template(_recipe())
    assert normalized['recipe_version'] == RECIPE_VERSION
    assert 'models' not in normalized
    entry = normalized['offering_recipes'][0]
    assert entry['model_id'] == 'shared-model'
    assert entry['request_ids'] == ['vendor/shared-model-v2']
    assert entry['capabilities'] == ['text', 'thinking']


def test_legacy_models_are_read_but_derived_alias_is_explicit():
    legacy = _recipe()
    legacy.pop('recipe_version')
    legacy['models'] = legacy.pop('offering_recipes')

    canonical = normalize_provider_template(legacy)
    assert canonical['recipe_version'] == RECIPE_VERSION
    assert 'models' not in canonical
    assert canonical['offering_recipes'][0]['model_id'] == 'shared-model'

    public = normalize_provider_template(legacy, include_legacy_models=True)
    assert public['models'] == public['offering_recipes']
    assert public['models'] is not public['offering_recipes']


def test_disagreeing_dual_fields_fail_loud():
    raw = _recipe(models=[{'model_id': 'other'}])
    with pytest.raises(ProviderTemplateRecipeError, match='disagree'):
        normalize_provider_template(raw)


def test_duplicate_provider_logical_offering_is_rejected():
    entry = _recipe()['offering_recipes'][0]
    raw = _recipe(offering_recipes=[entry, copy.deepcopy(entry)])
    with pytest.raises(ProviderTemplateRecipeError, match='duplicate'):
        offering_recipes(raw)


def test_provider_projection_is_legacy_transport_not_authored_authority():
    provider = provider_from_template(_recipe(), 'relay-installed')
    assert provider['id'] == 'relay-installed'
    assert 'offering_recipes' not in provider
    assert 'recipe_version' not in provider
    assert provider['models'][0]['model_id'] == 'shared-model'
    assert provider['models'][0]['request_ids'] == ['vendor/shared-model-v2']


def test_legacy_cost_is_removed_at_recipe_boundary():
    raw = _recipe(offering_recipes=[{
        'model_id': 'm', 'capabilities': ['text'], 'cost': 0.25,
    }])
    entry = offering_recipes(raw)[0]
    assert 'cost' not in entry
    assert entry['rpm'] == 30


def test_bundled_json_templates_use_recipe_authority():
    paths = sorted(TEMPLATES.glob('*.json'))
    assert paths
    for path in paths:
        raw = json.loads(path.read_text(encoding='utf-8'))
        assert raw['recipe_version'] == RECIPE_VERSION, path.name
        assert 'offering_recipes' in raw, path.name
        assert 'models' not in raw, path.name
        canonical = normalize_provider_template(raw, allow_legacy=False)
        assert canonical['key'] == raw['key']
        if raw.get('category') != 'local':
            assert len(canonical['offering_recipes']) > 0, path.name


def test_settings_templates_share_bootstrap_builtins():
    """Both pickers merge the same file-backed catalog (lib/model_info/data
    then static/provider_templates), so the bootstrap installer and the
    Settings picker must offer identical providers with identical models."""
    from bootstrap_pkg.providers import (
        _load_provider_templates as bootstrap_load,
    )

    from lib.provider_template_recipes import load_provider_templates

    settings_rows = {row['key']: row for row in load_provider_templates()}
    bootstrap_rows = {
        row['key']: row for row in bootstrap_load()
        if row['key'] != 'custom'  # bootstrap-only form placeholder
    }
    assert set(bootstrap_rows) <= set(settings_rows)
    for key, bootstrap_row in bootstrap_rows.items():
        assert {m['model_id'] for m in bootstrap_row['models']} == {
            r['model_id'] for r in settings_rows[key]['offering_recipes']
        }, key
    # bootstrap-only rows stay out of the Settings picker.
    assert 'custom' not in settings_rows
    # Anthropic enters the Settings picker via the package static recipe
    # (native protocol), which overrides the protocol-less builtin stub.
    anthropic = settings_rows['anthropic']
    assert anthropic['protocol'] == 'anthropic'
    assert anthropic['offering_recipes'][0]['model_id'] == 'claude-opus-5'


def test_static_template_overrides_builtin_recipe_list():
    from lib.provider_template_recipes import load_provider_templates

    templates = {row['key']: row for row in load_provider_templates()}
    static_glm = json.loads((TEMPLATES / 'glm.json').read_text(encoding='utf-8'))
    assert len(templates['glm']['offering_recipes']) == len(
        static_glm['offering_recipes'])
    assert len(templates['openai']['offering_recipes']) == 16


def test_every_shared_builtin_compiles_an_onboarding_bundle():
    from lib.provider_template_recipes import load_provider_templates

    for row in load_provider_templates():
        recipes = row['offering_recipes']
        if not recipes:
            assert row.get('category') == 'local'
            continue
        # Compile the FULL recipe set — the shape the world-catalog sync
        # uses. Selecting a single recipe would miss cross-recipe identity
        # collisions (one logical model per template; wire spellings belong
        # in request_ids).
        bundle = compile_provider_template_bundle(row['key'])
        assert bundle['offerings'], row['key']
        assert bundle['connections'][0]['base_url'], row['key']


def test_deepseek_and_meituan_anchor_flash_to_the_same_model_identity():
    bundles = {
        key: compile_provider_template_bundle(
            key, selected_model_ids=['deepseek-v4-flash'])
        for key in ('deepseek', 'meituan')
    }
    expected_ref = {'creator_id': 'deepseek', 'model_id': 'deepseek-v4-flash'}
    for bundle in bundles.values():
        assert bundle['offerings'][0]['model'] == expected_ref

    assert [row['wire_model_id'] for row in bundles['deepseek']['deployments']] == [
        'deepseek-v4-flash',
    ]
    assert sorted(
        row['wire_model_id'] for row in bundles['meituan']['deployments']
    ) == [
        'deepseek-v4-flash',
        'deepseek-v4-flash-huawei',
        'deepseek-v4-flash-meituan',
        'deepseek-v4-flash-tencent',
    ]


def test_compiled_offerings_never_exceed_the_profile_context_window():
    # normalize_document is fail-closed: an offering whose context_window
    # tops the registry model row rejects the whole save.  Registry rows
    # derive their window from context_profile whenever the provider
    # catalogue stays silent, so every compiled offering must clamp to the
    # profile window — no matter which sync path inserted the row first.
    from lib.model_info._context import context_profile
    from lib.provider_template_recipes import load_provider_templates

    for row in load_provider_templates():
        bundle = compile_provider_template_bundle(row['key'])
        for offering in bundle['offerings']:
            ref = offering.get('model')
            if not isinstance(ref, dict):
                continue  # pending_identity offerings carry no model row
            profile = context_profile(ref['model_id'])
            window = profile.get('window')
            if isinstance(window, int) and window > 0:
                assert offering['context_window'] <= window, (
                    row['key'], ref['model_id'],
                    offering['context_window'], window)


def test_bundle_model_rows_keep_the_recipe_context_window():
    # The clamp protects the offering invariant only; bundle MODEL rows feed
    # the world-catalogue registry and must keep the recipe's truthful
    # window (e.g. the Meituan-declared 256K for Doubao-Seed-1.6) instead of
    # the conservative profile estimate the offering clamps to.
    bundle = compile_provider_template_bundle(
        'meituan', selected_model_ids=['Doubao-Seed-1.6'])
    model = next(
        row for row in bundle['models'] if row['model_id'] == 'Doubao-Seed-1.6')
    assert model['context_window'] == 256000
    assert bundle['offerings'][0]['context_window'] == 128000


def test_tencent_hunyuan_recipes_confirm_official_tencent_identity():
    # The creator-family taxonomy knows Hunyuan ids: every tencent recipe
    # must compile to a confirmed offering (never provider-scoped pending),
    # or the models stay invisible to the 模型配置 world-catalogue registry.
    bundle = compile_provider_template_bundle('tencent')
    assert all(row['identity_state'] == 'confirmed'
               for row in bundle['offerings'])
    tencent_ids = {
        row['model']['model_id'] for row in bundle['offerings']
        if (row.get('model') or {}).get('creator_id') == 'tencent'
    }
    assert {'hy4-preview', 'hy3-preview', 'hunyuan-2.0-thinking-20251109'} \
        <= tencent_ids


def test_template_add_survives_registry_rows_with_different_pricing_tiers():
    """Regression for the TokenHub template-add rejection
    'offering claims capabilities absent from the official model: [cheap]'.

    The world-catalogue registry row for glm-5/glm-5.1 comes from the zhipu
    template at creator list prices (outside the cheap bracket), while
    TokenHub's subsidised price book earns the tag.  A pricing-tier tag is
    not an identity capability: merging existing-rows-win exactly like the
    settings frontend must normalize the full document.
    """
    from lib.model_routing.domain import empty_document, normalize_document
    from lib.model_routing.world_catalog import _world_catalog_rows

    bundle = compile_provider_template_bundle('tencent')
    creators, models = _world_catalog_rows()
    document = empty_document()
    document['creators'] = [dict(row) for row in creators]
    document['models'] = [dict(row) for row in models]
    known_creators = {row['creator_id'] for row in document['creators']}
    known_models = {
        (row['creator_id'], row['model_id']) for row in document['models']}
    document['creators'] += [
        row for row in bundle['creators']
        if row['creator_id'] not in known_creators]
    document['models'] += [
        row for row in bundle['models']
        if (row['creator_id'], row['model_id']) not in known_models]
    document['providers'].append(bundle['provider'])
    document['provider_accesses'].append(bundle['provider_access'])
    for key in ('connections', 'credentials', 'offerings', 'deployments'):
        document[key] += bundle[key]
    for credential in document['credentials']:
        credential['secret_reference'] = 'placeholder'
        credential['key_hint'] = ''

    normalized = normalize_document(document)
    glm51 = next(row for row in normalized['offerings']
                 if (row.get('model') or {}).get('model_id') == 'glm-5.1')
    assert 'cheap' in glm51['capabilities']
    registry = next(row for row in normalized['models']
                    if row['model_id'] == 'glm-5.1')
    assert 'cheap' not in registry['capabilities']
