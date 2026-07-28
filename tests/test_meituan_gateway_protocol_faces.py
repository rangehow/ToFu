#!/usr/bin/env python3
"""tests/test_meituan_gateway_protocol_faces.py — one gateway, two protocol faces.

Supersedes ``tests/test_meituan_opus5_gateway_template.py`` (deleted 2026-07-28).
That suite pinned "opus-5 must be registered in meituan.json with the
gateway-accepted yuju id on the wire" — correct on 2026-07-25, when the AIGC
gateway's OpenAI-compatible face was the ONLY way to reach Claude. The
2026-07-28 signature migration inverted its premise, so keeping it would have
pinned the bug: it demanded Claude stay on the face that silently drops
thinking signatures.

WHAT THIS PINS INSTEAD
----------------------
``aigc.sankuai.com`` exposes the SAME models over TWO wire protocols, sharing
one set of API keys:

  * ``/v1/openai/native``  → meituan.json              (OpenAI-compatible)
  * ``/v1/anthropic``      → meituan_claude_code.json  (protocol=anthropic)

Measured live 2026-07-28 on ``yuju-claude-opus-5-evaDaily``: the OpenAI face
streamed 111 chunks / 33 ``reasoning_content`` / **0 signature**, while the
Anthropic face delivered ``signature_delta`` intact (plus working tool_use and
prompt caching). A Claude thinking block replayed WITHOUT its signature is
rejected upstream, so which face a Claude model sits on is a correctness fact,
not a preference.

Therefore: **on a gateway host that offers an Anthropic-native face, every
Claude-family model belongs on that face.** The drift this catches is silent —
the model appears in the picker, requests succeed, and only the reasoning
signature goes missing — and it is one careless template edit (or one
``_syncFromTemplate`` click) away, because sync ADDS every template entry the
provider lacks.

DELIBERATELY HOST-SCOPED — not "Claude ⇒ protocol=anthropic"
------------------------------------------------------------
Bedrock, OpenRouter, shubiaobiao and yeysai all legitimately serve Claude over
OpenAI-compatible endpoints and expose NO Anthropic face; on those hosts the
OpenAI wire is the only option and forbidding it would outlaw working configs.
The invariant only bites when the same host offers both faces, which today is
exactly the Meituan gateway.

Classification uses ``lib.model_info._family.is_claude`` — the backend SSOT
that already drives the Claude wire contract — rather than a hand-rolled regex,
so a future ``fable``/``opus`` spelling is covered without editing this file.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_meituan_gateway_protocol_faces.py -v
"""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TPL_DIR = os.path.join(_ROOT, 'static', 'provider_templates')

GATEWAY_HOST = 'aigc.sankuai.com'
OPENAI_FACE = 'meituan.json'
ANTHROPIC_FACE = 'meituan_claude_code.json'

# The full Claude roster the gateway serves, as configured on the live
# sankuai_anthropic provider. The Anthropic face must carry all of them —
# a partial roster sends users back to the OpenAI face for the remainder.
EXPECTED_CLAUDE_ROSTER = {
    'claude-opus-5',
    'claude-opus-4.8',
    'claude-opus-4.7',
    'claude-opus-4.6',
    'claude-sonnet-4.6',
    'claude-fable-5',
}

# Gateway-accepted wire ids that must stay reachable after the move. These are
# the ids the gateway actually answers to; the clean logical names are NOT
# accepted as request ids on the yuju deployments (HTTP 400 不支持的模型类型).
REQUIRED_WIRE_IDS = {
    'claude-opus-5': 'yuju-claude-opus-5-evaDaily',
    'claude-opus-4.8': 'aws.claude-opus-4.8',
    'claude-opus-4.7': 'aws.claude-opus-4.7',
    'claude-opus-4.6': 'aws.claude-opus-4.6',
    'claude-sonnet-4.6': 'aws.claude-sonnet-4.6',
}


def _load(fname: str) -> dict:
    with open(os.path.join(_TPL_DIR, fname), encoding='utf-8') as f:
        return json.load(f)


def _all_templates() -> list[tuple[str, dict]]:
    out = []
    for fname in sorted(os.listdir(_TPL_DIR)):
        if fname.endswith('.json'):
            out.append((fname, _load(fname)))
    return out


def _host(tpl: dict) -> str:
    return (urlparse(tpl.get('base_url') or '').hostname or '').lower()


