#!/usr/bin/env python3
"""Restart-independent BRIDGE evidence for the cache fixes.

The live acceptance (post-restart [CacheRoundRecord] logs) is the gold standard
and is NOT replaced by this. But while the manual :15000 restart hasn't landed,
this proves — through the ACTUAL freshly-imported new bytecode, on REAL body
shapes — the two fixes that are process-internal (not send-path-dependent):

  #1 mid_oow MISATTRIBUTION GATE (18c04a6): a round whose prefix bytes changed
     AND whose mid anchor is out-of-window must now bucket by its REAL culprit
     (body_change), NOT cache_mid_out_of_window. We drive the production
     detect_cache_break with a real body pair and assert the new bucket.

  #2 DROP-DEFAULT (6bcac3e): the new add_cache_breakpoints, with no env set,
     must place ZERO mid stepping-stones on a long real-shaped body — only the
     tail. We run the production function and count body markers.

  #3 ttl-flip (a34beae) is a SEND-PATH property (the _task_id chokepoint stamp in
     stream_llm_response) — an in-process detector replay CANNOT fully prove it;
     it stays gated on the live post-restart check. Reported honestly, not faked.

This complements debug/cache_cost_prepost_restart.py (which needs live logs).

Run: python debug/cache_replay_newcode_bridge.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _real_shaped_body(n_pairs: int):
    """A realistic OpenAI-shape agent tool-loop body (system + user + N
    assistant/tool pairs), the shape live traffic produces."""
    msgs = [{'role': 'system', 'content': 'S' * 40000},
            {'role': 'user', 'content': 'kick off the task'}]
    for i in range(n_pairs):
        msgs.append({'role': 'assistant', 'content': f'step {i} analysis',
                     'tool_calls': [{'id': f'c{i}', 'type': 'function',
                                     'function': {'name': 'read_files',
                                                  'arguments': '{}'}}]})
        msgs.append({'role': 'tool', 'tool_call_id': f'c{i}',
                     'content': f'tool result {i} ' + ('x ' * 60)})
    return {'model': 'aws.claude-opus-4.8', '_task_id': 'bridge-task',
            'tools': [{'type': 'function',
                       'function': {'name': 'read_files', 'parameters': {}}}],
            'messages': msgs}


def _body_markers(body):
    return [i for i, m in enumerate(body['messages'])
            if i > 0 and isinstance(m.get('content'), list)
            and any(isinstance(x, dict) and 'cache_control' in x
                    for x in m['content'])]


def check_2_drop_default():
    """#2 — new add_cache_breakpoints places NO mid marker by default."""
    os.environ.pop('TOFU_CACHE_MID_MODE', None)   # DEFAULT
    os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
    import lib as _lib
    _lib.CACHE_EXTENDED_TTL = True
    from lib.llm.cache import add_cache_breakpoints, _mid_placement_mode
    mode = _mid_placement_mode()
    placed_counts = []
    for n in (10, 20, 30, 40):
        body = _real_shaped_body(n)
        add_cache_breakpoints(body)
        placed_counts.append(len(_body_markers(body)))
    ok = (mode == 'drop') and all(c == 1 for c in placed_counts)
    print('── #2 DROP-DEFAULT (new add_cache_breakpoints, no env) ──')
    print(f'  resolved mode          = {mode!r}  (expect drop)')
    print(f'  body markers per length= {dict(zip((10,20,30,40), placed_counts))}  '
          f'(expect all 1 = tail only, ZERO mid)')
    print(f'  VERDICT: {"PASS" if ok else "FAIL"} — '
          f'{"drop places no mid stepping-stone" if ok else "a mid was still armed"}')
    return ok


def check_1_misattribution_gate():
    """#1 — a REAL body-changed round with out-of-window mid geometry must NOT
    be bucketed cache_mid_out_of_window by the production detect_cache_break."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.cache_tracking._detect import classify_verdict
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, routing_fingerprint, static_prefix_hash,
        wire_byte_prefix,
    )

    # A real prefix mutation: round-2 rewrites an already-cached message.
    base = [{'role': 'system', 'content': 'STATIC SYSTEM PROMPT ' * 40},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'analysis v1 of the cached prefix'},
            {'role': 'user', 'content': 'continue'},
            {'role': 'assistant', 'content': 'tail turn text'}]
    changed = [dict(m) for m in base]
    changed[2] = {'role': 'assistant',
                  'content': 'analysis v2 REWRITTEN cached prefix'}

    def pack(msgs):
        return (canonical_messages(msgs), static_prefix_hash(msgs),
                wire_byte_prefix(msgs))
    fp1, st1, wb1 = pack(base)
    fp2, st2, wb2 = pack(changed)
    rt = routing_fingerprint(key_hash='k', anthropic_beta='pc',
                             endpoint='https://gw/claude/messages')
    # mid anchor far from tail (span 30 > 20) on both rounds — the geometry
    # that WOULD add <mid-out-of-window> if not byte-identity-gated.
    mk = {'count': 4, 'sys': 1, 'tools': 1, 'ttls': [],
          'msg': [('mid', 0), ('tail', 0)], 'msg_blocks': [12, 42],
          'body_msg_blocks': [12, 42]}
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp1, '_wire_static': st1, '_wire_bytes': wb1,
          '_wire_routing': dict(rt), '_wire_markers': dict(mk)}
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp2, '_wire_static': st2, '_wire_bytes': wb2,
          '_wire_routing': dict(rt), '_wire_markers': dict(mk)}
    _cache_states.clear()
    detect_cache_break('bridge1', base, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break('bridge1', changed, None, 'claude-opus-4', usage=dict(u2))
    bucket = classify_verdict(r)
    ok = (r is not None and 'cache_mid_out_of_window' not in r
          and bucket != 'cache_mid_out_of_window')
    print('── #1 MISATTRIBUTION GATE (production detect_cache_break, real body pair) ──')
    print(f'  prefix bytes changed + mid out-of-window + read collapsed')
    print(f'  new bucket             = {bucket!r}  (expect NOT cache_mid_out_of_window)')
    print(f'  VERDICT: {"PASS" if ok else "FAIL"} — '
          f'{"real body-change wins, layout token no longer hijacks" if ok else "still mislabelled mid_oow"}')
    return ok


def main():
    print('BRIDGE evidence — real body shapes through freshly-imported NEW code')
    print('(live post-restart logs remain the gold standard; this does not replace them)\n')
    ok2 = check_2_drop_default()
    print()
    ok1 = check_1_misattribution_gate()
    print('\n── #3 ttl-flip (a34beae) ──')
    print('  SEND-PATH property (stream_llm_response chokepoint stamp) — an '
          'in-process detector replay cannot fully prove it. Stays gated on the '
          'LIVE post-restart check (ttl_flip→0). Not claimed here.')
    print(f'\nSUMMARY: #1 gate={"PASS" if ok1 else "FAIL"}, '
          f'#2 drop-default={"PASS" if ok2 else "FAIL"}, '
          f'#3 ttl-flip=LIVE-PENDING')
    return 0 if (ok1 and ok2) else 1


if __name__ == '__main__':
    sys.exit(main())
