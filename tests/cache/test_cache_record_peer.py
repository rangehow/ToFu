"""Per-round cache records carry the gateway address the request reached.

The 2026-11-12 address probe (docs/LLM_COST_OPTIMIZATION.md, "Gateway address
and cache domains") found that the gateway address does not select the Claude
cache domain; the per-round ``peer`` keeps that checkable on production traffic
and gives the gateway owners the address beside each M-TraceId.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _records(monkeypatch, usage_extra):
    import lib.tasks_pkg.cache_tracking._detect as det

    payloads = []
    real_info = det.logger.info

    def _capture(fmt, *args):
        if fmt.startswith('[CacheRoundRecord]'):
            payloads.append(json.loads(args[0]))
        return real_info(fmt, *args)

    monkeypatch.setattr(det.logger, 'info', _capture)
    for read, write in [(0, 200_000), (195_000, 5_000)]:
        det.detect_cache_break(
            'conv-peer-record', [{'role': 'user', 'content': 'x'}], None, 'm',
            {'prompt_tokens': 1000, 'completion_tokens': 10,
             'cache_read_input_tokens': read,
             'cache_creation_input_tokens': write, **usage_extra},
            user_id=1, durable=False)
    return payloads


def test_records_carry_the_connected_peer(monkeypatch):
    route = {'routeId': 'direct', 'routeMode': 'direct',
             'decisionReason': 'network_route', 'peer': '10.192.19.176:443'}
    payloads = _records(monkeypatch, {'_network_route': route})
    assert len(payloads) == 2
    assert {p.get('peer') for p in payloads} == {'10.192.19.176:443'}


def test_records_without_a_known_peer_omit_the_field(monkeypatch):
    payloads = _records(monkeypatch, {'_network_route': {
        'routeId': 'direct', 'routeMode': 'direct', 'decisionReason': 'network_route'}})
    assert payloads and not any('peer' in p for p in payloads)
    assert not any('peer' in p for p in _records(monkeypatch, {}))
