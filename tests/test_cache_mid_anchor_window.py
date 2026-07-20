#!/usr/bin/env python3
"""Mid-history-anchor 20-block-lookback suite — root-fix the periodic
whole-prefix rewrite that was being laundered into ``upstream_identical``.

BACKGROUND (evidence, see this turn's investigation + the JOURNAL):
  Anthropic prompt caching only searches ~20 CONTENT BLOCKS backward from a
  breakpoint to find a prior cache entry to EXTEND. ``add_cache_breakpoints``
  plants a mid-history "stepping-stone" breakpoint that TRAILS the rolling tail
  so the tail can always reach the previously-written entry. But the trail was
  measured in MESSAGES (``_MID_TRAIL=12``) while the tail advances in BLOCKS
  (~3 blocks/tool-round), and the mid anchor only JUMPS every ``_MID_STEP=8``
  messages. Between jumps the tail keeps pulling away, so the mid→tail BLOCK
  span sawtooths 17→20→23→26 and SPENDS HALF THE ROUNDS past 20 — on those
  rounds the tail can't extend the mid entry and the whole prefix past the mid
  is re-written. Measured live: read collapses to the ~74–80k static floor on a
  BYTE-IDENTICAL, SAME-ROUTING body, mislabelled ``upstream_identical`` (the
  "blame the gateway" verdict the owner flagged).

THE FIX under test:
  A. LAYOUT (lib/llm/cache.py) — shrink ``_MID_TRAIL`` so the mid→tail BLOCK
     span stays ≤ ``_MID_LOOKBACK`` (20) on EVERY round, across content shapes
     (prose, empty-content, parallel tool calls), while keeping the anchor
     quantized (jumps, not every round) and never on the early-user turn.
  B. DETECTOR (wire_fingerprint.marker_signature + detect_cache_break) —
     fingerprint each message-marker's cumulative BLOCK position, add
     ``mid_anchor_out_of_window(sig)``, and give ``detect_cache_break`` a
     dedicated ``cache_mid_out_of_window`` verdict so a byte-identical,
     same-routing read-collapse whose mid anchor is out of the lookback window
     is NAMED a client-side breakpoint-layout miss — NEVER laundered into
     ``upstream_identical`` / ``server_side``.

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_mid_anchor_window.py
"""

from __future__ import annotations

import json as _json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _grow(rounds, *, prose=True, parallel=1):
    """A growing agent tool-loop body (OpenAI shape), like a real run_task."""
    msgs = [{'role': 'system', 'content': 'S' * 40000},
            {'role': 'user', 'content': 'task'}]
    for r in range(rounds):
        tcs = [{'id': f't{r}_{k}', 'type': 'function',
                'function': {'name': 'rf', 'arguments': '{}'}}
               for k in range(parallel)]
        msgs.append({'role': 'assistant',
                     'content': ('analysing the file now' if prose else ''),
                     'tool_calls': tcs})
        for k in range(parallel):
            msgs.append({'role': 'tool', 'tool_call_id': f't{r}_{k}',
                         'content': 'R' * 1400})
    return {'model': 'claude-sonnet-4', '_task_id': 'p',
            'tools': [{'type': 'function',
                       'function': {'name': 'rf', 'parameters': {}}}],
            'messages': msgs}


def _anthropic_blocks(msg):
    """Content-block count Anthropic sees for a message (the lookback unit)."""
    n = 0
    c = msg.get('content')
    if isinstance(c, list):
        n += len(c)
    elif isinstance(c, str) and c:
        n += 1
    if msg.get('tool_calls'):
        n += len(msg['tool_calls'])
    return max(1, n)


def _mid_tail_block_gap(body):
    """The mid→tail distance in CONTENT-BLOCK space (Anthropic's lookback unit).

    Returns None if fewer than 2 message-level markers (no mid armed yet)."""
    cum = []
    t = 0
    for m in body['messages']:
        cum.append(t)
        t += _anthropic_blocks(m)
    positions = []
    for i, m in enumerate(body['messages']):
        c = m.get('content')
        if i > 0 and isinstance(c, list):
            for bi, blk in enumerate(c):
                if isinstance(blk, dict) and blk.get('cache_control'):
                    positions.append(cum[i] + bi)
    if len(positions) < 2:
        return None
    return max(positions) - min(positions)