def _audit_faces(templates: list[tuple[str, dict]]) -> list[str]:
    """Return violations of the host-scoped protocol-face invariant.

    A Claude-family model may only be registered on an OpenAI-protocol
    template when its gateway host offers NO Anthropic-protocol template.
    Reusable so the NEUTER face can push synthetic payloads through the same
    predicate.
    """
    from lib.model_info._family import is_claude

    # Which hosts offer an Anthropic-native face?
    anthropic_hosts = {
        _host(tpl) for _, tpl in templates
        if (tpl.get('protocol') or '') == 'anthropic' and _host(tpl)
    }

    violations: list[str] = []
    for fname, tpl in templates:
        if (tpl.get('protocol') or '') == 'anthropic':
            continue
        host = _host(tpl)
        if host not in anthropic_hosts:
            continue  # OpenAI wire is the only option on this host — allowed.
        offenders = [
            (m.get('model_id') or '') for m in (tpl.get('models') or [])
            if is_claude(m.get('model_id') or '')
        ]
        if offenders:
            violations.append(
                '%s (host=%s, protocol=openai) registers Claude-family models '
                '%r while the SAME host offers an Anthropic-native face — that '
                'face is the only one that emits thinking-block signatures, so '
                'these entries silently drop the signature'
                % (fname, host, sorted(offenders)))
    return violations


# ═══════════════════════════════════════════════════════════
#  1. The invariant, across every shipped template
# ═══════════════════════════════════════════════════════════

def test_no_claude_on_an_openai_face_when_the_host_has_an_anthropic_face():
    violations = _audit_faces(_all_templates())
    assert not violations, (
        'provider-template protocol-face violations:\n'
        + '\n'.join('  ' + v for v in violations))


def test_openai_face_of_the_meituan_gateway_is_claude_free():
    """The concrete drift this epic fixed, stated directly."""
    from lib.model_info._family import is_claude
    tpl = _load(OPENAI_FACE)
    assert _host(tpl) == GATEWAY_HOST, _host(tpl)
    assert (tpl.get('protocol') or '') != 'anthropic'
    stowaways = sorted(
        (m.get('model_id') or '') for m in (tpl.get('models') or [])
        if is_claude(m.get('model_id') or ''))
    assert stowaways == [], (
        '%s must carry NO Claude-family model — found %r. They belong on %s '
        '(the Anthropic-native face of the same gateway).'
        % (OPENAI_FACE, stowaways, ANTHROPIC_FACE))


# ═══════════════════════════════════════════════════════════
#  2. The move must be a MOVE, not a deletion
# ═══════════════════════════════════════════════════════════

def test_anthropic_face_carries_the_whole_claude_roster():
    """Removing Claude from the OpenAI face is only correct if the Anthropic
    face actually serves every one of them — otherwise the fix degrades into
    "Claude is unavailable from the template"."""
    tpl = _load(ANTHROPIC_FACE)
    assert (tpl.get('protocol') or '') == 'anthropic', tpl.get('protocol')
    assert _host(tpl) == GATEWAY_HOST, _host(tpl)
    have = {(m.get('model_id') or '') for m in (tpl.get('models') or [])}
    missing = EXPECTED_CLAUDE_ROSTER - have
    assert not missing, (
        '%s is missing %r — a partial roster sends users back to the '
        'signature-dropping OpenAI face for the remainder'
        % (ANTHROPIC_FACE, sorted(missing)))


def test_gateway_accepted_wire_ids_survive_the_move():
    """The wire pool is what actually goes out as body['model']. The gateway
    refuses the clean logical names on the yuju deployments, so each moved
    entry must still dispatch a gateway-accepted id."""
    from lib.llm_dispatch.model_entry import resolve_request_ids, routing_group

    models = _load(ANTHROPIC_FACE).get('models') or []
    for logical, wire_id in REQUIRED_WIRE_IDS.items():
        entry = next((m for m in models if logical in routing_group(m)), None)
        assert entry is not None, '%s: not registered on %s' % (logical, ANTHROPIC_FACE)
        pool = resolve_request_ids(entry)
        assert wire_id in pool, (
            '%s: gateway-accepted id %s missing from the wire pool %r — every '
            'request would dispatch an id the gateway refuses (HTTP 400)'
            % (logical, wire_id, pool))


