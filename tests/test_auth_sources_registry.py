"""tests/test_auth_sources_registry.py — auth_sources as the site registry (P2).

The store evolves from "credential provider" to "site-access registry"
(docs/SITE_KNOWLEDGE_LAYER_DESIGN.md §3.1, epic pt_689b73b305fe4810):

  * ``access_strategy`` (browser_first / cookies_replay / public) is a
    registry field: defaults ride the catalog spec (zero-migration for old
    auth_sources.json), upsert validates it, and it travels on the FULL row
    tofu-search consumes (path ORDER is data, not engine code);
  * spec fields (login_url / cookie fields / risk_note_key) are merged into
    the full row too — the login flow's hint source single-sourced here;
  * the redacted listing carries the knowledge badge (site-doctor-pinned
    extractor = 已内化) injected from lib/site_knowledge in ONE store read.

All offline: the store + knowledge paths point at tmp dirs.
"""

import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def store(monkeypatch):
    import lib.auth_sources as A
    import lib.site_knowledge as sk
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(A, '_STORE_PATH', os.path.join(tmp, 'auth_sources.json'))
    monkeypatch.setattr(sk, '_STORE_PATH', os.path.join(tmp, 'site_knowledge.json'))
    A.invalidate_cache()
    yield A, sk
    A.invalidate_cache()


# ── access_strategy: defaults + validation ────────────────

def test_default_sources_carry_browser_first(store):
    A, _ = store
    rows = {r['domain']: r for r in A.list_sources()}
    assert rows['xiaohongshu.com']['access_strategy'] == 'browser_first'
    assert rows['sankuai.com']['access_strategy'] == 'browser_first'


def test_upsert_validates_strategy(store):
    A, _ = store
    with pytest.raises(ValueError):
        A.upsert_source('weibo.com', access_strategy='teleport')


def test_upsert_persists_strategy(store):
    A, _ = store
    A.upsert_source('weibo.com', label='Weibo', access_strategy='cookies_replay',
                    cookies=[{'name': 's', 'value': 'v', 'domain': 'weibo.com',
                              'path': '/'}])
    row = A.get_source('weibo.com')
    assert row['access_strategy'] == 'cookies_replay'
    listing = {r['domain']: r for r in A.list_sources()}
    assert listing['weibo.com']['access_strategy'] == 'cookies_replay'


def test_custom_source_defaults_to_browser_first(store):
    """A row persisted WITHOUT the field (old store) defaults via _with_spec."""
    A, _ = store
    A.upsert_source('zhihu.com', cookies=[{'name': 'z', 'value': 'v',
                                           'domain': 'zhihu.com', 'path': '/'}],
                    enabled=True)
    row = A.get_source('zhihu.com')
    assert row['access_strategy'] == 'browser_first'


# ── spec merge into the FULL row (tofu-search consumer chain) ──

def test_full_row_carries_spec_fields(store):
    """get_source / match_source rows carry login_url + cookie fields — the
    login flow reads its hints from here, not from a second copy."""
    A, _ = store
    A.upsert_source('xiaohongshu.com', enabled=True,
                    cookies=[{'name': 'web_session', 'value': 'v',
                              'domain': '.xiaohongshu.com', 'path': '/'}])
    row = A.get_source('xiaohongshu.com')
    assert row['login_url'].startswith('https://')
    names = [f['name'] for f in row['fields']]
    assert 'web_session' in names, (
        'the catalog cookie spec must ride the full row — the login flow '
        'reads its hints from this exact shape')

    matched = A.match_source('https://www.xiaohongshu.com/explore/abc')
    assert matched is not None
    assert matched['access_strategy'] == 'browser_first'
    assert 'web_session' in [f['name'] for f in matched['fields']]


def test_match_source_honours_alias(store):
    A, _ = store
    A.upsert_source('xiaohongshu.com', enabled=True,
                    cookies=[{'name': 'web_session', 'value': 'v',
                              'domain': '.xiaohongshu.com', 'path': '/'}])
    assert A.match_source('https://xhslink.com/a/b') is not None


# ── knowledge badge on the listing ────────────────────────

def test_listing_carries_knowledge_badge(store):
    A, sk = store
    sk.pin_knowledge('xiaohongshu.com', extractor_js='(() => [{title:"t",url:"https://x"}])()',
                     wait_selector='div.card', evidence={'anchors': 5})
    rows = {r['domain']: r for r in A.list_sources()}
    badge = rows['xiaohongshu.com']['knowledge']
    assert badge['pinned'] is True
    assert badge['version'] == 1
    assert badge['verified_at'] > 0
    # An unpinned site reports pinned=False — the 仅凭据 badge state.
    assert rows['sankuai.com']['knowledge'] == {'pinned': False}


def test_redact_never_leaks_cookie_values(store):
    A, _ = store
    A.upsert_source('xiaohongshu.com', enabled=True,
                    cookies=[{'name': 'web_session', 'value': 'SECRET',
                              'domain': '.xiaohongshu.com', 'path': '/'}])
    rows = {r['domain']: r for r in A.list_sources()}
    row = rows['xiaohongshu.com']
    assert 'cookies' not in row
    assert row['has_cookies'] is True and row['cookie_count'] == 1
    assert 'SECRET' not in str(row)
