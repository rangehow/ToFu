"""Executable spec for fetch_url extract mode (Gemini web_fetch inspiration).

Pins:
- extract_answer answers against the fetched page via one bounded model pass
  (temperature 0, thinking off, capped excerpt);
- bad inputs and empty model replies raise (fail-soft belongs to the caller);
- the serial handler returns the extraction when it succeeds, and degrades to
  the raw page — never losing fetched content — when extraction fails.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.search_extract as sx


def _patch_dispatch(monkeypatch, reply='The answer is 42.'):
    def fake_dispatch(messages, *, on_content=None, **kwargs):
        if on_content:
            on_content(reply)
        return object()
    monkeypatch.setattr(sx, 'dispatch_stream', fake_dispatch)
    monkeypatch.setattr(
        'lib.llm.stream_result.require_verified_provider_stream_result',
        lambda stream, context='': type('SR', (), {'message': {}})())


@pytest.mark.unit
class TestExtractAnswer:
    def test_happy_path_grounded_and_bounded(self, monkeypatch):
        _patch_dispatch(monkeypatch)
        captured = {}

        def spy(messages, *, on_content=None, **kwargs):
            captured['messages'] = messages
            captured['kwargs'] = kwargs
            if on_content:
                on_content('ans')
            return object()
        monkeypatch.setattr(sx, 'dispatch_stream', spy)

        out = sx.extract_answer('what is the answer?', 'https://x.test/p',
                                'page body ' * 100)
        assert out == 'ans'
        assert captured['kwargs']['temperature'] == 0.0
        assert captured['kwargs']['thinking_enabled'] is False
        user = captured['messages'][1]['content']
        assert 'https://x.test/p' in user
        assert 'what is the answer?' in user
        assert 'page body' in user

    def test_excerpt_capped(self, monkeypatch):
        _patch_dispatch(monkeypatch)
        captured = {}

        def spy(messages, *, on_content=None, **kwargs):
            captured['len'] = len(messages[1]['content'])
            if on_content:
                on_content('ok')
            return object()
        monkeypatch.setattr(sx, 'dispatch_stream', spy)

        sx.extract_answer('q', 'https://x.test', 'z' * 500_000)
        assert captured['len'] < 500_000
        assert captured['len'] <= sx._MAX_PAGE_EXCERPT_CHARS + 2_000

    def test_empty_question_raises(self):
        with pytest.raises(ValueError):
            sx.extract_answer('   ', 'https://x.test', 'body')

    def test_empty_page_raises(self):
        with pytest.raises(ValueError):
            sx.extract_answer('q', 'https://x.test', '')

    def test_empty_model_reply_raises(self, monkeypatch):
        _patch_dispatch(monkeypatch, reply='   ')
        with pytest.raises(ValueError):
            sx.extract_answer('q', 'https://x.test', 'body')


def _make_task():
    return {'id': 'extract-test', 'aborted': False,
            'lastUserQuery': 'q', '_tool_result_cache': {},
            '_userId': '41', 'config': {}}


def _ok_item(url):
    return {'url': url, 'page_content': 'raw page body', 'is_pdf': False,
            'raw_chars': 100, 'filtered_chars': 14, 'error_msg': None,
            'saved_path': None, 'is_asset': False}


@pytest.mark.unit
class TestFetchUrlExtractHandler:

    def _call(self, monkeypatch, args, item):
        import lib.tasks_pkg.handlers.search._core as core
        import lib.tasks_pkg.handlers.search._handlers as handlers
        monkeypatch.setattr(core, '_fetch_url_one', lambda *_a, **_k: item)
        monkeypatch.setattr(
            handlers, '_finalize_tool_round', lambda *_a, **_k: None)
        round_entry = {}
        tc_id, content, is_read = handlers._handle_fetch_url(
            _make_task(), {}, 'fetch_url', 'tc-ex', args, 1,
            round_entry, {}, '', False)
        return content, round_entry

    def test_extract_success_returns_answer(self, monkeypatch):
        url = 'https://x.test/page'
        monkeypatch.setattr(
            sx, 'extract_answer', lambda q, u, body: f'A: {q} [{u}]')
        content, round_entry = self._call(
            monkeypatch, {'url': url, 'extract': 'main point?'}, _ok_item(url))
        assert content == f'Extract from {url} — main point?\n\nA: main point? [{url}]'
        assert round_entry['extractMode'] is True

    def test_extract_failure_degrades_to_raw(self, monkeypatch):
        url = 'https://x.test/page'

        def boom(q, u, body):
            raise RuntimeError('no provider configured')
        monkeypatch.setattr(sx, 'extract_answer', boom)
        content, round_entry = self._call(
            monkeypatch, {'url': url, 'extract': 'main point?'}, _ok_item(url))
        assert 'extract mode unavailable: no provider configured' in content
        assert 'raw page body' in content
        assert 'extractMode' not in round_entry

    def test_extract_on_failed_fetch_stays_failure(self, monkeypatch):
        url = 'https://x.test/dead'
        fail = {'url': url, 'page_content': None, 'is_pdf': False,
                'raw_chars': 0, 'filtered_chars': 0,
                'error_msg': 'soft_blocked', 'saved_path': None,
                'is_asset': False}
        called = []
        monkeypatch.setattr(
            sx, 'extract_answer', lambda *a: called.append(a) or 'x')
        content, _ = self._call(
            monkeypatch, {'url': url, 'extract': 'q'}, fail)
        assert content.startswith('Failed to fetch')
        assert called == [], 'extract must not run without page content'
