#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3.5 step 3 — ConvView seam-hardening guards.

Owner-directed additions on top of the step-2 seam (docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §5):

① COLLAPSE — upsertMessage and apply were two near-identical upserts inside
   the seam itself (the fork moved INTO the seam). Now `apply(convId, idx,
   msg, opts)` is the ONLY entity; `upsertMessage` is a thin alias that
   preserves its legacy append-default-false semantics, and the identity
   sweep (_evictByMsgId) runs on ALL paths.
   Guards: test_upsert_is_thin_alias_of_apply (static),
           test_upsert_alias_runtime_parity (JSDOM).

② LIVE-BUBBLE GUARD — apply's docstring promised "do not target the live
   #streaming-msg bubble" but the code did not enforce it: `_findMsgEl`
   resolves by data-msg-id FIRST, #streaming-msg carries one, and per-round
   auto-translate completes while the turn is still streaming — a raw
   `outerHTML = renderMessage(...)` there wipes the live zones. Now apply
   REFUSES (console.warn + return false), and `_evictByMsgId` never removes
   #streaming-msg.
   Guards: test_apply_refuses_live_streaming_bubble (JSDOM, NEUTER-style:
           positive control proves the same call replaces a static node),
           test_sweep_never_evicts_streaming_bubble (JSDOM).

③ ORDER INVARIANT — (a) apply warns loudly when appending a non-tail
   message (index drift), (b) the cheapest hard proof of "rendering is
   traceable": after a send → edit → regen flow through the seam, the
   `#chatInner .message` data-msg-id sequence MUST equal conv.messages'
   _msgId sequence.
   Guards: test_apply_warns_on_midlist_append (JSDOM),
           test_dom_order_matches_messages_after_send_edit_regen (JSDOM).

④ RETIRED-CLASS SWEEP — step 2's byte-parity fix removed the
   `stream-seg-narration` marker class; the inert styles.css block is now
   deleted, and production JS must never assign that class again.
   Guards: test_stream_seg_narration_gone_from_production_js (static),
           test_inert_css_block_removed (static).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_convview_apply_guards.py
"""

from __future__ import annotations

import glob
import os
import re

import pytest

from tests._jsdom import run_harness, JS_DIR, ROOT

pytestmark = pytest.mark.unit

CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')
STYLES = os.path.join(ROOT, 'static', 'styles.css')


# ════════════════════════════════════════════════════════════════════
# ① Collapse — static alias check + runtime parity
# ════════════════════════════════════════════════════════════════════

def test_upsert_is_thin_alias_of_apply():
    """upsertMessage must DELEGATE to apply — no second upsert implementation.

    The pre-collapse upsertMessage had its own renderMessage + outerHTML +
    insertAdjacentHTML (and no identity sweep). The alias resolves idx and
    forwards with {append: !!opts.append}. This static guard pins: the alias
    body contains the delegation, and the raw write ops appear ONLY in the
    single apply implementation (conv_view's other raw ops are the other
    lifecycle methods — upsertMessage itself must have none).
    """
    with open(CONV_VIEW, encoding='utf-8') as f:
        src = f.read()
    m = re.search(
        r'upsertMessage:\s*function\s*\(convId,\s*msg,\s*opts\)\s*\{(.*?)\n    \},\n',
        src, re.DOTALL)
    assert m, 'upsertMessage not found in conv_view.js'
    body = m.group(1)
    assert 'ConvView.apply(' in body, (
        'upsertMessage no longer delegates to ConvView.apply — the ① collapse '
        'was reverted; the seam forked again')
    assert 'outerHTML' not in body and 'insertAdjacentHTML' not in body, (
        'upsertMessage re-grew its own raw DOM writes — the ① collapse says '
        'apply() is the ONLY entity that writes')
    assert '{ append: !!opts.append }' in body, (
        'the alias must preserve legacy semantics: append defaults FALSE '
        '(upsertMessage callers relied on replace-only unless opts.append)')


_ALIAS_PARITY_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

const conv = { id: 'c1', messages: [
  { _msgId: 'm1', role: 'user', content: 'u1' },
  { _msgId: 'm2', role: 'assistant', content: 'a1' },
]};
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">' +
        '<div class="message" id="msg-0" data-msg-id="m1">u1-static</div>' +
        '<div class="message" id="msg-1" data-msg-id="m2">a1-static</div>' +
        '</div></div></body>',
  targets: [process.argv[2]],
  globals: {
    activeConvId: 'c1',
    conversations: [conv],
    renderMessage: (msg, idx) =>
      '<div class="message" id="msg-' + idx + '" data-msg-id="' + msg._msgId +
      '">' + msg.content + '-R</div>',
    _ensureMsgId: () => {},
    _convRenderFingerprint: () => 'fp',
    _lastRenderedFingerprint: '',
  },
});

/* Legacy semantics 1: no existing node + no opts.append → refuse (false). */
const ghost = { _msgId: 'm9', role: 'user', content: 'ghost' };
conv.messages.push(ghost);
const r1 = window.ConvView.upsertMessage('c1', ghost);
check('alias_no_existing_no_append_refuses', r1 === false);
check('alias_refusal_did_not_append', !document.getElementById('msg-2'));
conv.messages.pop();

/* Legacy semantics 2: existing node → replace in place. */
const r2 = window.ConvView.upsertMessage('c1', conv.messages[0]);
check('alias_existing_replaced', r2 === true &&
  document.getElementById('msg-0').textContent === 'u1-R');

/* ① sweep on ALL paths: plant a drifted twin for m2, upsert → twin evicted. */
const inner = document.getElementById('chatInner');
inner.insertAdjacentHTML('beforeend',
  '<div class="message" id="msg-7" data-msg-id="m2">TWIN</div>');
check('twin_planted', inner.querySelectorAll('[data-msg-id="m2"]').length === 2);
const r3 = window.ConvView.upsertMessage('c1', conv.messages[1]);
check('alias_sweep_evicts_twin', r3 === true &&
  inner.querySelectorAll('[data-msg-id="m2"]').length === 1 &&
  document.getElementById('msg-1').textContent === 'a1-R');

/* Legacy semantics 3: opts.append=true with no existing → appends. */
const tail = { _msgId: 'm3', role: 'assistant', content: 'a2' };
conv.messages.push(tail);
const r4 = window.ConvView.upsertMessage('c1', tail, { append: true });
check('alias_append_true_appends', r4 === true &&
  !!document.querySelector('[data-msg-id="m3"]'));

report();
"""


