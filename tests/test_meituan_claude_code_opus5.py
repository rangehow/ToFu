#!/usr/bin/env python3
"""tests/test_meituan_claude_code_opus5.py — Claude Opus 5 (yuju evaDaily) registration guard.

Pins the end-to-end registration of Claude Opus 5 on the Meituan Claude Code
(Anthropic-protocol) provider, added 2026-07-25. The ONLY gateway alias
currently available is ``yuju-claude-opus-5-evaDaily`` — same provider and
naming family as the existing ``yuju-claude-opus-4.8-evaDaily`` /
``yuju-claude-opus-4.7-evaDaily`` entries.

Two registration surfaces are audited:

  1. ``static/provider_templates/meituan_claude_code.json`` — the Settings-UI
     template entry must mirror its 4.8 sibling (caps / rpm / cost).
  2. ``lib.model_info._family.is_claude_opus_47`` — the evaDaily alias is
     BARE-MAJOR (``opus-5-…``, no minor digit). The pre-fix regex required a
     ``opus-X.Y`` minor, so the new alias silently classified as Opus ≤4.6:
     no ``thinking.display='summarized'`` (reasoning trace hidden), sampling
     params NOT stripped (Opus 4.7+ contract violation — api.py notes these
     can be HTTP 400), and ``xhigh`` effort downgraded to ``high``.

plus wire-shape parity against ``aws.claude-opus-4.8`` (the reference 4.7+
model) through ``build_body``.

Deliberately NOT pinned (matching the 4.7/4.8 evaDaily precedent):
``DEFAULT_SLOT_CONFIGS`` / ``MODEL_PRICING`` / ``MODEL_ALIAS_GROUPS`` rows —
the evaDaily models carry their caps on the provider template and have no
marketplace price card to convert, so no table rows are invented.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_meituan_claude_code_opus5.py -v
"""
from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OPUS5 = 'yuju-claude-opus-5-evaDaily'
OPUS48 = 'yuju-claude-opus-4.8-evaDaily'
REF_48 = 'aws.claude-opus-4.8'

_DUMMY_MSGS = [{'role': 'user', 'content': 'hi'}]


def _load_claude_code_template() -> dict:
    path = os.path.join(
        _ROOT, 'static', 'provider_templates', 'meituan_claude_code.json')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _template_violations(models: list[dict]) -> list[str]:
    """Audit the template model list: opus-5 present + field-parity with its
    4.8 sibling. Reusable so the NEUTER face can feed a synthetic broken list
    through the same predicate."""
    by_id = {m.get('model_id'): m for m in models}
    violations: list[str] = []
    entry = by_id.get(OPUS5)
    if entry is None:
        violations.append('%s: missing from template' % OPUS5)
        return violations
    sibling = by_id.get(OPUS48)
    if sibling is None:
        violations.append('%s: parity sibling missing from template' % OPUS48)
        return violations
    for field in ('capabilities', 'rpm', 'cost'):
        if entry.get(field) != sibling.get(field):
            violations.append(
                '%s: %s %r != 4.8 sibling %r'
                % (OPUS5, field, entry.get(field), sibling.get(field)))
    if set(entry.get('capabilities') or []) != {'text', 'vision', 'thinking'}:
        violations.append(
            '%s: caps %r != {text, vision, thinking}'
            % (OPUS5, sorted(entry.get('capabilities') or [])))
    return violations


# ═══════════════════════════════════════════════════════════
#  1. Template registration
# ═══════════════════════════════════════════════════════════

def test_template_carries_opus5_with_sibling_parity():
    tpl = _load_claude_code_template()
    violations = _template_violations(tpl.get('models') or [])
    assert not violations, (
        'meituan_claude_code.json template violations:\n'
        + '\n'.join('  ' + v for v in violations))


# ═══════════════════════════════════════════════════════════
#  2. Family detection — bare-major opus-5 is Opus 4.7+
# ═══════════════════════════════════════════════════════════

def test_opus5_alias_detects_as_claude_opus_47_plus():
    """The whole point of the regex fix: the bare-major evaDaily alias must
    take the Opus 4.7+ wire contract (display=summarized, no sampling params,
    xhigh tier)."""
    from lib.model_info._family import is_claude, is_claude_opus_47
    assert is_claude(OPUS5)
    assert is_claude_opus_47(OPUS5), (
        '%s must classify as Opus 4.7+ — the bare-major form opus-5-<suffix> '
        'carries no minor digit for the legacy opus-X.Y regex' % OPUS5)
    # Future bare / dotted shapes take the same branch.
    assert is_claude_opus_47('claude-opus-5')
    assert is_claude_opus_47('aws.claude-opus-5')
    assert is_claude_opus_47('us.anthropic.claude-opus-5-0-v1:0')


