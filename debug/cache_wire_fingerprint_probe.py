#!/usr/bin/env python3
"""Step-1 probe — post-TRANSLATION wire-fingerprint diff for cache misses.

Supersedes ``debug/cache_prefix_byte_probe.py`` for the traceability question.
That probe had two structural blind spots the owner flagged:
  1. ``shared_len = len(conv_n) - 2`` EXCLUDED the round-N tail message — the
     one message whose wrapper flips ``str`` ↔ ``[{"type":"text"}]`` as the
     moving cache marker lands on it. So the exact false-positive source was
     never in its diff window.
  2. It diffed the OpenAI-form body only (``add_cache_breakpoints`` stage),
     stopping one transform SHORT of the Anthropic-protocol wire, where
     ``openai_body_to_anthropic`` re-serialises ``arguments`` via
     ``json.dumps(ensure_ascii=False)``.

This probe captures the FULL transform chain — ``build_body`` →
``add_cache_breakpoints`` → (on the anthropic arm) ``openai_body_to_anthropic``
— for round N and round N+1, INCLUDING the tail, on BOTH envelopes, then diffs
via ``lib.tasks_pkg.wire_fingerprint`` (the envelope-agnostic canonicaliser
that Step 2 uses in production). The verdict for each round pair:
  * canonical diff EMPTY  → our bytes were identical → a miss here is a
    PROVEN server-side miss (not eliminated — proven).
  * canonical diff NON-empty → names the exact ``key.field`` we mutated → a
    CLIENT-caused miss with a concrete culprit.

It also runs three adversarial scenarios that MUST be classified correctly:
  A. append-only growth (benign)                → no prefix culprit
  B. moving cache marker flips tail str↔block   → no prefix culprit (erased)
  C. a 5th image arrives, retro-shrinking the   → prefix culprit named
     first 4 (the _downscale threshold=5 bug)      (image field changed)

Usage:
    python debug/cache_wire_fingerprint_probe.py
    python debug/cache_wire_fingerprint_probe.py --model aws.claude-opus-4.8 --rounds 12
"""

import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib as _lib  # noqa: E402
from lib.llm import build_body  # noqa: E402
from lib.llm.cache import add_cache_breakpoints  # noqa: E402
from lib.llm.anthropic_outbound import openai_body_to_anthropic  # noqa: E402
from lib.tasks_pkg.wire_fingerprint import (  # noqa: E402
    canonical_messages,
    diff_canonical,
    static_prefix_hash,
)

_SIG = 'EpcBCkgIB' + ('x' * 200) + 'ABCD=='


def _tiny_png(seed: int) -> str:
    """A minimal but DISTINCT data-URI per seed (so a re-encode differs)."""
    import base64
    # 1x1 PNG-ish payload; vary a byte by seed so hashes differ per image.
    raw = bytes([137, 80, 78, 71, 13, 10, 26, 10] + [seed & 0xff] * 40)
    return 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')


def _assistant(i):
    return {
        'role': 'assistant', 'content': '',
        'reasoning_content': f'Thinking about step {i}. ' * 10,
        'thinking_signature': _SIG + str(i),
        'tool_calls': [{
            'id': f'call_{i:04d}', 'type': 'function',
            'function': {'name': 'read_files',
                         # keys deliberately NOT alphabetical, to prove the
                         # anthropic re-dump (sort) is erased by the canon.
                         'arguments': json.dumps({'path': f'lib/m_{i}.py', 'a': 1})},
        }],
    }


def _tool(i):
    return {'role': 'tool', 'tool_call_id': f'call_{i:04d}',
            'content': f'# file {i}\n' + ('source line\n' * 40)}


def _conv(n, sys_prompt):
    msgs = [{'role': 'system', 'content': sys_prompt},
            {'role': 'user', 'content': 'Investigate the cache behavior.'}]
    for i in range(n):
        msgs.append(_assistant(i))
        msgs.append(_tool(i))
    return msgs


def _wire(model, msgs, arm):
    """Full transform chain → the post-translation wire messages list."""
    body = build_body(model, copy.deepcopy(msgs), max_tokens=1024,
                      thinking_enabled=True, thinking_depth='medium',
                      tools=None, stream=True)
    body['_task_id'] = ''
    add_cache_breakpoints(body, log_prefix='')
    if arm == 'anthropic':
        body = openai_body_to_anthropic(body)
    return body['messages']


