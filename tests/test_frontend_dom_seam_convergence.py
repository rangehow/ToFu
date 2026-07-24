#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3.5 — DOM-apply single-seam: failing-first anchors + ratchet.

TESTS-FIRST by design. Docs: docs/RENDER_CONTRACT_PHASE3_5_PLAN.md.

THREE guards, three different colours today:

1. ``test_convview_exposes_single_apply_entry`` — **RED anchor.**
   Phase 3.5 §5 step 2 introduces ``ConvView.apply(convId, idx, msg)`` as THE
   single public DOM-apply entry (wraps renderMessage + _evictByMsgId +
   fingerprint). Today ConvView has no such method — this fails until it lands.

2. ``test_live_vs_cold_narration_byte_parity`` — **RED anchor (JSDOM).**
   The acceptance claim, generalized from Phase 3's reducer parity to the DOM:
   for the SAME narration fact, the LIVE translation preview painted into the
   streaming bubble (``_renderStreamingTranslatePreview``) must be byte-identical
   to the COLD settled render's narration slot (the markup
   ``tool_rounds.js:_renderNarrationSegments`` emits, painted by
   ``_applyPartialByRoundToSettled``). Today the live painter creates
   ``class="md-content seg-narration stream-seg-narration"`` while the settled
   renderer emits ``class="md-content seg-narration"`` — a one-class byte
   divergence between two projection paths for one fact. Both sides of the
   comparison are produced by REAL production code (translation_render.js);
   only the settled slot's initial markup is hand-built to the exact
   tool_rounds.js:3387 contract. NEUTER included: an injected byte difference
   must be detected, and the observed divergence must be exactly the known
   class-list one (so the test rots with the code, not with the harness).

3. ``test_raw_dom_write_ratchet`` — **GREEN today, guards the floor.**
   Static audit of the 13 non-seam files: a single-pass tokenizer strips
   comments + strings, then count
   ``innerHTML=`` / ``outerHTML=`` / ``insertAdjacentHTML(`` / ``appendChild(``
   / ``.remove()``, assert each file's count is ≤ its 2026-07-24 baseline
   (monotonic-decrease ratchet, same pattern as test_frontend_api_isolation.py).
   conv_view.js is excluded — it IS the seam. NEUTER included: poisoning a
   file's source in-memory with one extra raw op must trip the detector.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_dom_seam_convergence.py
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import run_harness, JS_DIR, ROOT

pytestmark = pytest.mark.unit

CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')
TRANSLATION_RENDER = os.path.join(JS_DIR, 'ui', 'translation_render.js')


# ════════════════════════════════════════════════════════════════════
# 1. ConvView.apply existence anchor (RED until Phase 3.5 step 2)
# ════════════════════════════════════════════════════════════════════

def test_convview_exposes_single_apply_entry():
    """Phase 3.5's single public DOM-apply entry must exist on ConvView.

    RED on HEAD: conv_view.js exposes upsertMessage / removeMessage /
    removeAfter / replaceAll / startStreaming / finalizeStreaming — six
    lifecycle methods, no unified ``apply``. The 58 CONTENT-DERIVED raw write
    sites in the §2 table converge onto this one method; the anchor flips
    GREEN the commit it lands.
    """
    with open(CONV_VIEW, encoding='utf-8') as f:
        src = f.read()
    assert re.search(r'\bapply\s*:\s*function\b', src) or \
           re.search(r'\bapplyMessage\s*:\s*function\b', src), (
        'PHASE 3.5 RED ANCHOR: ConvView has no single apply entry. '
        'Phase 3.5 step 2 adds ConvView.apply(convId, idx, msg) — the one '
        'public method every CONTENT-DERIVED write routes through '
        '(docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §5). This test stays RED '
        'until that lands; do NOT silence it by renaming an existing method.')


# ════════════════════════════════════════════════════════════════════
# 2. Live-vs-cold narration byte parity (RED anchor, JSDOM)
# ════════════════════════════════════════════════════════════════════

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">' +
        '<div class="message" id="streaming-msg" data-msg-id="m1">' +
          '<div class="message-body" id="streaming-body">' +
            '<div class="panel-body">' +
              '<div class="ptool-turn" data-llm-round="L0"></div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="message" id="msg-0" data-msg-id="m1">' +
          '<div class="message-body">' +
            '<div class="seg-timeline" data-seg-timeline="1">' +
              '<div class="md-content seg-narration" data-seg-round="L0"></div>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '</div></div></body>',
  targets: [process.argv[2]],   // the REAL static/js/ui/translation_render.js
  globals: {
    activeConvId: 'c1',
    stripNoTranslateTags: (s) => s,
    isNearBottom: () => false,
    scrollToBottom: () => {},
  },
});

