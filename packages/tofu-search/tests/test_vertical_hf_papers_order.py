"""HF Papers keyword search keeps the endpoint's relevance ranking."""

from tofu_search.search.vertical import base
from tofu_search.search.vertical import hf_papers


class _Resp:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _row(pid, upvotes):
    return {'paper': {'id': pid, 'title': f'Paper {pid}', 'upvotes': upvotes}}


def test_keyword_search_keeps_relevance_order_beyond_popular_rows(monkeypatch):
    # The exact match is ranked 2nd by relevance but has few upvotes; 20 more
    # popular, less relevant rows follow it.
    payload = [_row('0001.00001', 0), _row('2601.07372', 53)]
    payload += [_row(f'2500.{i:05d}', 100 + i) for i in range(20)]
    monkeypatch.setattr(base, 'http_get', lambda url, **kw: _Resp(payload))

    record = hf_papers.search('engram conditional memory', {})

    ids = [item['arxiv_id'] for item in record['items']]
    assert ids[:2] == ['0001.00001', '2601.07372']


def test_daily_list_is_ranked_by_upvotes(monkeypatch):
    payload = [_row('a', 1), _row('b', 50), _row('c', 7)]
    monkeypatch.setattr(base, 'http_get', lambda url, **kw: _Resp(payload))

    record = hf_papers.search('', {'period': 'day'})

    assert [item['arxiv_id'] for item in record['items']] == ['b', 'c', 'a']