def _msg_marker_indices(body):
    return [i for i, m in enumerate(body['messages'])
            if i > 0 and isinstance(m.get('content'), list)
            and any(isinstance(x, dict) and 'cache_control' in x
                    for x in m['content'])]


# ─────────────────────────────────────────────────────────────────────────────
#  Part A — the LAYOUT fix: mid→tail block gap stays within the lookback window
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestMidAnchorWithinBlockWindow:

    def _lookback(self):
        from lib.llm.cache import _MID_LOOKBACK
        return _MID_LOOKBACK

    def test_block_gap_never_exceeds_lookback(self):
        """★ THE ROOT-FIX GUARD (failing-first on _MID_TRAIL=12). Across a long
        growing tool loop, the mid→tail BLOCK gap must NEVER exceed the ~20-block
        lookback — else the tail cannot extend the mid entry and the prefix is
        re-written that round. Old params sawtooth to 26–30 → this FAILS; the
        fix keeps every round ≤ lookback."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        lookback = self._lookback()
        worst = 0
        offenders = []
        for shape in (dict(prose=True, parallel=1),
                      dict(prose=False, parallel=1),
                      dict(prose=True, parallel=2)):
            for r in range(6, 64):
                body = _grow(r, **shape)
                add_cache_breakpoints(body)
                gap = _mid_tail_block_gap(body)
                if gap is None:
                    continue
                worst = max(worst, gap)
                if gap > lookback:
                    offenders.append((shape, r, gap))
        assert not offenders, (
            f'mid→tail BLOCK gap exceeded the {lookback}-block lookback on '
            f'{len(offenders)} rounds (peak={worst}); tail cannot extend the '
            f'mid entry → whole-prefix rewrite. First offenders: {offenders[:5]}')

    def test_mid_anchor_stays_quantized(self):
        """The anchor must still JUMP (occupy few distinct positions over many
        rounds), not move every round — else it is always a fresh write."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        seen = []
        for r in range(18, 40):
            body = _grow(r)
            add_cache_breakpoints(body)
            marks = _msg_marker_indices(body)
            if len(marks) == 2:
                seen.append(marks[0])
        assert seen, 'mid anchor never armed across the sampled rounds'
        assert len(set(seen)) < len(seen), \
            f'mid anchor moved every round (not quantized): {seen}'

    def test_mid_anchor_never_on_early_user(self):
        """The anti-oscillation invariant survives the trail shrink: the mid /
        tail marker must never collapse onto the first user turn (msg[1])."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        for r in range(4, 40):
            body = _grow(r)
            add_cache_breakpoints(body)
            assert 1 not in _msg_marker_indices(body), \
                f'r={r}: a marker landed on the early user turn (msg[1])'

    def test_total_never_exceeds_four(self):
        """Even after the fix, total markers never exceed Anthropic's hard 4."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        def _count(body):
            n = 0
            for m in body['messages']:
                c = m.get('content')
                if isinstance(c, list):
                    n += sum(1 for x in c
                             if isinstance(x, dict) and x.get('cache_control'))
            for t in body.get('tools') or []:
                fn = t.get('function', {})
                if isinstance(fn, dict) and fn.get('cache_control'):
                    n += 1
            return n
        for r in (2, 8, 16, 30, 50):
            body = _grow(r)
            add_cache_breakpoints(body)
            assert _count(body) <= 4, f'r={r}: exceeded 4 breakpoints'