def test_upsert_alias_runtime_parity():
    """The alias preserves legacy semantics AND gains the identity sweep."""
    output = run_harness(
        target_js=CONV_VIEW,
        body_js=_ALIAS_PARITY_BODY,
        min_pass=6,
        label='convview-alias-parity',
    )
    assert 'PASS alias_no_existing_no_append_refuses' in output, output
    assert 'PASS alias_existing_replaced' in output, output
    assert 'PASS alias_sweep_evicts_twin' in output, output
    assert 'PASS alias_append_true_appends' in output, output


# ════════════════════════════════════════════════════════════════════
# ② Live-bubble guard (NEUTER: positive control proves replace works)
# ════════════════════════════════════════════════════════════════════

_LIVE_GUARD_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

const conv = { id: 'c1', messages: [
  { _msgId: 'm1', role: 'user', content: 'u1' },
  { _msgId: 'm9', role: 'assistant', content: 'LIVE' },
]};
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">' +
        '<div class="message" id="msg-0" data-msg-id="m1">u1-static</div>' +
        '<div class="message" id="streaming-msg" data-msg-id="m9">' +
          '<div class="message-body" id="streaming-body">' +
            '<div data-zone="content">live-zones</div>' +
          '</div>' +
        '</div>' +
        '</div></div></body>',
  targets: [process.argv[2]],
  globals: {
    activeConvId: 'c1',
    conversations: [conv],
    renderMessage: (msg, idx) =>
      '<div class="message" id="msg-' + idx + '" data-msg-id="' + msg._msgId +
      '">' + msg.content + '-R</div>',
    _ensureMsgId: () => {},
    _convRenderFingerprint: () => 'fp',
    _lastRenderedFingerprint: '',
  },
});

const warns = [];
const _ow = console.warn;
console.warn = (...a) => { warns.push(a.join(' ')); };

/* ② the guard: apply targeting the LIVE bubble must refuse, DOM untouched. */
const smBefore = document.getElementById('streaming-msg').outerHTML;
const kidsBefore = document.getElementById('chatInner').childElementCount;
const r1 = window.ConvView.apply('c1', 1, conv.messages[1]);
check('apply_on_live_bubble_refused', r1 === false);
check('live_bubble_dom_untouched',
  document.getElementById('streaming-msg') &&
  document.getElementById('streaming-msg').outerHTML === smBefore);
check('no_static_twin_appended', document.getElementById('chatInner').childElementCount === kidsBefore);
check('refusal_warned', warns.some(w => w.indexOf('REFUSED') >= 0));

/* NEUTER / positive control: the SAME call on a STATIC node replaces it —
 * proving the refusal above is the guard, not a dead apply. */
const r2 = window.ConvView.apply('c1', 0, conv.messages[0]);
check('POSITIVE_CONTROL_static_replaced', r2 === true &&
  document.getElementById('msg-0').textContent === 'u1-R');

/* ② sweep exemption: plant a STATIC twin carrying m9 BEFORE the live
 * bubble (the drifted-bubble shape — _findMsgEl's data-msg-id query resolves
 * the first match in document order, so the static twin is the apply target,
 * not the live bubble). Apply on it — the sweep must evict IT but keep
 * #streaming-msg. */