def _diff_round_pair(model, n, sys_prompt, arm, *, mutate=None):
    """Build round-N and round-(N+1) wires; include the tail; canon-diff.

    ``mutate`` optionally transforms the N+1 stored conv BEFORE the wire
    transform (to inject an adversarial change). Returns (culprits, lenN,
    lenN1, static_same).
    """
    conv_n = _conv(n, sys_prompt)
    conv_n1 = _conv(n, sys_prompt)
    conv_n1.append(_assistant(n))
    conv_n1.append(_tool(n))
    if mutate:
        mutate(conv_n, conv_n1)

    wn = _wire(model, conv_n, arm)
    wn1 = _wire(model, conv_n1, arm)

    cn = canonical_messages(wn)
    cn1 = canonical_messages(wn1)
    # Compare the SHARED region INCLUDING round N's tail: everything in conv_n
    # (its full length) maps to the same leading messages of conv_n1. This is
    # the fix for the old len-2 blind spot.
    shared = len(cn)
    culprits = diff_canonical(cn[:shared], cn1[:shared])
    static_same = static_prefix_hash(wn) == static_prefix_hash(wn1)
    return culprits, len(cn), len(cn1), static_same


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='aws.claude-opus-4.8')
    ap.add_argument('--rounds', type=int, default=10)
    args = ap.parse_args()
    _lib.CACHE_EXTENDED_TTL = True

    sys_prompt = 'You are Tofu. ' + ('Follow the conventions strictly. ' * 300)

    print(f'== Wire-fingerprint prefix probe ==  model={args.model} '
          f'rounds={args.rounds}\n')

    for arm in ('openai', 'anthropic'):
        print(f'--- ENVELOPE: {arm} ---')

        # Scenario A: benign append-only growth (the normal round-over-round).
        cul, ln, ln1, static_same = _diff_round_pair(
            args.model, args.rounds, sys_prompt, arm)
        print(f'  [A benign growth]   shared={ln} -> {ln1}  '
              f'static_prefix_same={static_same}  prefix_culprits={cul or "NONE ✓"}')

        # Scenario B: force the tail marker to sit on a message that is a bare
        # string in round N but wrapped by the breakpoint pass — this is the
        # str↔block flip. add_cache_breakpoints already does this naturally, so
        # A exercises it; here we additionally assert the LAST shared msg (the
        # round-N tail) carries no culprit.
        last_shared_culprit = [c for c in cul if not c.startswith('len ')]
        print(f'  [B marker flip]     tail-in-window, culprits on shared tail='
              f'{last_shared_culprit or "NONE ✓"}')

        # Scenario C: a 5th image arrives → _downscale retro-shrinks the first
        # four (threshold=5). We simulate the downscaler's effect: the same
        # early image messages get a DIFFERENT base64 in round N+1.
        def _mutate_5th_image(cn, cn1):
            # Put 4 images into the first user turn in BOTH rounds…
            for c in (cn, cn1):
                c[1]['content'] = [{'type': 'text', 'text': 'imgs:'}] + [
                    {'type': 'image_url', 'image_url': {'url': _tiny_png(k)}}
                    for k in range(4)
                ]
            # …then in round N+1 a 5th image lands AND the first four are
            # re-encoded (new bytes) — exactly the threshold=5 retro-shrink.
            cn1[1]['content'] = [{'type': 'text', 'text': 'imgs:'}] + [
                {'type': 'image_url', 'image_url': {'url': _tiny_png(k + 100)}}
                for k in range(4)
            ] + [{'type': 'image_url', 'image_url': {'url': _tiny_png(200)}}]

        cul_c, _, _, static_c = _diff_round_pair(
            args.model, args.rounds, sys_prompt, arm, mutate=_mutate_5th_image)
        _img_culprit = [c for c in cul_c if '.content' in c]
        print(f'  [C 5th-image retro] prefix_culprits={cul_c or "NONE"}  '
              f'-> image-content culprit named={"YES ✓" if _img_culprit else "NO ✗"}  '
              f'static_prefix_same={static_c}')
        print()

    print('VERDICT KEY:')
    print('  A/B culprits=NONE  → benign transforms (wrapping/marker/arg-reorder)')
    print('                       are correctly ERASED; a real miss on such a')
    print('                       round is a PROVEN server-side miss.')
    print('  C culprit NAMED    → the _downscale threshold=5 retro-shrink is a')
    print('                       CLIENT-caused miss the canon CATCHES (was')
    print('                       previously laundered into "stochastic server").')


if __name__ == '__main__':
    main()
