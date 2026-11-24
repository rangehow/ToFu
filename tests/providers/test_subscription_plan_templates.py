"""Domestic coding-plan subscriptions as plan-key provider templates.

Each plan is a vendor's own plan-bound coding endpoint reached with a plan
API key. The templates are the single roster source for Settings →
Subscription sign-in's plan cards and for the Kimi Code OAuth provider; the
specs pin the documented endpoints and the boundary that plan limits are
Offering limits, never Model facts.
"""

from __future__ import annotations

import pytest

from lib.model_routing.model_defaults import official_model_directory
from lib.provider.provider_template_recipes import (
    compile_provider_template_bundle, load_provider_templates,
    normalize_provider_template,
)

pytestmark = pytest.mark.unit

PLANS = {
    'kimi_code': ('https://api.kimi.com/coding/v1',
                  'https://www.kimi.com/code/console'),
    'bailian_coding_plan': ('https://coding.dashscope.aliyuncs.com/v1',
                            'https://bailian.console.aliyun.com/cn-beijing/subscription/coding-plan'),
    'tencent_coding_plan': ('https://api.lkeap.cloud.tencent.com/coding/v3',
                            'https://console.cloud.tencent.com/tokenhub/codingplan'),
    'ark_coding_plan': ('https://ark.cn-beijing.volces.com/api/coding/v3',
                        'https://ark.volcengine.com/region:cn-beijing/apikey'),
    'minimax_mplan': ('https://api.minimax.cn/v1',
                      'https://platform.minimax.cn/console/plan'),
    'opencode_go': ('https://opencode.ai/inference/go/openai/v1',
                    'https://opencode.ai/console/go'),
}


@pytest.fixture(scope='module')
def templates():
    return {row['key']: row for row in load_provider_templates()}


def test_subscription_is_an_accepted_template_category():
    template = normalize_provider_template({
        'key': 'plan', 'category': 'subscription', 'base_url': 'https://x.test/v1',
        'subscription': {'key_console_url': 'https://x.test/console'},
        'offering_recipes': [{'model_id': 'm'}]})
    assert template['category'] == 'subscription'


@pytest.mark.parametrize('changes', [
    {'subscription': None},
    {'subscription': {'key_console_url': 'http://x.test/console'}},
    {'subscription': {'key_console_url': 'https://x.test/c', 'extra': 1}},
    {'category': 'relay'},
])
def test_subscription_presentation_is_validated(changes):
    from lib.provider.provider_template_recipes import ProviderTemplateRecipeError
    raw = {'key': 'plan', 'category': 'subscription', 'base_url': 'https://x.test/v1',
           'subscription': {'key_console_url': 'https://x.test/console'},
           'offering_recipes': [{'model_id': 'm'}], **changes}
    if raw['subscription'] is None:
        raw.pop('subscription')
    with pytest.raises(ProviderTemplateRecipeError):
        normalize_provider_template(raw)


@pytest.mark.parametrize('key', sorted(PLANS))
def test_plan_template_names_the_documented_endpoint_and_key_console(templates, key):
    base_url, console = PLANS[key]
    template = templates[key]
    assert template['category'] == 'subscription'
    assert template['base_url'] == base_url
    assert template['subscription']['key_console_url'] == console
    assert template['catalog_source']['url'].startswith('https://')
    assert template['offering_recipes']


@pytest.mark.parametrize('key', sorted(PLANS))
def test_plan_bundle_is_an_api_key_access_on_the_plan_endpoint(templates, key):
    bundle = compile_provider_template_bundle(key)
    assert [row['base_url'] for row in bundle['connections']] == [PLANS[key][0]]
    assert {row['kind'] for row in bundle['credentials']} == {'api_key'}
    assert all(row['probe_status'] == 'unprobed' for row in bundle['deployments'])
    # A flat subscription carries no per-token transaction price.
    assert not any('actual_pricing' in row for row in bundle['offerings'])


