"""Friday Skills Market pack behavior (bundles/meituan/site_packs/friday.json).

The legacy built-in drove page.research with a canonical filter URL; the pack
renders the same URL from its template and feeds the receipt payload into the
normalized search-records tail.
"""

from types import SimpleNamespace

import pytest

from lib.site_packs import load_packs, reset_cache_for_tests
from lib.site_packs._interpreter import operation_capabilities, operation_handler

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_loader_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


@pytest.fixture()
def pack():
    return load_packs()['friday']


def _operation(pack, name):
    return next(op for op in pack.operations if op.name == name)


def test_search_research_url_and_payload_flow(pack, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        'lib.site_packs._interpreter.read_search_records',
        lambda page, query, limit, source_id, source_name, **kw: captured.update(
            query=query, limit=limit, source_name=source_name,
            payload=kw.get('payload')) or [{'title': '网页信息提取'}])

    class FakePage:
        current_url = 'https://friday.sankuai.com/skills/skills-market'
        lease = SimpleNamespace(owner_user_id='41')

        def __init__(self):
            self.calls = []

        def research(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return {'result': {'requestedUrl': url, 'url': url, 'data': {}}}

    page = FakePage()
    handler = operation_handler(pack, _operation(pack, 'search'))
    rows = handler(page, {'query': '网页', 'limit': 30, 'pages': 2})

    assert rows[0]['title'] == '网页信息提取'
    (url, kwargs), = page.calls
    assert 'keyword=%E7%BD%91%E9%A1%B5' in url
    assert 'pageSize=30' in url
    assert kwargs['max_pages'] == 2
    assert kwargs['pagination'] == 'auto'
    assert captured['payload']['url'] == url
    assert captured['source_name'] == 'Friday Skills Market'


def test_search_page_size_has_floor_of_30(pack, monkeypatch):
    monkeypatch.setattr(
        'lib.site_packs._interpreter.read_search_records',
        lambda *a, **kw: [])

    class FakePage:
        current_url = 'https://friday.sankuai.com/skills/skills-market'
        lease = SimpleNamespace(owner_user_id='41')

        def __init__(self):
            self.urls = []

        def research(self, url, **kwargs):
            self.urls.append(url)
            return {'result': {}}

    page = FakePage()
    handler = operation_handler(pack, _operation(pack, 'search'))
    handler(page, {'query': 'x', 'limit': 5, 'pages': 1})
    assert 'pageSize=30' in page.urls[0]  # max(30, limit), not pageSize=5


def test_operation_capabilities_match_legacy_manifest(pack):
    for name in ('search', 'detail'):
        assert sorted(operation_capabilities(_operation(pack, name))) == sorted(
            ('deep_collect', 'network_body'))
