"""Declared gateway request shapes on the Anthropic wire (lib.llm.gateway_shape).

A gateway profile may require a client identity at the head of the system
prompt and a working-directory header. Every gateway here is synthetic; the
CatPaw declaration is pinned in bundles/yourprovider/tests/test_catpaw_gateway_profile.py.
"""
import json

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm.gateway_shape import apply_gateway_anthropic_shape, gateway_anthropic_headers
from tests.support.gateway_profiles import install_gateway_profiles

pytestmark = pytest.mark.unit

GATEWAY = 'https://identity-gateway.example.test/v1'
IDENTITY = 'You are Example CLI, the vendor client.'


@pytest.fixture(autouse=True)
def identity_gateway(monkeypatch):
    install_gateway_profiles(monkeypatch, {
        'id': 'test-identity', 'hosts': ['identity-gateway.example.test'],
        'traits': {'anthropic_system_identity': {'text': IDENTITY,
                                                 'accepted_prefix': 'you are example cli'},
                   'working_directory_header': 'X-Working-Dir'}})


def shape(body, base_url=GATEWAY):
    return apply_gateway_anthropic_shape(body, base_url)


def test_undeclared_hosts_are_left_alone():
    for url in ('https://api.anthropic.com/v1', 'https://identity-gateway.example.test.evil.com',
                '', None):
        assert shape({'system': 'Tofu.'}, url) == {'system': 'Tofu.'}
        assert gateway_anthropic_headers(url) == {}


def test_missing_system_becomes_identity():
    assert shape({'messages': []})['system'] == IDENTITY


def test_string_system_is_prefixed_not_replaced():
    system = shape({'system': 'Tofu system prompt.'})['system']
    assert system == f'{IDENTITY}\n\nTofu system prompt.'


def test_existing_identity_is_untouched_case_insensitively():
    for original in (IDENTITY + '\n\nExtra.', IDENTITY.lower()):
        assert shape({'system': original})['system'] == original


def test_block_system_gets_identity_block_prepended():
    blocks = [{'type': 'text', 'text': 'Tofu block.', 'cache_control': {'type': 'ephemeral'}}]
    system = shape({'system': list(blocks)})['system']
    assert system == [{'type': 'text', 'text': IDENTITY}, *blocks]
    already = [{'type': 'text', 'text': IDENTITY}, {'type': 'text', 'text': 'Tofu block.'}]
    assert shape({'system': list(already)})['system'] == already


def test_openai_translation_pipeline_is_gateway_legal():
    body = shape(openai_body_to_anthropic({'model': 'kimi-k3', 'messages': [
        {'role': 'system', 'content': 'You are Tofu.'}, {'role': 'user', 'content': 'hi'}]}))
    first = body['system'] if isinstance(body['system'], str) else body['system'][0]['text']
    assert first.lower().startswith('you are example cli')
    assert 'You are Tofu.' in json.dumps(body['system'])
    bare = shape(openai_body_to_anthropic({'model': 'kimi-k3',
                                           'messages': [{'role': 'user', 'content': 'hi'}]}))
    assert bare['system'] == IDENTITY


def test_working_directory_header_is_present_and_nonempty():
    headers = gateway_anthropic_headers(GATEWAY)
    assert list(headers) == ['X-Working-Dir'] and headers['X-Working-Dir'].strip()


def test_malformed_identity_declarations_are_refused(monkeypatch):
    install_gateway_profiles(monkeypatch, {
        'id': 'bad', 'hosts': ['identity-gateway.example.test'],
        'traits': {'anthropic_system_identity': {'text': 'Hello', 'accepted_prefix': 'other'},
                   'working_directory_header': 'Bad Header'}})
    assert shape({'system': 'Tofu.'}) == {'system': 'Tofu.'}
    assert gateway_anthropic_headers(GATEWAY) == {}