def test_opus47_verdicts_unchanged_for_existing_names():
    """Regression matrix: widening the regex must not flip any pre-existing
    verdict (4.6 line stays False, 4.7/4.8 stay True, non-opus stays False)."""
    from lib.model_info._family import is_claude_opus_47
    expected = {
        'aws.claude-opus-4.8': True,
        'yuju-claude-opus-4.8-evaDaily': True,
        'aws.claude-opus-4.7': True,
        'yuju-claude-opus-4.7-evaDaily': True,
        'claude-opus-4-7': True,
        'us.anthropic.claude-opus-4-7-v1:0': True,
        'aws.claude-opus-4.6': False,
        'vertex.claude-opus-4.6': False,
        'claude-opus-4-6': False,
        'claude-sonnet-4-6': False,
        'aws.claude-sonnet-4.6': False,
        'claude-fable-5': False,          # no 'opus' token
        'yuju-claude-opus-4-evaDaily': False,  # bare FOUR is still < 4.7
    }
    for name, want in expected.items():
        assert is_claude_opus_47(name) is want, (
            '%s: expected %s' % (name, want))


def test_discovery_caps_parity_with_opus_48():
    """Name-based capability inference treats opus-5 exactly like its 4.8
    sibling (explicit template caps are the authoritative source for both)."""
    from lib.llm_dispatch.discovery._capabilities import _infer_capabilities
    assert _infer_capabilities(OPUS5) == _infer_capabilities(OPUS48)


# ═══════════════════════════════════════════════════════════
#  3. Wire shape — build_body parity with the reference 4.7+ model
# ═══════════════════════════════════════════════════════════

def _claude_body(model: str, **kw) -> dict:
    from lib.llm import build_body
    args = dict(max_tokens=4096, thinking_enabled=True, stream=False)
    args.update(kw)
    return build_body(model, copy.deepcopy(_DUMMY_MSGS), **args)


def test_wire_shape_parity_with_reference_opus_48():
    """Same generation contract ⇒ byte-identical bodies modulo the model id:
    thinking adaptive + display=summarized, NO temperature/top_p/top_k."""
    got = _claude_body(OPUS5)
    ref = _claude_body(REF_48)
    assert got.get('thinking') == ref.get('thinking') == {
        'type': 'adaptive', 'display': 'summarized'}
    for key in ('temperature', 'top_p', 'top_k'):
        assert key not in got, '%s must strip %s (Opus 4.7+ contract)' % (OPUS5, key)
        assert key not in ref
    stripped_got = {k: v for k, v in got.items() if k != 'model'}
    stripped_ref = {k: v for k, v in ref.items() if k != 'model'}
    assert stripped_got == stripped_ref


def test_xhigh_effort_not_downgraded_on_opus5():
    """xhigh is the Opus 4.7+ tier — on the un-widened regex opus-5 silently
    downgraded it to high."""
    got = _claude_body(OPUS5, thinking_depth='xhigh')
    assert got.get('effort') == 'xhigh', got.get('effort')
    # …and the GPT-5.6 'ultra' tier still maps to Claude's top rung (max).
    got_ultra = _claude_body(OPUS5, thinking_depth='ultra')
    assert got_ultra.get('effort') == 'max', got_ultra.get('effort')


# ═══════════════════════════════════════════════════════════
#  4. NEUTER faces — prove the predicates discriminate
# ═══════════════════════════════════════════════════════════

def test_neuter_bare_opus4_is_not_47_plus():
    """If the regex ever degrades to 'any opus ⇒ 4.7+', this face goes red:
    a bare-major FOUR must stay below the 4.7 threshold."""
    from lib.model_info._family import is_claude_opus_47
    assert not is_claude_opus_47('yuju-claude-opus-4-evaDaily')
    assert not is_claude_opus_47('claude-opus-4')


def test_neuter_template_audit_flags_broken_payload():
    """Feed a synthetic broken template through the audit predicate: missing
    opus-5 AND caps-divergent opus-5 must BOTH be flagged. If the audit ever
    degrades to a tautology this face goes red."""
    missing = _template_violations([
        {'model_id': OPUS48, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
    ])
    assert any('missing from template' in v for v in missing), missing
    divergent = _template_violations([
        {'model_id': OPUS48, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': OPUS5, 'capabilities': ['text'], 'rpm': 30, 'cost': 0.045},
    ])
    assert any(OPUS5 in v and 'caps' in v for v in divergent), divergent
    # …and the audit is green on a payload that is exactly right.
    good = [
        {'model_id': OPUS48, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
        {'model_id': OPUS5, 'capabilities': ['text', 'vision', 'thinking'],
         'rpm': 30, 'cost': 0.045},
    ]
    assert _template_violations(good) == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
