"""Executable contract for the dynamic provider-template feed.

The feed folds the public OpenRouter model catalog into the onboarding
template picker (lib/provider_template_feed.py).  Safety rails pinned here:
authored recipes win every conflict, retired model ids stay dead, dated /
billing variants are never imported, every failure mode keeps the authored
catalog, and the loader never blocks on the network.
"""

from __future__ import annotations

import os
import time

import pytest

import lib.provider_template_feed as feed_mod
from lib.provider_template_feed import (
    load_cached_feed,
    maybe_refresh_async,
    merge_feed_recipes,
    recipes_from_catalog,
    refresh_now,
)
from lib.provider_template_recipes import load_provider_templates

pytestmark = pytest.mark.unit


def _catalog_row(full_id, *, context=262144, prompt='2e-7',
                 completion='8e-7', inputs=('text',), outputs=('text',),
                 params=('reasoning',)):
    return {
        'id': full_id,
        'context_length': context,
        'pricing': {'prompt': prompt, 'completion': completion},
        'architecture': {
            'input_modalities': list(inputs),
            'output_modalities': list(outputs),
        },
        'supported_parameters': list(params),
    }


class _FakeResponse:
    def __init__(self, payload, *, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


def _write_cache(templates, *, fetched_at=None):
    os.makedirs(os.path.dirname(feed_mod._CACHE_PATH), exist_ok=True)
    from lib.json_store import write_json_atomic
    write_json_atomic(feed_mod._CACHE_PATH, {
        'version': 1,
        'source': 'test',
        'fetched_at': time.time() if fetched_at is None else fetched_at,
        'templates': templates,
    })


@pytest.fixture
def feed_enabled(monkeypatch):
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '1')
    yield
    # Never leak an in-flight flag into a later test.
    with feed_mod._refresh_lock:
        feed_mod._refresh_in_flight = False


# ── Catalog → recipes ──────────────────────────────────────────────

def test_catalog_rows_map_to_template_recipes():
    out = recipes_from_catalog([
        _catalog_row('deepseek/deepseek-v5-flash'),
        _catalog_row('openai/gpt-6-vision', inputs=('text', 'image'),
                     params=()),
    ])
    deepseek_ids = [row['model_id'] for row in out['deepseek']]
    assert deepseek_ids == ['deepseek-v5-flash']
    row = out['deepseek'][0]
    assert row['context_window'] == 262144
    assert row['pricing'] == {
        'input': 0.2, 'output': 0.8, 'currency': 'USD',
        'unit': 'per_million_tokens',
    }
    assert {'text', 'thinking'} <= set(row['capabilities'])
    # Tier tags come from real pricing rows: $0.2/$0.8 is strictly cheaper
    # than the Sonnet reference bracket on both axes.
    assert 'cheap' in row['capabilities']
    vision = out['openai'][0]
    assert 'vision' in vision['capabilities']
    # Templates with no rows in the catalog are absent, not empty.
    assert 'qwen' not in out


def test_catalog_rows_drop_dated_and_billing_variants():
    out = recipes_from_catalog([
        _catalog_row('deepseek/deepseek-v5-flash'),
        _catalog_row('deepseek/deepseek-v5-flash-0801'),
        _catalog_row('deepseek/deepseek-v5-flash:free'),
        _catalog_row('openai/gpt-5-2025-08-07'),
    ])
    assert [row['model_id'] for row in out['deepseek']] == [
        'deepseek-v5-flash']
    assert 'openai' not in out


def test_catalog_rows_without_pricing_get_no_tier_tag():
    out = recipes_from_catalog([
        _catalog_row('deepseek/deepseek-v5-free', prompt='0', completion='0'),
    ])
    row = out['deepseek'][0]
    assert 'pricing' not in row
    assert 'cheap' not in row['capabilities']


# ── Merge ──────────────────────────────────────────────────────────

def _authored_template():
    return {
        'key': 'deepseek',
        'name': 'DeepSeek',
        'category': 'official',
        'retired_model_ids': ['deepseek-chat', 'deepseek-reasoner'],
        'offering_recipes': [
            {'model_id': 'deepseek-v4-pro', 'capabilities': ['text']},
        ],
    }


