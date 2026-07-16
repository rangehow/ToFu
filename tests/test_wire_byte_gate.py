"""tests/test_wire_byte_gate.py

The TRUE-byte gate on the "upstream cache eviction" verdict.

WHY
---
``detect_cache_break`` calls a byte-identical miss an "upstream cache eviction"
(NOT a random server fault). That verdict rested on ``canonical_messages`` — a
DELIBERATELY LOSSY fingerprint: it strips ``cache_control``, collapses
``str`` ↔ ``[{type:text}]``, canonicalises tool-arg key order, and does NOT
hash ``reasoning_details`` (``build_body`` synthesises that field). So
"canonical identical" does NOT prove "the SERIALIZED wire bytes were
identical". A round can rebuild ``reasoning_details``, merge consecutive
same-role turns, reorder JSON fields, or switch OpenAI↔Anthropic envelope while
canonical reports "identical" — and the verdict's literal claim *"bytes were
byte-identical"* would be FALSE, laundering a real content/serialization change
into an eviction.

``wire_byte_prefix`` hashes ``json.dumps(msg)`` per prefix message (only
``cache_control`` stripped) so the detector can REFUSE the byte-identical
eviction claim when the true bytes diverged, and name the honest set of causes.

This suite pins:
  1. ``wire_byte_prefix`` catches a reasoning_details-only change that
     ``canonical_messages`` is blind to (the core lossy-canonical divergence).
  2. It IGNORES a moved ``cache_control`` marker (the legitimately-mobile
     element) — no false positive on the rolling tail.
  3. ``diff_byte_prefix`` names the divergent message with a ``<bytes>`` tag.
  4. End-to-end: a byte-diverged-but-canonical-identical round is NOT called an
     "upstream cache eviction"; the verdict names the wire-byte change.
  5. NEUTER: without the true-byte gate the SAME round IS laundered into the
     eviction verdict — proving the gate is load-bearing.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_wire_byte_gate.py
"""

from __future__ import annotations

import pytest

from lib.tasks_pkg.wire_fingerprint import (
    canonical_messages,
    diff_byte_prefix,
    diff_canonical,
    wire_byte_prefix,
)

pytestmark = pytest.mark.unit


def _asst_with_reasoning(rd_value):
    """An assistant turn whose ``reasoning_details`` differs but whose
    canonical thinking (reasoning_content + thinking_signature) is fixed."""
    return {
        'role': 'assistant',
        'content': 'Let me look.',
        'reasoning_content': 'stable thought',
        'thinking_signature': 'sig-fixed-' + ('a' * 20),
        # build_body rebuilds this field; canonical does NOT hash it.
        'reasoning_details': rd_value,
    }


# ── 1. reasoning_details change: canonical blind, true-byte catches ──

def test_reasoning_details_change_invisible_to_canonical_visible_to_bytes():
    prev = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'hi'},
            _asst_with_reasoning([{'type': 'reasoning', 'id': 'r1'}])]
    cur = [{'role': 'system', 'content': 'sys'},
           {'role': 'user', 'content': 'hi'},
           _asst_with_reasoning([{'type': 'reasoning', 'id': 'r2-REBUILT'}])]

    # Canonical fingerprint: IDENTICAL (reasoning_details is not hashed).
    assert diff_canonical(canonical_messages(prev),
                          canonical_messages(cur)) == [], (
        'precondition: canonical_messages must be blind to reasoning_details — '
        'that is exactly the lossy blind spot the byte gate closes')

    # True-byte fingerprint: DIVERGES on the assistant message.
    pb = wire_byte_prefix(prev)
    cb = wire_byte_prefix(cur)
    byte_diff = diff_byte_prefix(pb, cb)
    assert byte_diff, 'wire_byte_prefix must catch the reasoning_details change'
    assert any(c.startswith('<bytes>') for c in byte_diff)


# ── 2. moved cache_control marker: true-byte must IGNORE it ──

