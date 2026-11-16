"""Connector-owned document URLs are read through the connector's MCP tool.

A catalog entry's ``document_reader`` (lib/mcp/registry.py) names the hosts,
path shape and tool; ``_ChatuiMcpDocumentReader`` reroutes matching links
instead of the anonymous login wall. The connector here is synthetic; the
学城 declaration is pinned in bundles/meituan/tests/test_xuecheng_document_reader.py.
"""
from __future__ import annotations

import pytest

import lib.mcp
from lib.search.search_bridge import _ChatuiMcpDocumentReader

pytestmark = pytest.mark.unit

ENTRY = {'id': 'wiki', 'name': 'Wiki', 'category': 'Productivity', 'command': 'wiki-mcp',
         'document_reader': {'label': 'Wiki', 'hosts': ['docs.example.test'],
                             'path_pattern': r'^/page/\d+', 'tool': 'fetch_page',
                             'url_argument': 'url', 'max_chars_argument': 'limit',
                             'min_timeout_s': 30, 'base_url': 'https://docs.example.test'}}


class _FakeBridge:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.calls = []

    def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr('lib.mcp.registry.get_catalog', lambda: [ENTRY])


def test_matches_only_declared_hosts_and_paths():
    reader = _ChatuiMcpDocumentReader()
    assert reader.matches('https://docs.example.test/page/42')
    assert reader.matches('https://user@docs.example.test:443/page/42#x')
    assert not reader.matches('https://docs.example.test/')
    assert not reader.matches('https://evil.example.test/page/42')
    assert not reader.matches('not-a-url')


def test_without_a_declaring_connector_nothing_matches(monkeypatch):
    monkeypatch.setattr('lib.mcp.registry.get_catalog', lambda: [])
    assert not _ChatuiMcpDocumentReader().matches('https://docs.example.test/page/42')


def test_read_calls_the_declared_tool(monkeypatch):
    bridge = _FakeBridge(result='PAGE')
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda *a, **k: bridge)
    url = 'https://docs.example.test/page/42'
    assert _ChatuiMcpDocumentReader().read(url, max_chars=500, timeout=5) == 'PAGE'
    assert bridge.calls == [('mcp__wiki__fetch_page', {'url': url, 'limit': 500},
                             {'timeout_override': 30})]


def test_unconfigured_server_falls_through_and_failures_are_explained(monkeypatch):
    url = 'https://docs.example.test/page/1'
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda *a, **k: _FakeBridge(error=ValueError('x')))
    assert _ChatuiMcpDocumentReader().read(url) is None
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda *a, **k: _FakeBridge(error=TimeoutError('t')))
    assert _ChatuiMcpDocumentReader().read(url) == (
        f'[Wiki文档自动读取失败] t 可改用 wiki 的 fetch_page 工具重试。原始链接: {url}')