def test_kimi_code_maps_plan_wire_ids_onto_registered_models():
    bundle = compile_provider_template_bundle('kimi_code')
    offerings = {row['offering_id']: row for row in bundle['offerings']}
    by_wire = {row['wire_model_id']: offerings[row['offering_id']]
               for row in bundle['deployments']}
    assert set(by_wire) == {'k3.1-pro', 'k3-256k', 'k3', 'kimi-for-coding',
                            'kimi-for-coding-highspeed', 'k3.1-pro-highspeed'}
    k31 = by_wire['k3.1-pro']
    assert k31['identity_state'] == 'confirmed'
    assert k31['model']['model_id'] == 'kimi-k3.1-pro'
    # Plus members get K3.1 Pro only up to 256K; Pro members may raise it.
    assert k31['context_window'] == 262144
    k3 = by_wire['k3-256k']
    assert k3 is by_wire['k3'] and k3['model']['model_id'] == 'kimi-k3'
    # Unregistered identities arrive as fact-less Model rows, which shared
    # admission keeps as non-callable drafts until sourced facts exist.
    models = {row['model_id']: row for row in bundle['models']}
    for wire, model_id in (('kimi-for-coding', 'kimi-k2.8-preview'),
                           ('k3.1-pro-highspeed', 'kimi-k3.1-pro-highspeed')):
        assert by_wire[wire]['model']['model_id'] == model_id
        assert not models[model_id].get('registration_metadata')
        assert models[model_id].get('list_pricing') is None


def test_tencent_plan_keeps_the_documented_lowercase_wire_for_minimax():
    bundle = compile_provider_template_bundle('tencent_coding_plan')
    offerings = {row['offering_id']: row for row in bundle['offerings']}
    wire = next(row for row in bundle['deployments']
                if row['wire_model_id'] == 'minimax-m2.5')
    offering = offerings[wire['offering_id']]
    assert offering['context_window'] == 196608
    assert wire['max_output_tokens'] == 32768


def test_opencode_go_pins_the_go_gateway_and_session_carrier(templates):
    bundle = compile_provider_template_bundle('opencode_go')
    connection = bundle['connections'][0]
    assert (connection['base_url'], connection['protocol']) == (
        'https://opencode.ai/inference/go/openai/v1', 'openai')
    assert (connection['inference_profile']['features']
            ['cache_affinity_opencode_session'] == 'supported')
    offerings = {row['offering_id']: row for row in bundle['offerings']}
    by_wire = {row['wire_model_id']: offerings[row['offering_id']]
               for row in bundle['deployments']}
    # The Go plan's published wire IDs, including lower-case spellings of
    # creator identities and contributor SKUs mapped onto the public Model.
    assert {'deepseek-v4.1-flash', 'deepseek-v4-pro', 'glm-5.3',
            'gpt-5.6-luna', 'gpt-6-luna', 'claude-haiku-5-5',
            'kimi-k3', 'kimi-k3.1-pro', 'minimax-m3', 'longcat-2.5',
            'muse-spark-1.3-contributor'} <= set(by_wire)
    assert by_wire['minimax-m3']['model']['model_id'] == 'MiniMax-M3'
    assert by_wire['longcat-2.5']['model']['model_id'] == 'LongCat-2.5'
    assert (by_wire['muse-spark-1.3-contributor']['model']['model_id']
            == 'muse-spark-1.3')
    # Go publishes only max for Kimi K3; the route must not offer a rung the
    # plan does not list.
    assert by_wire['kimi-k3']['inference_profile']['reasoning'][
        'supported_efforts'] == ['max']

    from lib.llm.route_features import route_feature
    from lib.model_routing.inference import resolve_inference_profile
    deployment = next(row for row in bundle['deployments']
                      if row['wire_model_id'] == 'kimi-k3')
    profile = resolve_inference_profile(
        offering=by_wire['kimi-k3'], connection=connection,
        deployment=deployment)
    assert route_feature(
        profile, 'cache_affinity_opencode_session', protocol='openai') is True



@pytest.mark.parametrize('key', sorted(PLANS))
def test_plan_offerings_carry_no_price_tier(templates, key):
    # A flat plan has no per-token price, and the tier would enrol plan models
    # in the unpinned background pools (auto titles request 'cheap').
    from lib.llm_dispatch.config._pricing import MANAGED_TIER_TAGS
    assert not any(MANAGED_TIER_TAGS & set(row.get('capabilities') or [])
                   for row in templates[key]['offering_recipes'])
    bundle = compile_provider_template_bundle(key)
    assert not any(MANAGED_TIER_TAGS & set(row['capabilities'])
                   for row in bundle['offerings'])
    assert all('text' in row['capabilities'] for row in bundle['offerings'])


def test_subscription_templates_never_author_model_facts(templates):
    rows = {(row['creator_id'], row['model_id']): row
            for row in official_model_directory(list(templates.values()))}
    # The plan's 256K cap stays an Offering limit: the Model keeps its window.
    assert rows[('moonshot', 'kimi-k3.1-pro')]['context_window'] == 1048576
    assert not any(model_id in {'kimi-k2.8-preview', 'kimi-k3.1-pro-highspeed',
                                'doubao-seed-evolving', 'MiniMax-M3.1-Flash'}
                   for _creator, model_id in rows)
