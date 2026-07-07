"""Ground-truth equivalence: the BACKEND ghost reconcile
(``lib.conversations.reconcile.reconcile_conversation_messages``) must produce
the SAME swept-set + tail verdict as the FRONTEND Case-D sequence
(``_sweepBuriedGhostAssistants`` then ``_classifyGhostTail`` in
``static/js/main/main_init_tasks.js``), over ONE shared fixture corpus.

WHY
---
The frontend Case-D path INFERS settled lifecycle state in JS and truncates
persisted history (``syncConversationToServer(conv, {allowTruncate:true})``) —
the separation-of-concerns violation the backend GET-path reconcile
(``routes/conversations.py::_reconcile_conv_on_get_blocking``) was built to
retire. Before deleting the JS sweep + its truncating PUTs we must PROVE the
backend produces the identical tail decision the JS did — including the
buried-ghost, special-turn, and tail cases — not assume "it should be covered".

EQUIVALENCE AXIS — ``cache_prefix_count=0``
-------------------------------------------
The JS has NO prompt-cache-prefix guard; the backend has one
(``cache_prefix_count``). We therefore run the backend at ``cache_prefix_count=0``
— the documented byte-identical-behaviour default — so the two are compared on
the same axis. The LIVE GET path passes the real prefix count, so the backend
deliberately will NOT sweep a buried ghost sitting inside the immutable cache
prefix (a ghost the JS WOULD have swept). That is a FEATURE (cache-neutrality),
not a regression: ``get_cache_prefix_count`` reflects LIVE in-memory cache state,
which resets to 0 on server restart / cache eviction, so the next idle GET
sweeps the formerly-in-prefix ghost — it is never stranded forever. This is
pinned by ``test_in_prefix_ghost_divergence_is_prefix_gated`` below.

We compare the APPLIED RESULT (surviving message keys in order + each survivor's
``finishReason`` + a ``changed`` flag), NOT the raw verdict string — the backend
verdict token is ``'interrupt'`` while the JS token is ``'interrupted'``, but
BOTH apply the same mutation (stamp ``finishReason='interrupted'``, keep the
message), so the applied output is identical.

Runs the REAL shipped JS under node (per project convention: node for
extraction/eval, no JSDOM); skips cleanly when node isn't installed.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
INIT_TASKS_JS = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── The shared corpus (single source of truth for BOTH sides) ──────────────
# Each message carries a unique 'k' key so we compare surviving SETS by identity
# (not by content). 'k' is ignored by every reconcile predicate, so it does not
# perturb the verdict.
def _u(k, content='Q'):
    return {'k': k, 'role': 'user', 'content': content}


def _a(k, **kw):
    m = {'k': k, 'role': 'assistant', 'content': ''}
    m.update(kw)
    return m


FIXTURES = [
    # 1. Bare empty tail → delete.
    {'name': 'bare_empty_tail', 'messages': [_u('u0'), _a('a1')]},
    # 2. Thinking-only tail → interrupt (stamp, keep).
    {'name': 'thinking_only_tail', 'messages': [_u('u0'), _a('a1', thinking='I')]},
    # 3. Settled (finishReason+usage) tail → kept, no change.
    {'name': 'settled_tail_finish',
     'messages': [_u('u0'), _a('a1', finishReason='stop', usage={'input_tokens': 5})]},
    # 4. Content tail → kept, no change.
    {'name': 'content_tail',
     'messages': [_u('u0'), _a('a1', content='hi', finishReason='stop')]},
    # 5. The mr3jfcw10pianj shape: 4 buried empties + a settled tail.
    {'name': 'buried_4_ghosts', 'messages': [
        _u('u0'),
        _a('g1', finishReason='aborted', usage={}),  # buried ghost (bodyless badge)
        _a('g2'),                                     # buried ghost
        _a('g3'),                                     # buried ghost
        _a('r1', content='real reply', finishReason='stop'),  # keep
        _u('u1', content='Q2'),                       # keep
        _a('g4'),                                     # buried ghost
        _a('t1', content='tail', finishReason='stop'),  # tail keep
    ]},
    # 6. Buried settled-but-bodyless bubble → swept (buried is more aggressive
    #    than the tail: it removes even a finishReason-bearing empty).
    {'name': 'buried_settled_bodyless', 'messages': [
        _u('u0'),
        _a('g1', finishReason='aborted', usage={'input_tokens': 3}),  # buried → swept
        _u('u1', content='Q2'),
        _a('t1', content='reply', finishReason='stop'),  # tail keep
    ]},
    # 7. Buried SPECIAL turns (VU / endpoint planner) → never swept.
    {'name': 'special_turns_kept', 'messages': [
        _u('u0'),
        _a('vu', _isVirtualUser=True),        # buried special → keep
        _a('ep', _isEndpointPlanner=True),    # buried special → keep
        _a('t1', content='reply', finishReason='stop'),  # tail keep
    ]},
    # 8. Buried real tool round → kept.
    {'name': 'buried_real_round_kept', 'messages': [
        _u('u0'),
        _a('g1', toolRounds=[{'status': 'done', 'toolName': 'run_command'}]),  # keep
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 9. Buried error envelope → kept.
    {'name': 'buried_error_kept', 'messages': [
        _u('u0'),
        _a('g1', error={'kind': 'internal'}),  # keep (renders an error block)
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 10. Buried thinking-only → kept (recovered reasoning renders a block).
    {'name': 'buried_thinking_kept', 'messages': [
        _u('u0'),
        _a('g1', thinking='some recovered reasoning'),  # keep
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 11. _epIteration:0 is NON-special in BOTH implementations (backend
    #     `!=0` clause, JS `0` falsy) → an empty _epIteration:0 buried turn is
    #     swept. Locks that parity edge.
    {'name': 'ep_iteration_zero_nonspecial', 'messages': [
        _u('u0'),
        _a('g1', _epIteration=0),  # non-special empty → buried ghost → swept
        _a('t1', content='reply', finishReason='stop'),
    ]},
    # 12. All buried ghosts then an empty tail → sweep all buried, delete tail.
    {'name': 'all_ghosts_empty_tail', 'messages': [_a('g1'), _a('t1')]},
    # 13. Single empty assistant (no user) → sweep skipped (<2), tail delete.
    {'name': 'single_empty', 'messages': [_a('a1')]},
    # 14. Empty list → no-op.
    {'name': 'empty_list', 'messages': []},
    # 15. Tail with a real tool round but empty content → kept.
    {'name': 'tail_real_round_kept', 'messages': [
        _u('u0'), _a('a1', toolRounds=[{'status': 'searching', 'toolContent': 'out'}])]},
]


def _normalize(messages, changed):
    return {
        'survivors': [m['k'] for m in messages],
        'finish': {m['k']: m.get('finishReason') for m in messages},
        'changed': bool(changed),
    }


def _backend_results(fixtures, prefix=0):
    from lib.conversations.reconcile import reconcile_conversation_messages
    res = {}
    for fx in fixtures:
        out, changed = reconcile_conversation_messages(
            copy.deepcopy(fx['messages']), prefix)
        res[fx['name']] = _normalize(out, changed)
    return res


_JS_HARNESS = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));  // main_init_tasks.js (declarations only)
const fixtures = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// Replay the EXACT Case-D sequence: sweep buried ghosts, then classify the
// (post-sweep) tail and apply delete/interrupt — mirroring
// static/js/main/main_init_tasks.js Case D.
function reconcileJS(messages) {
  const conv = { id: 'x', messages: messages.map(m => Object.assign({}, m)) };
  let changed = false;
  const removed = _sweepBuriedGhostAssistants(conv);
  if (removed > 0) changed = true;
  if (conv.messages.length) {
    const tail = conv.messages[conv.messages.length - 1];
    const v = _classifyGhostTail(tail);
    if (v === 'delete') { conv.messages.pop(); changed = true; }
    else if (v === 'interrupted') { tail.finishReason = 'interrupted'; changed = true; }
  }
  return { messages: conv.messages, changed };
}

const res = {};
for (const fx of fixtures) {
  const { messages, changed } = reconcileJS(fx.messages);
  const finish = {};
  for (const m of messages) finish[m.k] = (m.finishReason === undefined ? null : m.finishReason);
  res[fx.name] = {
    survivors: messages.map(m => m.k),
    finish,
    changed,
  };
}
console.log(JSON.stringify(res));
"""


