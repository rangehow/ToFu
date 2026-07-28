#!/usr/bin/env python3
"""tests/test_meituan_opus5_gateway_template.py — Opus 5 on the AIGC-gateway template.

Companion to tests/test_meituan_claude_code_opus5.py (which pins the
Claude-Code Anthropic-protocol template). This suite pins the OTHER Meituan
template — ``static/provider_templates/meituan.json`` (AIGC gateway,
OpenAI-protocol, base_url aigc.sankuai.com/v1/openai/native) — the one the
production ``sankuai`` provider syncs from.

Regression class pinned (production incident 2026-07-25): a hand-added entry
used the CLEAN name as primary id (``model_id: "claude-opus-5"``) and demoted
the ONLY gateway-accepted id (``yuju-claude-opus-5-evaDaily``) to ``aliases``.
Because ``body['model'] = slot.model`` sends the wire id verbatim
(lib/llm_dispatch/api.py), every preset request 400'd with
``不支持的模型类型(model=claude-opus-5)`` — verified live against the gateway.

The invariant is about the WIRE, not about which config field holds the id:
**the gateway-accepted id must be in the entry's request pool, and the id the
gateway refuses must not be.** Since 2026-07-27 the model-identity contract
(lib/llm_dispatch/model_entry.py) splits those two roles — ``model_id`` is the
logical name (presets / picker / persisted conversations) and ``request_ids``
is the wire pool — so the clean name is now the CORRECT ``model_id`` precisely
because it can no longer reach the wire. This suite therefore audits the
resolved pool; both the pre-contract shape (gateway id as ``model_id``) and the
contract shape (logical id + ``request_ids``) are accepted, and both spellings
of the incident are rejected.

``MODEL_PRICING`` now carries a row for the evaDaily id (added 2026-07-27):
prices come from the opus-tier card ($5/$25 + 1.25/0.10 cache multipliers —
the same numbers every opus sibling row uses), and ``name: 'Claude Opus 5'``
is the standardized display channel the UI reads via ``_modelShortName``.
The raw yuju id stays the primary wire id; the pricing row only provides
cost accounting and the friendly display name.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_meituan_opus5_gateway_template.py -v
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OPUS5 = 'yuju-claude-opus-5-evaDaily'
SIBLING = 'aws.claude-opus-4.8'
# The clean upstream name the gateway echoes but REFUSES as a request id.
UPSTREAM_ECHO = 'claude-opus-5'


def _load_gateway_template() -> dict:
    path = os.path.join(
        _ROOT, 'static', 'provider_templates', 'meituan.json')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _audit(models: list[dict]) -> list[str]:
    """Audit a meituan.json model list for correct opus-5 registration.

    Asserts the RESULT — the gateway-accepted id ends up ON THE WIRE, and the
    id the gateway refuses does NOT — rather than which config field holds it.
    Under the model-identity contract (lib/llm_dispatch/model_entry.py) the wire
    pool is ``request_ids``, so ``model_id`` is free to be the clean logical
    name; asserting on ``model_id`` directly would go red purely because the
    id moved fields, while the protection it encodes is intact.

    Reusable so the NEUTER face can feed synthetic broken lists through the
    same predicate."""
    from lib.llm_dispatch.model_entry import resolve_request_ids, routing_group

    violations: list[str] = []

    # Which entry OWNS opus-5? The one whose routing group contains it.
    entry = next((m for m in models if OPUS5 in routing_group(m)), None)
    sibling = next((m for m in models if SIBLING in routing_group(m)), None)
    if entry is None:
        violations.append('%s: not registered in any entry' % OPUS5)
    if sibling is None:
        violations.append('%s: parity sibling missing' % SIBLING)
    if entry is not None and sibling is not None:
        for field in ('capabilities', 'rpm', 'cost'):
            if entry.get(field) != sibling.get(field):
                violations.append(
                    '%s: %s %r != %s sibling %r'
                    % (OPUS5, field, entry.get(field), SIBLING, sibling.get(field)))

    # ── The incident shape, stated as a wire fact ──
    # 2026-07-25: the gateway-accepted id was demoted to a routing-only alias
    # under a clean primary, so every request sent ``claude-opus-5`` verbatim
    # and 400'd with 不支持的模型类型. The invariant is about the WIRE:
    if entry is not None:
        wire = resolve_request_ids(entry)
        if OPUS5 not in wire:
            violations.append(
                '%s: routable but NEVER SENT (wire pool=%r) — the only '
                'gateway-accepted id must be in request_ids, or every request '
                'dispatches an id the gateway refuses' % (OPUS5, wire))
    for m in models:
        if UPSTREAM_ECHO in resolve_request_ids(m):
            violations.append(
                '%s: present in the wire pool of %r — the gateway ECHOES this '
                'name but REFUSES it as a request id (HTTP 400)'
                % (UPSTREAM_ECHO, m.get('model_id')))
    return violations


def test_gateway_template_carries_opus5_on_the_wire():
    tpl = _load_gateway_template()
    violations = _audit(tpl.get('models') or [])
    assert not violations, (
        'meituan.json template violations:\n'
        + '\n'.join('  ' + v for v in violations))


def test_neuter_audit_flags_the_incident_shape():
    """Feed the exact broken payload from the production config through the
    audit: the gateway id present but NEVER SENT must be flagged, as must the
    refused clean name reaching the wire. The correct payload must be green.
    If the audit degrades to a tautology this face goes red."""
    # The 2026-07-25 shape under the LEGACY field spelling: clean primary +
    # gateway id demoted to a routing-only alias. Legacy pools are
    # ``[model_id] + aliases``, so the refused clean name IS on the wire here.
    broken_legacy = [
        {'model_id': SIBLING, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': UPSTREAM_ECHO, 'aliases': [OPUS5],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30,
         'cost': 15},
    ]
    violations = _audit(broken_legacy)
    assert any('REFUSES it as a request id' in v for v in violations), violations

    # The SAME incident expressed under the new contract: the entry is
    # routable (a per-key cell lists the gateway id) but the entry-level wire
    # pool omits it and sends the REFUSED clean name — the silent
    # half-migration.
    broken_contract = [
        {'model_id': SIBLING, 'request_ids': [SIBLING],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
        {'model_id': UPSTREAM_ECHO, 'request_ids': [UPSTREAM_ECHO],
         'key_access': {'0': {'request_ids': [OPUS5]}},
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
    ]
    violations = _audit(broken_contract)
    assert any('NEVER SENT' in v or 'REFUSES it as a request id' in v
               for v in violations), violations

    # …and a plain missing registration is still caught.
    assert any('not registered' in v for v in _audit([
        {'model_id': SIBLING, 'request_ids': [SIBLING],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
    ]))

    # Correct shape under the contract: clean logical id, gateway id on the wire.
    good = [
        {'model_id': SIBLING, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': 'claude-opus-5', 'request_ids': [OPUS5],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30, 'cost': 0.045},
    ]
    assert _audit(good) == []
    # And the pre-contract shape (gateway id AS the primary) stays green —
    # repointing this guard must not outlaw configs already on disk.
    legacy_ok = [
        {'model_id': SIBLING, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': OPUS5, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
    ]
    assert _audit(legacy_ok) == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