def test_claude_entries_keep_thinking_capability_and_sibling_parity():
    """A Claude model that lost its 'thinking' cap in the move would silently
    stop requesting reasoning at all — which is the very thing the Anthropic
    face exists to preserve."""
    models = _load(ANTHROPIC_FACE).get('models') or []
    by_id = {(m.get('model_id') or ''): m for m in models}
    for logical in EXPECTED_CLAUDE_ROSTER:
        entry = by_id.get(logical)
        assert entry is not None, logical
        caps = set(entry.get('capabilities') or [])
        assert caps == {'text', 'vision', 'thinking'}, (
            '%s: caps %r != {text, vision, thinking}' % (logical, sorted(caps)))
        assert entry.get('rpm'), '%s: rpm missing' % logical
        assert entry.get('cost') is not None, '%s: cost missing' % logical


# ═══════════════════════════════════════════════════════════
#  3. The three models the OpenAI face was missing
# ═══════════════════════════════════════════════════════════

def test_openai_face_carries_the_models_live_already_serves():
    """Template drift cuts both ways: these three are configured on the live
    sankuai provider but were absent from the template, so "sync from
    template" could never (re)establish them."""
    have = {(m.get('model_id') or '')
            for m in (_load(OPENAI_FACE).get('models') or [])}
    for mid in ('gemini-2.5-flash-image', 'gemini-3-pro-image-preview',
                'text-embedding-3-large'):
        assert mid in have, '%s missing from %s' % (mid, OPENAI_FACE)


# ═══════════════════════════════════════════════════════════
#  4. NEUTER faces — prove the predicate discriminates
# ═══════════════════════════════════════════════════════════

def test_neuter_audit_flags_a_claude_stowaway_on_the_dual_face_host():
    """The exact drift shape (a Claude entry back on the OpenAI face of a host
    that has an Anthropic face) must be flagged. If the audit degrades to a
    tautology this face goes red."""
    drifted = [
        (OPENAI_FACE, {'base_url': 'https://aigc.sankuai.com/v1/openai/native',
                       'models': [{'model_id': 'gemini-3.5-flash'},
                                  {'model_id': 'claude-opus-5',
                                   'request_ids': ['yuju-claude-opus-5-evaDaily']}]}),
        (ANTHROPIC_FACE, {'base_url': 'https://aigc.sankuai.com/v1/anthropic',
                          'protocol': 'anthropic',
                          'models': [{'model_id': 'claude-opus-5'}]}),
    ]
    violations = _audit_faces(drifted)
    assert any('claude-opus-5' in v for v in violations), violations

    # A wire-id spelling of the same stowaway is caught too (is_claude is not
    # anchored to the clean name).
    drifted_wire = [
        (OPENAI_FACE, {'base_url': 'https://aigc.sankuai.com/v1/openai/native',
                       'models': [{'model_id': 'aws.claude-opus-4.6'}]}),
        (ANTHROPIC_FACE, {'base_url': 'https://aigc.sankuai.com/v1/anthropic',
                          'protocol': 'anthropic', 'models': []}),
    ]
    assert _audit_faces(drifted_wire), 'wire-id spelling must also be flagged'


def test_neuter_single_face_hosts_stay_allowed():
    """Bedrock / OpenRouter / relay hosts serve Claude over OpenAI-compat and
    expose NO Anthropic face. If the invariant ever widens to a blanket
    "Claude ⇒ anthropic" rule, this face goes red — it would outlaw configs
    that are the only way those gateways work."""
    single_face = [
        ('bedrock.json', {
            'base_url': 'https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1',
            'models': [{'model_id': 'us.anthropic.claude-opus-4-7-v1:0'}]}),
        ('openrouter.json', {
            'base_url': 'https://openrouter.ai/api/v1',
            'models': [{'model_id': 'anthropic/claude-sonnet-4.6'}]}),
        (ANTHROPIC_FACE, {'base_url': 'https://aigc.sankuai.com/v1/anthropic',
                          'protocol': 'anthropic',
                          'models': [{'model_id': 'claude-opus-5'}]}),
    ]
    assert _audit_faces(single_face) == [], (
        'Claude on an OpenAI-compat host with no Anthropic face must stay legal')

    # …and the real shipped set is green (the positive control for the above).
    assert _audit_faces(_all_templates()) == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