def _js_results(fixtures):
    harness = os.path.join(HERE, '_recon_equiv_harness.js')
    fx_file = os.path.join(HERE, '_recon_equiv_fixtures.json')
    with open(harness, 'w') as f:
        f.write(_JS_HARNESS)
    with open(fx_file, 'w') as f:
        json.dump(fixtures, f)
    try:
        proc = subprocess.run(
            ['node', harness, INIT_TASKS_JS, fx_file],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (harness, fx_file):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_reconcile_js_backend_equivalence():
    """Backend == JS on every fixture (applied survivors + finishReason + changed)."""
    backend = _backend_results(FIXTURES, prefix=0)
    js = _js_results(FIXTURES)
    assert set(backend) == set(js)
    mismatches = {name: {'backend': backend[name], 'js': js[name]}
                  for name in backend if backend[name] != js[name]}
    assert not mismatches, (
        'backend/JS reconcile DIVERGED on:\n' + json.dumps(mismatches, indent=2))


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_special_turn_guard_detects_divergence(monkeypatch):
    """NC-1 (teeth): if the backend's buried predicate DROPS the special-turn
    guard, it sweeps the buried VU/planner turns the JS keeps → the equivalence
    test MUST detect divergence on `special_turns_kept`. Proves the test isn't a
    tautology."""
    js = _js_results(FIXTURES)
    import lib.conversations.reconcile as rec

    def _fake_buried(msg):
        # Same as the real predicate MINUS the _is_special_turn guard.
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            return False
        if (msg.get('content') or '').strip():
            return False
        if (msg.get('thinking') or '').strip():
            return False
        if msg.get('error'):
            return False
        if rec._has_real_round(msg):
            return False
        return True

    monkeypatch.setattr(rec, 'is_buried_empty_ghost', _fake_buried)
    backend = _backend_results(FIXTURES, prefix=0)
    assert backend['special_turns_kept'] != js['special_turns_kept'], (
        'neuter dropped the special-turn guard but the equivalence comparison '
        'failed to notice — the test has no teeth')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_thinking_tail_detects_divergence(monkeypatch):
    """NC-2 (teeth): if the backend tail classifier IGNORES `thinking` (deletes
    a thinking-only husk instead of stamping interrupt), it diverges from the JS
    on `thinking_only_tail` → the comparison MUST catch it."""
    js = _js_results(FIXTURES)
    import lib.conversations.reconcile as rec
    _orig = rec.classify_ghost_tail

    def _fake_tail(msg):
        v = _orig(msg)
        return 'delete' if v == 'interrupt' else v  # collapse interrupt→delete

    monkeypatch.setattr(rec, 'classify_ghost_tail', _fake_tail)
    backend = _backend_results(FIXTURES, prefix=0)
    assert backend['thinking_only_tail'] != js['thinking_only_tail'], (
        'neuter collapsed interrupt→delete but the comparison failed to notice')


def test_in_prefix_ghost_divergence_is_prefix_gated():
    """Constraint #1: a buried ghost INSIDE the cache prefix is NOT swept by the
    backend (cache-neutrality), but IS swept once the prefix is 0. Same input,
    two prefix values — proves the divergence from JS is purely prefix-gated and
    SELF-HEALS when the prefix resets (server restart / cache eviction → next
    idle GET sweeps it). It is never stranded forever."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    msgs = [_u('u0'),
            _a('g1'),  # buried empty ghost at idx 1
            _a('t1', content='reply', finishReason='stop')]

    # prefix=0 → swept (matches JS).
    out0, changed0 = reconcile_conversation_messages(copy.deepcopy(msgs), 0)
    assert [m['k'] for m in out0] == ['u0', 't1']
    assert changed0 is True

    # prefix=2 → idx<2 protected → the buried ghost survives (intended,
    # cache-neutral). Nothing swept, settled tail → no change.
    out2, changed2 = reconcile_conversation_messages(copy.deepcopy(msgs), 2)
    assert [m['k'] for m in out2] == ['u0', 'g1', 't1']
    assert changed2 is False


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_known_whitespace_thinking_tail_divergence():
    """DOCUMENTED, INTENTIONAL non-equivalence (found while checking predicate
    parity): a tail whose `thinking` is WHITESPACE-ONLY.

    Backend ``classify_ghost_tail`` uses ``(thinking or '').strip()`` → '   '
    → falsy → DELETE. JS ``_classifyGhostTail`` uses truthy ``lastMsg.thinking``
    (no trim) → '   ' truthy → INTERRUPTED (keep+stamp). The backend is stricter
    and more correct (whitespace is not real reasoning), and since the backend
    becomes the AUTHORITATIVE reconciler this is an improvement, not a
    regression. Pinned here so the difference is RECORDED, not hidden — this is
    the one predicate-level divergence in the corpus, and it is on a shape that
    does not occur in practice (reasoning deltas are never pure whitespace)."""
    from lib.conversations.reconcile import reconcile_conversation_messages
    fx = [_u('u0'), _a('a1', thinking='   ')]
    out, _changed = reconcile_conversation_messages(copy.deepcopy(fx), 0)
    assert [m['k'] for m in out] == ['u0'], 'backend deletes whitespace-thinking tail'

    if _node_available():
        js = _js_results([{'name': 'ws', 'messages': fx}])['ws']
        assert js['survivors'] == ['u0', 'a1'], 'JS keeps whitespace-thinking tail'
        assert js['finish']['a1'] == 'interrupted', 'JS stamps interrupted'