const inner = document.getElementById('chatInner');
document.getElementById('streaming-msg').insertAdjacentHTML('beforebegin',
  '<div class="message" id="msg-1" data-msg-id="m9">stale-static</div>');
const r3 = window.ConvView.apply('c1', 1, conv.messages[1]);
check('apply_on_static_twin_succeeds', r3 === true);
check('streaming_bubble_survives_sweep', !!document.getElementById('streaming-msg'));
check('only_one_m9_static_remains',
  inner.querySelectorAll('[data-msg-id="m9"]').length === 2 &&  // static + streaming
  document.getElementById('msg-1').textContent === 'LIVE-R');

console.warn = _ow;
report();
"""


def test_apply_refuses_live_streaming_bubble():
    """apply must never replace / evict the live #streaming-msg (owner ②).

    The positive control is the load-bearing half: the same apply() replaces
    a static node in the same DOM, so the refusal on the live node is the
    guard firing — not a broken apply.
    """
    output = run_harness(
        target_js=CONV_VIEW,
        body_js=_LIVE_GUARD_BODY,
        min_pass=8,
        label='convview-live-guard',
    )
    for needle in ('PASS apply_on_live_bubble_refused',
                   'PASS live_bubble_dom_untouched',
                   'PASS no_static_twin_appended',
                   'PASS refusal_warned',
                   'PASS POSITIVE_CONTROL_static_replaced',
                   'PASS streaming_bubble_survives_sweep',
                   'PASS only_one_m9_static_remains'):
        assert needle in output, f'{needle}\n{output}'


# ════════════════════════════════════════════════════════════════════
# ③ Order invariant — mid-list warn + DOM/messages sequence equality
# ════════════════════════════════════════════════════════════════════

_ORDER_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

const conv = { id: 'c1', messages: [
  { _msgId: 'm1', role: 'user', content: 'u1' },
  { _msgId: 'm2', role: 'assistant', content: 'a1' },
  { _msgId: 'm3', role: 'user', content: 'u2' },
  { _msgId: 'm4', role: 'assistant', content: 'a2' },
]};
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    activeConvId: 'c1',
    conversations: [conv],
    renderMessage: (msg, idx) =>
      '<div class="message" id="msg-' + idx + '" data-msg-id="' + msg._msgId +
      '">' + msg.content + '</div>',
    _ensureMsgId: () => {},
    _convRenderFingerprint: () => 'fp',
    _lastRenderedFingerprint: '',
  },
});

const warns = [];
console.warn = (...a) => { warns.push(a.join(' ')); };

function domSeq() {
  return Array.from(document.querySelectorAll('#chatInner .message'))
    .map(el => el.getAttribute('data-msg-id'));
}
function msgSeq() { return conv.messages.map(m => m._msgId); }
function sameSeq(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/* send: initial four applies append in order. */
for (let i = 0; i < conv.messages.length; i++) {
  window.ConvView.apply('c1', i, conv.messages[i]);
}
check('after_send_order_matches', sameSeq(domSeq(), msgSeq()));

/* edit: apply on existing mid-list node replaces IN PLACE (no reorder). */
conv.messages[1].content = 'a1-edited';
window.ConvView.apply('c1', 1, conv.messages[1]);
check('after_edit_order_matches', sameSeq(domSeq(), msgSeq()) &&
  document.querySelector('[data-msg-id="m2"]').textContent === 'a1-edited');

/* regen: truncate tail (removeMessage) then push+apply a fresh assistant. */
window.ConvView.removeMessage('c1', conv.messages[3]);
conv.messages.splice(3, 1);
const fresh = { _msgId: 'm4b', role: 'assistant', content: 'a2-fresh' };
conv.messages.push(fresh);
window.ConvView.apply('c1', 3, fresh);
check('after_regen_order_matches', sameSeq(domSeq(), msgSeq()));

/* upsert replace keeps order. */
window.ConvView.upsertMessage('c1', conv.messages[2]);
check('after_upsert_order_matches', sameSeq(domSeq(), msgSeq()));

/* THE ANCHOR: after the whole send/edit/regen flow, DOM seq === doc seq. */
check('ANCHOR_dom_seq_equals_doc_seq', sameSeq(domSeq(), msgSeq()));

/* ③a mid-list append with NO existing node → loud warn (drift surface).
 * NOTE: this step deliberately drifts DOM vs doc (the ghost's idx fallback
 * clobbers msg-1) — that destruction is exactly what the loud warn exists
 * to surface, so the anchor runs BEFORE it. */
const midGhost = { _msgId: 'm2x', role: 'assistant', content: 'drifted' };
conv.messages.splice(1, 0, midGhost);   // mid-list in the doc, absent in DOM
window.ConvView.apply('c1', 1, midGhost);
check('midlist_append_warned',
  warns.some(w => w.indexOf('MID-LIST') >= 0));

report();
"""