def test_moved_cache_control_marker_is_not_a_byte_divergence():
    """cache_control is the one legitimately-mobile element (the rolling tail
    marker moves every round). wire_byte_prefix strips it, so a marker
    appearing/moving must NOT register as a byte change."""
    base_user = {'role': 'user',
                 'content': [{'type': 'text', 'text': 'stable turn'}]}
    prev = [{'role': 'system', 'content': 'sys'}, dict(base_user)]
    # Same message, but now carrying a cache_control marker on its block.
    marked_user = {'role': 'user', 'content': [
        {'type': 'text', 'text': 'stable turn',
         'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]}
    cur = [{'role': 'system', 'content': 'sys'}, marked_user]

    assert diff_byte_prefix(wire_byte_prefix(prev),
                            wire_byte_prefix(cur)) == [], (
        'a moved/added cache_control marker must NOT count as a byte change — '
        'cache_control is stripped before hashing')


# ── 3. diff_byte_prefix output shape ──

def test_diff_byte_prefix_tags_and_length():
    prev = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'a'}]
    cur_changed = [{'role': 'system', 'content': 'sys'},
                   {'role': 'user', 'content': 'a-DIFFERENT'}]
    d = diff_byte_prefix(wire_byte_prefix(prev), wire_byte_prefix(cur_changed))
    assert d and all(c.startswith('<bytes>') for c in d)
    # Length change reported distinctly.
    longer = wire_byte_prefix(prev + [{'role': 'assistant', 'content': 'x'}])
    dl = diff_byte_prefix(wire_byte_prefix(prev), longer)
    assert any(c.startswith('byte-len') for c in dl)


# ── 4. End-to-end detector: byte-diverged round is NOT called eviction ──

def _usage_with_wire(msgs, *, cache_read, cache_write):
    return {
        'cache_read_tokens': cache_read,
        'cache_write_tokens': cache_write,
        '_wire_fp': canonical_messages(msgs),
        '_wire_static': '',
        '_wire_bytes': wire_byte_prefix(msgs),
    }


def test_detector_byte_divergence_not_laundered_into_eviction():
    from lib.tasks_pkg.cache_tracking import detect_cache_break
    from lib.tasks_pkg.cache_tracking._state import _cache_states

    conv = 'byte-gate-1'
    # Round 1: establish a warm prefix (big read so a drop is a "break").
    r1 = [{'role': 'system', 'content': 'S' * 200},
          {'role': 'user', 'content': 'u1'},
          _asst_with_reasoning([{'type': 'reasoning', 'id': 'r1'}])]
    detect_cache_break(conv, r1, None, 'claude-opus-4',
                       usage=_usage_with_wire(r1, cache_read=90000,
                                              cache_write=0))
    # Round 2: SAME prefix but reasoning_details rebuilt on the assistant turn
    # (canonical identical, true bytes differ) + a big read DROP → a break.
    r2 = [{'role': 'system', 'content': 'S' * 200},
          {'role': 'user', 'content': 'u1'},
          _asst_with_reasoning([{'type': 'reasoning', 'id': 'r2-REBUILT'}]),
          {'role': 'user', 'content': 'u2'}]
    out = detect_cache_break(conv, r2, None, 'claude-opus-4',
                             usage=_usage_with_wire(r2, cache_read=40000,
                                                    cache_write=0))
    _cache_states.clear()
    assert out is not None
    blob = str(out)
    # It must NOT claim byte-identical eviction — the bytes were NOT identical.
    assert 'upstream cache eviction' not in blob, (
        'a round whose true bytes diverged must NOT be called a byte-identical '
        f'eviction — got: {out}')
    assert 'wire bytes changed between turns' in blob
    assert '<bytes>' in blob


def test_detector_NEUTER_without_byte_gate_launders_to_eviction():
    """NEUTER: strip the _wire_bytes signal (pre-gate behaviour) and the SAME
    reasoning_details-rebuild round IS laundered into the eviction verdict,
    because canonical alone is blind to it. Proves the byte gate is
    load-bearing."""
    from lib.tasks_pkg.cache_tracking import detect_cache_break
    from lib.tasks_pkg.cache_tracking._state import _cache_states

    conv = 'byte-gate-neuter'

    def _usage_no_bytes(msgs, *, cache_read):
        u = _usage_with_wire(msgs, cache_read=cache_read, cache_write=0)
        u.pop('_wire_bytes')  # NEUTER: hide the true-byte signal
        return u

    r1 = [{'role': 'system', 'content': 'S' * 200},
          {'role': 'user', 'content': 'u1'},
          _asst_with_reasoning([{'type': 'reasoning', 'id': 'r1'}])]
    detect_cache_break(conv, r1, None, 'claude-opus-4',
                       usage=_usage_no_bytes(r1, cache_read=90000))
    r2 = [{'role': 'system', 'content': 'S' * 200},
          {'role': 'user', 'content': 'u1'},
          _asst_with_reasoning([{'type': 'reasoning', 'id': 'r2-REBUILT'}]),
          {'role': 'user', 'content': 'u2'}]
    out = detect_cache_break(conv, r2, None, 'claude-opus-4',
                             usage=_usage_no_bytes(r2, cache_read=40000))
    _cache_states.clear()
    assert out is not None
    # Without the byte gate the lossy canonical says "identical" → eviction.
    assert 'upstream cache eviction' in str(out), (
        'NEUTER expectation: without _wire_bytes the reasoning_details rebuild '
        f'is invisible → laundered into eviction — got: {out}')
