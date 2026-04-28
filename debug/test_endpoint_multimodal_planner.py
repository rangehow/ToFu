#!/usr/bin/env python3
"""Regression test: endpoint planner wrapper must handle BOTH plain-string
and multimodal-list user content without raising.

Reproduces the crash fixed in lib/tasks_pkg/endpoint_review.py:

    TypeError: can only concatenate str (not "list") to str

Run: ``python debug/test_endpoint_multimodal_planner.py``
"""

from __future__ import annotations

import os
import sys

# Ensure project root on sys.path when invoked directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.tasks_pkg import endpoint_review


def _make_stub_run_single_turn():
    """Return a stub that records the last messages it was called with and
    mimics the real ``_run_single_turn`` contract (returns dict with
    content/usage/messages/error keys)."""
    captured = {'last_messages': None}

    def stub(task, messages_override=None):
        captured['last_messages'] = messages_override
        return {
            'content': 'stub plan',
            'thinking': '',
            'usage': {'total_tokens': 0},
            'messages': messages_override or [],
            'error': None,
        }

    return stub, captured


def _find_wrapped_user(messages):
    """Return the last user message in a planner_messages list."""
    for m in reversed(messages):
        if m.get('role') == 'user':
            return m
    return None


def test_string_content():
    """Plain-text user content — must still produce a ``str`` content
    that starts with the planner wrapper + ends with the original text.
    """
    stub, captured = _make_stub_run_single_turn()
    endpoint_review._run_single_turn = stub  # monkey-patch

    task = {'id': 'test-str-1234567890'}
    messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'Hello world, please help.'},
    ]
    result = endpoint_review._run_planner_turn(task, messages)
    assert result['error'] is None, f'planner returned error: {result["error"]}'

    wrapped = _find_wrapped_user(captured['last_messages'])
    assert wrapped is not None, 'no wrapped user message'
    content = wrapped['content']
    assert isinstance(content, str), f'expected str, got {type(content).__name__}'
    assert content.startswith('=== Your role for THIS turn: Planner ==='), \
        'missing planner role prefix'
    assert content.endswith('Hello world, please help.'), \
        'original user text not preserved at tail'
    assert '───── User request ─────' in content, \
        'missing user-request separator'
    print('✅ string-content path: ok (len=%d)' % len(content))


def test_list_content_with_image():
    """Multimodal list content (text + image_url block) — must NOT raise
    and must preserve the original blocks."""
    stub, captured = _make_stub_run_single_turn()
    endpoint_review._run_single_turn = stub

    task = {'id': 'test-list-1234567890'}
    original_blocks = [
        {'type': 'text', 'text': 'Look at this screenshot.'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,XXX'}},
    ]
    messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': original_blocks},
    ]
    result = endpoint_review._run_planner_turn(task, messages)
    assert result['error'] is None, f'planner returned error: {result["error"]}'

    wrapped = _find_wrapped_user(captured['last_messages'])
    assert wrapped is not None, 'no wrapped user message'
    content = wrapped['content']
    assert isinstance(content, list), f'expected list, got {type(content).__name__}'
    # First block must be the planner wrapper text.
    assert content[0].get('type') == 'text', 'first block not text'
    assert content[0]['text'].startswith('=== Your role for THIS turn: Planner ==='), \
        'first block missing planner prefix'
    # Original blocks must follow, in original order.
    assert content[1:] == original_blocks, \
        'original blocks not preserved verbatim'
    # Sanity: image block still present.
    assert any(b.get('type') == 'image_url' for b in content), \
        'image_url block dropped'
    print('✅ list-content path: ok (%d blocks after wrap)' % len(content))


def test_empty_list_content():
    """Edge case: empty list → fall back to string path with empty body."""
    stub, captured = _make_stub_run_single_turn()
    endpoint_review._run_single_turn = stub

    task = {'id': 'test-empty-1234567890'}
    messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': []},
    ]
    result = endpoint_review._run_planner_turn(task, messages)
    assert result['error'] is None

    wrapped = _find_wrapped_user(captured['last_messages'])
    content = wrapped['content']
    assert isinstance(content, str), \
        f'empty-list should fall back to str, got {type(content).__name__}'
    assert content.startswith('=== Your role for THIS turn: Planner ==='), \
        'missing planner prefix on empty-list fallback'
    print('✅ empty-list path: ok (len=%d)' % len(content))


def main():
    test_string_content()
    test_list_content_with_image()
    test_empty_list_content()
    print('\nAll endpoint multimodal-planner tests passed. ✅')


if __name__ == '__main__':
    main()