const ZH = '第一轮旁白';
const BY_ROUND = { 0: ZH };

// ── LIVE path: the real streaming translation preview painter. ──
const liveOk = window._renderStreamingTranslatePreview('c1', 'm1', ZH, BY_ROUND);
check('live_preview_painted', liveOk === true);
const liveNarr = document.querySelector(
  '#streaming-msg .stream-seg-narration[data-seg-round="L0"]');
check('live_narration_node_exists', !!liveNarr);

// ── COLD path: the real settled-slot painter over a tool_rounds-contract slot. ──
const coldOk = window._applyPartialByRoundToSettled('c1', 0, BY_ROUND);
check('cold_settled_painted', coldOk === true);
const coldNarr = document.querySelector(
  '#msg-0 .seg-narration[data-seg-round="L0"]');
check('cold_narration_node_exists', !!coldNarr);

// ── THE ANCHOR: one narration fact ⇒ one byte-identical DOM fragment. ──
const liveOuter = liveNarr ? liveNarr.outerHTML : '<missing-live>';
const coldOuter = coldNarr ? coldNarr.outerHTML : '<missing-cold>';
console.error('LIVE  : ' + liveOuter);
console.error('COLD  : ' + coldOuter);
check('ANCHOR_live_cold_narration_byte_identical', liveOuter === coldOuter);

// ── Sanity: the divergence, when present, is ONLY the live side's extra
//    `stream-seg-narration` class (documents the exact fix target; if some
//    OTHER byte starts differing this check fails too, forcing a re-audit). ──
const normalized = liveOuter.replace(' stream-seg-narration', '');
check('divergence_is_exactly_the_extra_live_class',
      liveOuter !== coldOuter && normalized === coldOuter);

// ── NEUTER: the comparator must detect an injected byte difference. ──
const coldNarr2 = document.querySelector('#msg-0 .seg-narration[data-seg-round="L0"]');
if (coldNarr2) coldNarr2.innerHTML = coldNarr2.innerHTML + 'X';
const drifted = document.querySelector('#msg-0 .seg-narration[data-seg-round="L0"]');
check('NEUTER_injected_byte_difference_detected',
      !!drifted && drifted.outerHTML !== coldOuter);

