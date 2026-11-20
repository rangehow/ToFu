"""All installed provider wires share the same ordered-history comparison."""
from copy import deepcopy

import pytest

from lib.llm.request import seal_request
from lib.llm.request_prefix import compare_request_prefix

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('protocol', 'history_field', 'controls'), [
    ('responses', 'input', {'instructions': 'system'}),
    ('anthropic', 'messages', {'system': [{'type': 'text', 'text': 'system'}]}),
    ('openai', 'messages', {'tools': [{'type': 'function'}]}),
])
def test_sealed_requests_share_prefix_comparison(protocol, history_field, controls):
    first = {'role': 'user', 'content': 'first'}
    next_item = {'role': 'user', 'content': 'next'}
    previous = {'model': 'sample-model', **controls, history_field: [first]}
    current = {**previous, history_field: [first, next_item]}

    def compare(old, new, **options):
        seal_options = dict(model='sample-model', protocol=protocol,
                            input_tokens=1, semantic_id='test')
        return compare_request_prefix(
            seal_request(old, **seal_options), seal_request(new, **seal_options),
            protocol=protocol, **options)

    verdict = compare(previous, current)
    assert not verdict.full_reason
    assert verdict.suffix == [next_item]
    assert compare(previous, {**current, 'model': 'another-model'}).full_reason == (
        'request_fields_changed')
    assert compare(previous, {**current, history_field: []}).full_reason == (
        'history_shortened')

    edited = deepcopy(current)
    edited[history_field][0]['content'] = 'edited'
    assert compare(previous, edited).full_reason == 'history_changed'
    assert compare(previous, {**current, 'stream': True}).full_reason == (
        'request_fields_changed')
    ignored = compare(previous, {**current, 'stream': True},
                      variable_fields=frozenset({'stream'}))
    assert not ignored.full_reason
    assert ignored.suffix == [next_item]
