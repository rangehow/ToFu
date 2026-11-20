"""The pairing link's LAN ``via`` route names the minting server's own port.

A test Home inherited ``PORT``/``_TOFU_RUNTIME_PORT`` from the production Tofu
that launched it (or fell back to 15000), so its minted link carried a LAN
route to production and a joining test node sent its pairing attempt there.
The route must use the port the minting request actually arrived on.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

pytestmark = [pytest.mark.slow]


def _mint(base: str) -> dict:
    request = urllib.request.Request(
        f'{base}/api/v4/desktop/pair-code?base={base}/', data=b'{}', method='POST',
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 201
        payload = json.loads(response.read())
    return payload.get('data', payload)


def test_lan_route_uses_the_minting_servers_own_port(live_server, monkeypatch):
    from lib.desktop import pairing
    from lib.desktop_wire.node_protocol import parse_pairing_link

    own_port = int(live_server.rsplit(':', 1)[1])
    inherited = 15000 if own_port != 15000 else 15001
    monkeypatch.setenv('PORT', str(inherited))
    monkeypatch.setenv('_TOFU_RUNTIME_PORT', str(inherited))
    monkeypatch.setenv('_TOFU_RUNTIME_HOST', '0.0.0.0')
    monkeypatch.setattr(pairing, 'lan_ip', lambda: '192.0.2.7')

    link = parse_pairing_link(_mint(live_server)['link'])

    assert link.server_url == live_server
    assert list(link.candidates) == [f'http://192.0.2.7:{own_port}', live_server]


def test_loopback_bound_server_offers_no_lan_route(live_server, monkeypatch):
    from lib.desktop import pairing
    from lib.desktop_wire.node_protocol import parse_pairing_link

    monkeypatch.setenv('_TOFU_RUNTIME_HOST', '127.0.0.1')
    monkeypatch.setattr(pairing, 'lan_ip', lambda: '192.0.2.7')

    assert list(parse_pairing_link(_mint(live_server)['link']).candidates) == [live_server]
