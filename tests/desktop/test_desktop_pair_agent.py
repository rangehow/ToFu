#!/usr/bin/env python3
"""tests/desktop/test_desktop_pair_agent.py — node-side route repair.

``lib.desktop_agent._pair.resume_attachment`` re-points a dead saved route
only to the attachment's own candidate routes (those its pairing link
carried), keeps the credential, persists through the caller's callback, and
never discovers destinations for an existing bearer.
"""

import pytest

from lib.desktop_agent import _pair

pytestmark = pytest.mark.unit


def test_resume_alive_is_zero_action(monkeypatch):
    monkeypatch.setattr(_pair, 'probe_server', lambda url, timeout=0: (True, ''))
    assert _pair.resume_attachment(
        'http://srv:15000', 'tok', candidates=['http://other:15000'],
        persist=lambda url: pytest.fail('live attachment must not be re-written'),
    ) == ('http://srv:15000', 'tok')


def test_resume_dead_repoints_to_own_candidate_and_keeps_token(monkeypatch):
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (url == 'http://10.0.0.9:15000', ''))
    saved = []
    assert _pair.resume_attachment(
        'http://127.0.0.1:15000', 'tok-keep',
        candidates=['http://dead.example', 'http://10.0.0.9:15000/'],
        persist=saved.append,
    ) == ('http://10.0.0.9:15000', 'tok-keep')
    assert saved == ['http://10.0.0.9:15000']


def test_resume_nothing_answers_keeps_attachment(monkeypatch):
    """The server may simply be off: the route and token survive and the
    poll loop keeps retrying."""
    monkeypatch.setattr(_pair, 'probe_server', lambda url, timeout=0: (False, 'unreachable'))
    assert _pair.resume_attachment(
        'http://127.0.0.1:15000', 'tok', candidates=['http://10.0.0.9:15000'],
        persist=lambda url: pytest.fail('no rewrite when nothing answers'),
    ) == ('http://127.0.0.1:15000', 'tok')


def test_resume_ignores_unsafe_and_duplicate_candidates(monkeypatch):
    probed = []

    def probe(url, timeout=0):
        probed.append(url)
        return False, 'unreachable'

    monkeypatch.setattr(_pair, 'probe_server', probe)
    _pair.resume_attachment(
        'https://saved.example', 'tok',
        candidates=['https://saved.example', 'http://u:p@evil.example',
                    'https://x.example?token=1', 42, 'https://ok.example',
                    'https://ok.example/'])
    assert probed == ['https://saved.example', 'https://ok.example']


def test_resume_has_no_discovery_surface():
    for name in ('discover', 'lan_probe', 'try_ssh_tunnel', 'ssh_config_hosts'):
        assert not hasattr(_pair, name), name
