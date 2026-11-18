"""Text-pure section reads are memoized per Turn and stay byte-identical."""
from __future__ import annotations

import json

import pytest

from lib.llm.body import build_body
from lib.llm_sanitize import context_sections as sections
from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context

pytestmark = pytest.mark.unit


def _envelope(block_id, body):
    return (f'<!-- tofu-context:{block_id}:start -->\n{body}\n'
            f'<!-- tofu-context:{block_id}:end -->')


def test_scope_returns_the_scan_and_reuses_it_until_two_rounds_pass():
    text = 'lead ' + _envelope('user_context', 'a') + ' mid ' + _envelope(' padded ', 'b')
    expected = sections._scan_sections(text)
    assert sections.text_sections(text) == expected
    assert expected[1][0] == ' padded ' and expected[1][1] == 'padded'
    with sections.text_memo_scope():
        first = sections.text_sections(text)
        assert first == expected
        assert sections.text_sections(text) is first
        sections.next_text_memo_round()
        assert sections.text_sections(text) is first
        sections.next_text_memo_round()
        sections.next_text_memo_round()
        memo = sections._TEXT_MEMO.get()
        assert not memo.current and not memo.previous
    assert sections._TEXT_MEMO.get() is None


def test_equal_text_built_separately_hits_with_the_same_result():
    body = 'x' * 100
    left = _envelope('memory', body)
    right = ''.join(['<!-- tofu-context:memory:start -->\n', body,
                     '\n<!-- tofu-context:memory:end -->'])
    assert left == right and left is not right
    with sections.text_memo_scope():
        assert sections.text_sections(left) is sections.text_sections(right)


def _script(rounds):
    """A Turn's context preparation and request builds; every observable byte."""
    observed = []
    legacy = []
    render_context(legacy, [context_block('parallel_execution', 'PARALLEL')],
                   ComposeRequest(model='m'))
    legacy[0]['role'] = 'user'
    messages = [legacy[0], {'role': 'user', 'content': 'request'}]
    for number in range(rounds):
        if number:
            sections.next_text_memo_round()
        result = render_context(messages, [
            context_block('parallel_execution', 'PARALLEL'),
            context_block('user_context', f'preference {number // 2}'),
        ], ComposeRequest(model='m'))
        render_context(messages, [context_block('current_date', 'Current date: 2026-11-17')],
                       ComposeRequest(model='m'))
        body = build_body('claude-sonnet-4', messages, max_tokens=1024,
                          provider_id='test', precomputed_input_tokens=100,
                          context_window=200000)
        observed.append(json.dumps(
            {'messages': messages, 'manifest': result.manifest,
             'body': body},
            sort_keys=True, default=repr))
        messages.append({'role': 'assistant', 'content': f'answer {number}'})
        messages.append({'role': 'user', 'content': f'next {number}'})
    return observed


def test_memoized_preparation_is_byte_identical_to_the_unmemoized_one(monkeypatch):
    scans = []
    scan = sections._scan_sections
    monkeypatch.setattr(sections, '_scan_sections', lambda text: scans.append(text) or scan(text))
    plain = _script(5)
    plain_scans, scans[:] = len(scans), []
    with sections.text_memo_scope():
        memoized = _script(5)
    assert memoized == plain
    assert 'PARALLEL' in plain[0] and 'preference 2' in plain[-1]
    assert len(scans) == len(set(scans)) < plain_scans