# ─────────────────────────────────────────────────────────────────────────────
#  Part B — the DETECTOR dimension: marker block positions + out-of-window bucket
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestMarkerBlockWindow:

    def test_marker_signature_captures_block_positions(self):
        """marker_signature must fingerprint each message-marker's cumulative
        BLOCK position so mid→tail window reachability is computable."""
        from lib.tasks_pkg.wire_fingerprint import marker_signature
        # Anthropic-shape body: system hoisted; messages carry block lists.
        body = {
            'model': 'claude-opus-4',
            'system': [{'type': 'text', 'text': 'SYS',
                        'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
            'messages': [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'u0'}]},
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'a1'},
                    {'type': 'text', 'text': 'mid',
                     'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]},
                {'role': 'tool', 'content': [{'type': 'text', 'text': 'r'}]},
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'tail',
                     'cache_control': {'type': 'ephemeral'}}]},
            ],
        }
        sig = marker_signature(body)
        assert 'msg_blocks' in sig, 'marker_signature must expose msg_blocks'
        assert len(sig['msg_blocks']) == 2, \
            f'two message markers expected, got {sig["msg_blocks"]}'

    def test_mid_anchor_out_of_window_predicate(self):
        """mid_anchor_out_of_window is True only when the two message markers'
        block span exceeds the lookback; ≤ lookback and <2 markers are inert."""
        from lib.tasks_pkg.wire_fingerprint import mid_anchor_out_of_window
        near = {'msg_blocks': [10, 28]}   # span 18 ≤ 20
        far = {'msg_blocks': [10, 36]}    # span 26 > 20
        assert mid_anchor_out_of_window(near) is False
        assert mid_anchor_out_of_window(far) is True
        # <2 markers → inert (no mid armed)
        assert mid_anchor_out_of_window({'msg_blocks': [10]}) is False
        assert mid_anchor_out_of_window({'msg_blocks': []}) is False
        # missing side (mid-deploy / non-Claude) → inert
        assert mid_anchor_out_of_window(None) is False
        assert mid_anchor_out_of_window({}) is False


def _identical_body():
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, wire_byte_prefix,
    )
    msgs = [{'role': 'system', 'content': 'STATIC SYSTEM'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'}]
    return (msgs, canonical_messages(msgs), static_prefix_hash(msgs),
            wire_byte_prefix(msgs))


def test_detector_names_mid_out_of_window_not_upstream():
    """★ CORE (B). Body byte-identical AND routing identical both rounds, the
    read collapsed, and the mid anchor is OUT of the lookback window → the miss
    must be NAMED ``cache_mid_out_of_window`` (a client-side breakpoint-layout
    miss), NEVER ``server_side`` / laundered into the upstream verdict."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'mid-oow'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    # mid anchor far from tail (span 30 > 20) on BOTH rounds.
    mk = {'count': 4, 'sys': 1, 'tools': 1, 'ttls': [],
          'msg': [('mid', 0), ('tail', 0)], 'msg_blocks': [12, 42]}
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None, 'expected a break (read collapsed to the floor)'
    assert 'cache_mid_out_of_window' in r, (
        f'a byte-identical, same-routing read-collapse with the mid anchor out '
        f'of the lookback window must be named cache_mid_out_of_window — got: {r}')
    assert 'server_side' not in r, f'must NOT enter the server_side branch: {r}'
    blob = _json.dumps(r).lower()
    assert 'lookback' in blob or 'window' in blob, (
        f'the verdict must name the lookback-window layout miss: {r}')


def test_detector_NEUTER_without_block_positions_launders_to_upstream():
    """NEUTER control — proves the block-position fingerprint is load-bearing.
    The SAME collapse, but with marker signatures that DROP ``msg_blocks`` (the
    pre-fix signature) → the detector can't see the out-of-window layout and the
    miss launders back into the byte-identical ``upstream cache miss`` verdict."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'mid-oow-neuter'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    # Pre-fix signature: NO msg_blocks field.
    mk = {'count': 4, 'sys': 1, 'tools': 1, 'ttls': [],
          'msg': [('mid', 0), ('tail', 0)]}
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    assert 'cache_mid_out_of_window' not in r, (
        f'NEUTER: without msg_blocks the out-of-window layout MUST NOT be named '
        f'(this is exactly the blind spot the fingerprint closes) — got: {r}')
    blob = _json.dumps(r).lower()
    assert 'upstream cache miss' in blob, (
        f'NEUTER: without block positions the miss launders to the '
        f'byte-identical upstream verdict — got: {r}')


def test_classify_verdict_maps_mid_out_of_window_bucket():
    """The single-source bucketer maps the new verdict key to its own bucket so
    live records + offline replay count it identically (never as upstream)."""
    from lib.tasks_pkg.cache_tracking._detect import (
        BUCKET_MID_WINDOW, classify_verdict,
    )
    assert classify_verdict({'cache_mid_out_of_window': 'x'}) == BUCKET_MID_WINDOW


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
