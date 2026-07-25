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
Because ``body['model'] = slot.model`` sends the PRIMARY id verbatim
(lib/llm_dispatch/api.py), every preset request 400'd with
``不支持的模型类型(model=claude-opus-5)`` — verified live against the gateway.
Aliases are routing candidates, never substituted onto the wire, so the only
correct shape is the yuju id AS the primary ``model_id``.

Deliberately NOT pinned (same precedent as the claude_code suite): no
``MODEL_PRICING`` row — there is no published marketplace price card for the
evaDaily id, so no pricing data is invented.

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

    Reusable so the NEUTER face can feed synthetic broken lists through the
    same predicate."""
    by_id = {m.get('model_id'): m for m in models}
    violations: list[str] = []

    entry = by_id.get(OPUS5)
    if entry is None:
        violations.append('%s: missing as PRIMARY model_id' % OPUS5)
    sibling = by_id.get(SIBLING)
    if sibling is None:
        violations.append('%s: parity sibling missing' % SIBLING)
    if entry is not None and sibling is not None:
        for field in ('capabilities', 'rpm', 'cost'):
            if entry.get(field) != sibling.get(field):
                violations.append(
                    '%s: %s %r != %s sibling %r'
                    % (OPUS5, field, entry.get(field), SIBLING, sibling.get(field)))

    # The incident shape: gateway id demoted to alias under a clean primary.
    for m in models:
        if m.get('model_id') != OPUS5 and OPUS5 in (m.get('aliases') or []):
            violations.append(
                '%s: present only as an ALIAS of %r — aliases are never sent '
                'on the wire; the gateway-accepted id must be the primary '
                'model_id' % (OPUS5, m.get('model_id')))
    return violations


def test_gateway_template_carries_opus5_as_primary_id():
    tpl = _load_gateway_template()
    violations = _audit(tpl.get('models') or [])
    assert not violations, (
        'meituan.json template violations:\n'
        + '\n'.join('  ' + v for v in violations))


def test_neuter_audit_flags_the_incident_shape():
    """Feed the exact broken payload from the production config through the
    audit: clean primary + yuju alias MUST be flagged, alongside the plain
    missing case; the correct payload must be green. If the audit degrades
    to a tautology this face goes red."""
    broken = [
        {'model_id': SIBLING, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': UPSTREAM_ECHO, 'aliases': [OPUS5],
         'capabilities': ['text', 'vision', 'thinking'], 'rpm': 30,
         'cost': 15},
    ]
    violations = _audit(broken)
    assert any('missing as PRIMARY' in v for v in violations), violations
    assert any('ALIAS' in v for v in violations), violations

    good = [
        {'model_id': SIBLING, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': OPUS5, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
    ]
    assert _audit(good) == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