def test_merge_authored_wins_and_retired_stay_dead(feed_enabled):
    template = _authored_template()
    feed = {'templates': {'deepseek': [
        # Same model_id as the authored row: curated rpm/caps win.
        {'model_id': 'deepseek-v4-pro', 'capabilities': ['text', 'vision']},
        # Retired aliases must never be resurrected by the feed.
        {'model_id': 'deepseek-chat', 'capabilities': ['text']},
        {'model_id': 'deepseek-reasoner', 'capabilities': ['text']},
        # Genuinely new: appended after the authored rows.
        {'model_id': 'deepseek-v5-flash', 'capabilities': ['text']},
    ]}}
    [merged] = merge_feed_recipes([template], feed)
    ids = [row['model_id'] for row in merged['offering_recipes']]
    assert ids == ['deepseek-v4-pro', 'deepseek-v5-flash']
    assert merged['offering_recipes'][0]['capabilities'] == ['text']
    # The input template object is never mutated.
    assert len(template['offering_recipes']) == 1


def test_merge_leaves_unmapped_templates_untouched(feed_enabled):
    custom = {'key': 'custom', 'offering_recipes': []}
    groq = {'key': 'groq',
            'offering_recipes': [{'model_id': 'llama-4', 'capabilities': []}]}
    feed = {'templates': {'deepseek': [
        {'model_id': 'deepseek-v5-flash', 'capabilities': ['text']}]}}
    merged = merge_feed_recipes([custom, groq], feed)
    assert merged == [custom, groq]


def test_merge_disabled_returns_authored(monkeypatch):
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '0')
    template = _authored_template()
    feed = {'templates': {'deepseek': [
        {'model_id': 'deepseek-v5-flash', 'capabilities': ['text']}]}}
    [merged] = merge_feed_recipes([template], feed)
    assert [row['model_id'] for row in merged['offering_recipes']] == [
        'deepseek-v4-pro']


# ── Cache ──────────────────────────────────────────────────────────

def test_load_cached_feed_missing_or_invalid():
    if os.path.exists(feed_mod._CACHE_PATH):
        os.unlink(feed_mod._CACHE_PATH)
    assert load_cached_feed() is None
    _write_cache(['not', 'a', 'dict'])
    assert load_cached_feed() is None


def test_load_cached_feed_revalidates_every_row():
    _write_cache({'deepseek': [
        {'model_id': 'deepseek-v5-flash', 'capabilities': ['text']},
        {'model_id': ''},                      # rejected by registration
        {'capabilities': ['text']},            # missing model_id
        'garbage',
    ]})
    feed = load_cached_feed()
    assert feed is not None
    assert [row['model_id'] for row in feed['templates']['deepseek']] == [
        'deepseek-v5-flash']
    assert feed['fetched_at'] > 0


# ── Refresh ────────────────────────────────────────────────────────

def test_refresh_now_writes_cache_and_roundtrips(feed_enabled):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse({'data': [
            _catalog_row('deepseek/deepseek-v5-flash'),
            _catalog_row('moonshotai/kimi-k3-thinking'),
        ]})

    stats = refresh_now(http_get=fake_get)
    assert stats == {'ok': True, 'templates': 2, 'models': 2}
    assert calls == ['https://openrouter.ai/api/v1/models']
    feed = load_cached_feed()
    assert feed is not None
    assert feed['templates']['deepseek'][0]['model_id'] == (
        'deepseek-v5-flash')
    assert feed['templates']['kimi'][0]['model_id'] == 'kimi-k3-thinking'


def test_refresh_now_is_fail_closed(feed_enabled):
    _write_cache({'deepseek': [
        {'model_id': 'deepseek-v5-flash', 'capabilities': ['text']}]})
    before = load_cached_feed()

    def raising_get(url, **kwargs):
        raise ConnectionError('offline')

    assert refresh_now(http_get=raising_get)['ok'] is False
    assert load_cached_feed() == before

    def http_error(url, **kw):
        return _FakeResponse({}, ok=False, status_code=503)
    assert refresh_now(http_get=http_error)['ok'] is False
    assert load_cached_feed() == before

    def bad_payload(url, **kw):
        return _FakeResponse({'data': 'not-a-list'})
    assert refresh_now(http_get=bad_payload)['ok'] is False
    assert load_cached_feed() == before


