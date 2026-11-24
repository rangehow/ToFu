"""A required gateway session header reaches every probe surface.

OpenCode Go rejects a generation request without ``x-opencode-session``
(HTTP 400 ``MissingSessionID``), so a probe that omits it reports a false
model failure. The template declares the shared cache-affinity carrier; both
the matrix probe worker and template-supply's live prober must send a fresh
one-request session.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

PROFILE = {'features': {'cache_affinity_opencode_session': 'supported'}}


def test_template_supply_probe_mints_the_declared_session(monkeypatch):
    from lib.model_routing import template_supply
    from lib.provider import provider_probe

    work = (0, 'synthetic-key', 'model', 'wire', [],
            'https://opencode.ai/inference/go/openai/v1', 'openai')
    monkeypatch.setattr(provider_probe, 'build_probe_work',
                        lambda *a, **k: [work])
    received = []

    def probe(url, key, wire, headers, timeout, **kwargs):
        received.append(dict(headers))
        return 'ok', ''

    monkeypatch.setattr(provider_probe, 'probe_one_cell', probe)
    template_supply._live_prober(
        base_url='https://opencode.ai/inference/go/openai/v1',
        protocol='openai', faces={}, api_key='synthetic-key',
        extra_headers={}, recipes=[], inference_profile=PROFILE)
    assert len(received[0]['x-opencode-session']) == 36


def test_template_supply_probe_keeps_an_owner_session_header(monkeypatch):
    from lib.model_routing import template_supply
    from lib.provider import provider_probe

    work = (0, 'synthetic-key', 'model', 'wire', [],
            'https://opencode.ai/inference/go/openai/v1', 'openai')
    monkeypatch.setattr(provider_probe, 'build_probe_work',
                        lambda *a, **k: [work])
    received = []

    def probe(url, key, wire, headers, timeout, **kwargs):
        received.append(dict(headers))
        return 'ok', ''

    monkeypatch.setattr(provider_probe, 'probe_one_cell', probe)
    template_supply._live_prober(
        base_url='https://opencode.ai/inference/go/openai/v1',
        protocol='openai', faces={}, api_key='synthetic-key',
        extra_headers={'x-opencode-session': 'pinned-by-owner'}, recipes=[],
        inference_profile=PROFILE)
    assert received[0]['x-opencode-session'] == 'pinned-by-owner'
