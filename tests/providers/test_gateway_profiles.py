"""Overlay data discovery and declared gateway quirks (docs/PUBLIC_CORE.md).

``tofu_extensions`` finds bundle roots (in-tree and ``tofu.bundles`` entry
points); ``lib.provider.gateway_profiles`` admits closed-world traits and
answers lookups by host or provider id. Every gateway here is synthetic.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tofu_extensions
from lib.provider import gateway_profiles as gp
from tests.support.gateway_profiles import install_gateway_profiles

pytestmark = pytest.mark.unit


@pytest.fixture
def bundles(tmp_path, monkeypatch):
    in_tree = tmp_path / 'bundles'
    (in_tree / 'acme' / 'gateway_profiles').mkdir(parents=True)
    (in_tree / 'not-a-bundle.txt').write_text('ignored')
    installed = tmp_path / 'site-packages' / 'tofu_globex' / 'bundle'
    (installed / 'gateway_profiles').mkdir(parents=True)
    entries = [
        SimpleNamespace(name='globex', load=lambda: (lambda: installed)),
        SimpleNamespace(name='acme-editable', load=lambda: str(in_tree / 'acme')),
        SimpleNamespace(name='broken', load=lambda: (_ for _ in ()).throw(ImportError('x'))),
        SimpleNamespace(name='missing', load=lambda: tmp_path / 'absent'),
    ]
    monkeypatch.setattr(tofu_extensions, 'IN_TREE_BUNDLES_DIR', in_tree)
    monkeypatch.setattr('importlib.metadata.entry_points',
                        lambda group: entries if group == 'tofu.bundles' else [])
    tofu_extensions.clear_bundle_cache()
    gp.clear_gateway_profile_cache()
    yield SimpleNamespace(acme=in_tree / 'acme', globex=installed)
    tofu_extensions.clear_bundle_cache()
    gp.clear_gateway_profile_cache()


def _write_profile(directory, name, document):
    (directory / 'gateway_profiles' / f'{name}.json').write_text(json.dumps(
        {'contract': gp.GATEWAY_PROFILE_CONTRACT, **document}), encoding='utf-8')


def test_bundle_roots_merge_in_tree_and_installed_without_duplicates(bundles):
    roots = tofu_extensions.bundle_roots()
    assert roots == [bundles.acme, bundles.globex]
    assert tofu_extensions.bundle_directories('gateway_profiles') == [
        bundles.acme / 'gateway_profiles', bundles.globex / 'gateway_profiles']
    assert tofu_extensions.bundle_directories('site_packs') == []
    assert tofu_extensions.is_bundle_path(bundles.globex / 'gateway_profiles' / 'x.json')
    assert not tofu_extensions.is_bundle_path(tofu_extensions.IN_TREE_BUNDLES_DIR.parent)


def test_profiles_load_in_bundle_order_and_reject_malformed_documents(bundles):
    _write_profile(bundles.acme, 'acme', {
        'id': 'acme', 'hosts': ['api.acme.test'], 'provider_ids': ['acme'],
        'traits': {'image_generation_api': 'friday_async', 'invented_trait': True,
                   'anthropic_claude_tool_name_budget_bytes': -1}})
    _write_profile(bundles.globex, 'globex', {
        'id': 'globex', 'hosts': ['.globex.test'], 'provider_ids': ['globex*'],
        'traits': {'blocked_term_sanitize': True}})
    (bundles.globex / 'gateway_profiles' / 'wrong.json').write_text(
        json.dumps({'contract': 'other/v1', 'id': 'x', 'traits': {}}))
    (bundles.globex / 'gateway_profiles' / 'broken.json').write_text('{not json')

    profiles = gp.gateway_profiles()
    assert [profile['id'] for profile in profiles] == ['acme', 'globex']
    assert dict(profiles[0]['traits']) == {'image_generation_api': 'friday_async'}


def test_trait_lookup_matches_exact_hosts_dot_suffixes_and_id_prefixes(monkeypatch):
    install_gateway_profiles(monkeypatch, {
        'id': 'acme', 'hosts': ['api.acme.test'], 'provider_ids': ['acme'],
        'traits': {'anthropic_claude_tool_name_budget_bytes': 300}}, {
        'id': 'globex', 'hosts': ['.globex.test'], 'provider_ids': ['globex*'],
        'traits': {'blocked_term_sanitize': True,
                   'anthropic_claude_tool_name_budget_bytes': 200}})
    trait = gp.gateway_trait
    assert trait('anthropic_claude_tool_name_budget_bytes', base_url='https://api.acme.test/v1') == 300
    assert trait('anthropic_claude_tool_name_budget_bytes', base_url='https://x.api.acme.test') is None
    assert trait('anthropic_claude_tool_name_budget_bytes', base_url='https://eu.globex.test') == 200
    assert trait('blocked_term_sanitize', base_url='https://globex.test') is True
    assert trait('blocked_term_sanitize', base_url='https://evilglobex.test') is None
    assert trait('blocked_term_sanitize', provider_id='GLOBEX_anthropic') is True
    assert trait('blocked_term_sanitize', provider_id='acme') is None
    assert trait('blocked_term_sanitize', base_url='http://[::1') is None
    with pytest.raises(KeyError):
        trait('invented_trait', base_url='https://api.acme.test')


def test_no_profile_means_plain_provider_behavior(monkeypatch):
    install_gateway_profiles(monkeypatch)
    from lib.llm.tool_wire import provider_tool_name_budget_bytes
    from lib.llm_sanitize import gateway_blocked_term_sanitize

    assert provider_tool_name_budget_bytes(
        api_protocol='anthropic', base_url='https://api.acme.test', model='claude-opus-5') == 0
    assert gateway_blocked_term_sanitize('acme') is False


def test_declared_claude_cache_multipliers_install_exact_provider_rows(monkeypatch):
    from lib.pricing import _provider

    install_gateway_profiles(monkeypatch, {
        'id': 'acme', 'provider_ids': ['acme-gw', 'acme-family*'],
        'traits': {'claude_cache_multipliers': {'cacheWriteMul': 1.5, 'cacheReadMul': 0.2}}})
    monkeypatch.setattr(_provider, 'PROVIDER_PRICING', _provider.RevisionedTable())
    _provider._install_gateway_claude_cache_pricing()

    assert set(_provider.PROVIDER_PRICING) == {'acme-gw'}
    rows = _provider.PROVIDER_PRICING['acme-gw']
    assert rows and all(row['cacheReadMul'] == 0.2 and row['cacheWriteMul'] == 1.5
                        for row in rows.values())
    from lib.model_info._family import is_claude
    assert all(is_claude(model_id) for model_id in rows)