def test_refresh_now_disabled_skips_fetch(monkeypatch):
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '0')

    def forbidden_get(url, **kwargs):  # pragma: no cover - must not run
        raise AssertionError('fetch attempted while disabled')

    assert refresh_now(http_get=forbidden_get) == {
        'ok': False, 'disabled': True}


# ── Refresh scheduling ─────────────────────────────────────────────

def test_maybe_refresh_async_one_flight(feed_enabled, monkeypatch):
    spawned = []
    assert maybe_refresh_async(None, _spawn=spawned.append) is True
    # In-flight: a second stale read must not spawn a duplicate fetch.
    assert maybe_refresh_async(None, _spawn=spawned.append) is False
    assert len(spawned) == 1
    # The daemon body resets the flag even when refresh raises.
    def boom():
        raise RuntimeError('fetch exploded')
    monkeypatch.setattr(feed_mod, 'refresh_now', boom)
    spawned[0]()
    assert maybe_refresh_async(None, _spawn=spawned.append) is True
    # A fresh cache never schedules work.
    fresh = {'fetched_at': time.time(), 'templates': {'x': []}}
    assert maybe_refresh_async(fresh, _spawn=spawned.append) is False


def test_maybe_refresh_async_disabled(monkeypatch):
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '0')
    assert maybe_refresh_async(None, _spawn=lambda t: None) is False


# ── Loader integration ─────────────────────────────────────────────

def test_load_provider_templates_merges_cached_feed(feed_enabled):
    _write_cache({'deepseek': [
        {'model_id': 'deepseek-v4-pro', 'capabilities': ['text', 'vision']},
        {'model_id': 'deepseek-chat', 'capabilities': ['text']},
        {'model_id': 'deepseek-v5-flash', 'capabilities': ['text'],
         'context_window': 262144},
    ]})
    templates = load_provider_templates()
    deepseek = next(t for t in templates if t['key'] == 'deepseek')
    ids = [row['model_id'] for row in deepseek['offering_recipes']]
    # Authored rows keep their authored order and curated fields; the feed
    # row colliding with deepseek-v4-pro and the retired deepseek-chat are
    # dropped; the new model is appended.
    assert ids[:2] == ['deepseek-v4-pro', 'deepseek-v4-flash']
    assert 'deepseek-v5-flash' in ids
    assert 'deepseek-chat' not in ids
    assert ids.count('deepseek-v4-pro') == 1
    v4pro = deepseek['offering_recipes'][0]
    assert v4pro.get('rpm') == 30  # authored curation wins over the feed row


def test_load_provider_templates_drops_canonical_spelling_variants(feed_enabled):
    _write_cache({'minimax': [
        {'model_id': 'minimax-m3', 'capabilities': ['text']},
        {'model_id': 'minimax-m4', 'capabilities': ['text']},
    ]})
    templates = load_provider_templates()
    minimax = next(t for t in templates if t['key'] == 'minimax')
    ids = [row['model_id'] for row in minimax['offering_recipes']]
    # Authored 'MiniMax-M3' canonically owns the minimaxm3 identity; the
    # feed's lowercase respelling is dropped, the genuinely new id is kept.
    assert 'MiniMax-M3' in ids
    assert 'minimax-m3' not in ids
    assert 'minimax-m4' in ids


def test_load_provider_templates_ignores_cache_when_disabled(monkeypatch):
    monkeypatch.setenv('TOFU_TEMPLATE_FEED', '0')
    _write_cache({'deepseek': [
        {'model_id': 'deepseek-v5-flash', 'capabilities': ['text']}]})
    templates = load_provider_templates()
    deepseek = next(t for t in templates if t['key'] == 'deepseek')
    assert [row['model_id'] for row in deepseek['offering_recipes']] == [
        'deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-chat-v3.1',
        'deepseek-r1', 'deepseek-r1-distill-llama-70b',
        'deepseek-v3.1-terminus', 'deepseek-v3.2', 'deepseek-v3.2-exp',
        'deepseek-v4-flash-vision-exp']