report();
"""


def test_live_vs_cold_narration_byte_parity():
    """One narration fact, one DOM fragment — live preview == cold settled render.

    RED on HEAD: the live painter adds `stream-seg-narration` to the class list
    (translation_render.js:175) while the settled renderer emits only
    `md-content seg-narration` (tool_rounds.js:3387). Phase 3.5 routes both
    through one projection (ConvView.apply → renderMessage), making the live
    and cold bytes identical; which side changes is the implementer's choice.
    """
    output = run_harness(
        target_js=TRANSLATION_RENDER,
        body_js=_BODY,
        min_pass=7,
        label='dom-seam-narration-parity',
    )
    assert 'PASS live_preview_painted' in output, output
    assert 'PASS cold_settled_painted' in output, output
    assert 'PASS NEUTER_injected_byte_difference_detected' in output, output
    # The sanity check documents today's exact divergence; when the fix lands
    # on EITHER side, live==cold makes the anchor pass and this sanity check
    # flips to its equality branch — update it in the same commit.
    assert 'PASS ANCHOR_live_cold_narration_byte_identical' in output, (
        'PHASE 3.5 RED ANCHOR: live translation preview and cold settled '
        'render emit different bytes for the same narration fact '
        '(see stderr for the two fragments). Converge both onto one '
        'projection per docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §3.\n' + output)


# ════════════════════════════════════════════════════════════════════
# 3. Raw DOM-write ratchet (GREEN today; monotonic decrease)
# ════════════════════════════════════════════════════════════════════

_RAW_PATS = [
    r'\b(?:inner|outer)HTML\s*=',
    r'\binsertAdjacentHTML\s*\(',
    r'\bappendChild\s*\(',
    r'\.remove\s*\(\s*\)',
]

# 2026-07-24 baseline, measured with the single-pass tokenizer in
# _scan_raw_dom_ops (docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §4). A file's
# count may only go DOWN as its CONTENT-DERIVED sites converge onto
# ConvView.apply; any NEW raw write trips the ratchet. conv_view.js is
# excluded — it IS the seam (its raw ops are the allowed writes).
_RATCHET_BASELINE = {
    'static/js/main/main_send_pipeline.js': 23,
    'static/js/ui/streaming_render.js': 21,
    'static/js/ui/translation_render.js': 18,
    'static/js/image-gen.js': 18,
    'static/js/ui/sse_pipeline.js': 17,
    'static/js/main/main_conv_lifecycle.js': 10,
    'static/js/ui/chat_render.js': 10,
    'static/js/ui/edit_message.js': 7,
    'static/js/ui/stream_lifecycle.js': 6,
    'static/js/main/main_translating_bubble.js': 6,
    'static/js/image-gen-batch.js': 5,
    'static/js/main/main_regen_continue.js': 4,
    'static/js/core/conversations.js': 4,
}


def _scan_raw_dom_ops(src: str) -> int:
    """Count raw DOM-mutation ops in JS source via a single-pass tokenizer.

    A regex-strip pass gets confused by real JS — a quote inside a template
    literal / regex flips the string state and silently swallows the rest of
    the file (the first 2026-07-24 audit undercounted main_send_pipeline.js
    23→1 that way, leaving the ratchet blind to 22 real writes). This scanner
    walks the source once, tracking // and /* */ comments and the three
    string delimiters with backslash escapes, so only CODE is matched. Known
    limitation: regex literals are not tokenized (a quote inside a regex can
    still flip state); the baselines were measured with this same scanner, so
    the ratchet is self-consistent.
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
            i += 1
            while i < n:
                if src[i] == '\\':
                    i += 2
                elif src[i] == q:
                    i += 1
                    break
                else:
                    i += 1
        else:
            out.append(src[i])
            i += 1
    code = ''.join(out)
    return sum(len(re.findall(p, code)) for p in _RAW_PATS)


def test_raw_dom_write_ratchet():
    """No file may gain raw DOM writes beyond its Phase-3.5 baseline."""
    violations = []
    for rel, baseline in _RATCHET_BASELINE.items():
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            count = _scan_raw_dom_ops(f.read())
        if count > baseline:
            violations.append(
                f'{rel}: {count} raw DOM ops > baseline {baseline} '
                f'(+{count - baseline})')
    assert not violations, (
        'RAW-WRITE RATCHET TRIPPED — new direct #chatInner mutation(s) '
        'appeared. Route content writes through ConvView.apply '
        '(docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §2); if the new write is '
        'genuinely STRUCT-ONLY / PENDING-PLACEHOLDER, justify it in the plan '
        'and lower another file instead:\n  ' + '\n  '.join(violations))


def test_ratchet_baseline_matches_plan_total():
    """The baselines sum to the plan's §2.14 tally (149 non-seam raw ops)."""
    assert sum(_RATCHET_BASELINE.values()) == 149, (
        f'baseline sum {sum(_RATCHET_BASELINE.values())} != 149 — the plan '
        '§2.14 tally and this ratchet drifted apart; update both together')


def test_NEUTER_ratchet_detects_injected_raw_op():
    """NEUTER: poisoning a file's source with one extra raw op must increment
    the count — proves the audit is load-bearing, not decorative."""
    rel = 'static/js/main/main_send_pipeline.js'   # lowest baseline (1)
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        clean = f.read()
    baseline = _scan_raw_dom_ops(clean)
    poisoned = clean + '\nvar x = document.getElementById("chatInner");\nx.innerHTML = "y";\n'
    after = _scan_raw_dom_ops(poisoned)
    assert after == baseline + 1, (
        f'NEUTER FAILED: injected innerHTML= moved count {baseline} → {after}, '
        'expected +1; the ratchet scan is blind to a real raw write')


if __name__ == '__main__':
    for fn in (test_convview_exposes_single_apply_entry,
               test_raw_dom_write_ratchet,
               test_ratchet_baseline_matches_plan_total,
               test_NEUTER_ratchet_detects_injected_raw_op):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:300])
    try:
        test_live_vs_cold_narration_byte_parity()
        print('  PASS test_live_vs_cold_narration_byte_parity')
    except Exception as e:  # noqa: BLE001
        print('  RED  test_live_vs_cold_narration_byte_parity ::', str(e)[:300])