def test_dom_order_matches_messages_after_send_edit_regen():
    """Cheapest hard proof of traceable rendering: DOM order == doc order."""
    output = run_harness(
        target_js=CONV_VIEW,
        body_js=_ORDER_BODY,
        min_pass=6,
        label='convview-order-invariant',
    )
    for needle in ('PASS after_send_order_matches',
                   'PASS after_edit_order_matches',
                   'PASS after_regen_order_matches',
                   'PASS after_upsert_order_matches',
                   'PASS midlist_append_warned',
                   'PASS ANCHOR_dom_seq_equals_doc_seq'):
        assert needle in output, f'{needle}\n{output}'


# ════════════════════════════════════════════════════════════════════
# ④ Retired-class sweep — static token guards
# ════════════════════════════════════════════════════════════════════

_RETIRED_TOKEN = 'stream-seg-narration'


def _strip_js_comments(src: str) -> str:
    """Remove // and /* */ comments ONLY — string CONTENTS are preserved.

    The ④ token guard looks for class ASSIGNMENTS, which live inside string
    literals (`narr.className = '… stream-seg-narration'`). A stripper that
    also drops string contents is blind to exactly what it must catch (the
    NEUTER caught this). String tracking is still needed so an apostrophe
    inside a string can't fake a comment opener — but the string's bytes are
    kept verbatim.
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == '//':
            j = src.find('\n', i)
            i = n if j < 0 else j
        elif two == '/*':
            j = src.find('*/', i + 2)
            i = n if j < 0 else j + 2
        elif src[i] in ('"', "'", '`'):
            q = src[i]
            out.append(src[i])
            i += 1
            while i < n:
                if src[i] == '\\':
                    out.append(src[i:i + 2])
                    i += 2
                elif src[i] == q:
                    out.append(src[i])
                    i += 1
                    break
                else:
                    out.append(src[i])
                    i += 1
        else:
            out.append(src[i])
            i += 1
    return ''.join(out)


def test_stream_seg_narration_gone_from_production_js():
    """No production JS may ASSIGN the retired class (owner ④).

    Scans every static/js/**/*.js, excluding *.nc_copy.js scratch files and
    bundle-*.js build artifacts (regenerated at server start from the real
    sources). Comments are stripped first — accurate historical mentions in
    comments are fine; CODE must never carry the token (a class assignment,
    a query selector — both would resurrect the fork).
    """
    offenders = []
    for path in glob.glob(os.path.join(JS_DIR, '**', '*.js'), recursive=True):
        base = os.path.basename(path)
        if base.endswith('.nc_copy.js') or base.startswith('bundle-'):
            continue
        with open(path, encoding='utf-8') as f:
            code = _strip_js_comments(f.read())
        if _RETIRED_TOKEN in code:
            offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, (
        'retired class `stream-seg-narration` reappeared in production JS '
        'CODE (comments excluded): ' + ', '.join(offenders) +
        ' — the step-2 byte-parity unification is being re-forked; use the '
        'settled `seg-narration` class (see docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §3)')


def test_inert_css_block_removed():
    """styles.css must not contain a `.stream-seg-narration` rule (owner ④).

    The block at former styles.css:6158-6166 went inert when step 2 dropped
    the marker class (its values are duplicated verbatim by
    `.seg-timeline .seg-narration` at :6096). Comments are stripped before
    the check, so accurate historical prose is fine; a live RULE is not.
    """
    with open(STYLES, encoding='utf-8') as f:
        src = f.read()
    code = re.sub(r'/\*.*?\*/', ' ', src, flags=re.DOTALL)
    assert _RETIRED_TOKEN not in code, (
        'an inert .stream-seg-narration CSS rule is back in styles.css — '
        'step 3 deleted it because the class no longer exists in production '
        'JS; style the settled `.seg-narration` instead')


def test_NEUTER_token_guard_detects_injected_assignment():
    """NEUTER: the ④ JS guard must fire on an injected class assignment."""
    poisoned = 'var d = document.createElement("div");\nd.className = "md-content seg-narration stream-seg-narration";\n'
    assert _RETIRED_TOKEN in _strip_js_comments(poisoned), (
        'NEUTER FAILED: the token guard is blind to a real class assignment')


if __name__ == '__main__':
    for fn in (test_upsert_is_thin_alias_of_apply,
               test_stream_seg_narration_gone_from_production_js,
               test_inert_css_block_removed,
               test_NEUTER_token_guard_detects_injected_assignment):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:300])
    for fn in (test_upsert_alias_runtime_parity,
               test_apply_refuses_live_streaming_bubble,
               test_dom_order_matches_messages_after_send_edit_regen):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:300])
