#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3.5 §7.4 — the reconnect byte-parity anchor (RED by design).

THE CLAIM (docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §7.4): for ONE in-flight turn,
the `#streaming-body` subtree produced by the LIVE-PAINT path (the tab holding
the SSE stream, `streamBufs` fed by deltas) MUST be byte-identical to the one
produced by a COLD-OPEN RECONNECT (`connectToTask` re-seeds the buffer from the
persisted message checkpoint) at the same logical instant — content zone,
thinking zone, AND **status zone** (the only place `phase` shows).

**RED today, by design** — and red in exactly one place: the checkpoint
fallback in `_streamFrameArg` (health_stream_timer.js) makes content and
thinking byte-identical across the two arms, but `phase` lives ONLY in the
buffer (`phase: buf.phase`, no document fallback — it is buffer runtime state
that never touches `conv.messages`). A cold-open reconnect seeds the buffer
WITHOUT phase, so the live arm paints the `llm_thinking` phase block into the
status zone while the reconnect arm paints the default waiting pulse.

This is the §7 streamBufs-retirement acceptance anchor. The owner's ruling
(plan §7.4): phase belongs to the REDUCER's live session state — NEVER the
message document (runtime state must not pollute the SSOT). The retirement
moves phase there and makes the reconnect arm read it from the same place;
this anchor then flips GREEN. Landing it RED *before* the retirement is the
failing-first discipline — the §7 commit gets a full red→green evidence chain
instead of a self-reported "didn't break anything".

NEUTER: making the two arms' checkpoint content differ MUST flip the content
comparison red too — the comparator is load-bearing on both zones, not
vacuously green on the side that happens to match today.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_reconnect_parity_anchor.py
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

HEALTH_TIMER = os.path.join(JS_DIR, 'core', 'health_stream_timer.js')
STREAMING_UI = os.path.join(JS_DIR, 'ui', 'streaming_ui.js')


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

/* Shared doc state at checkpoint K: the trailing STREAMING assistant message
 * carries the checkpointed content/thinking/rounds — the ONLY thing a cold
 * reconnect can read (phase is nowhere in it). */
const conv = { id: 'c1', messages: [
  { _msgId: 'm0', role: 'user', content: 'go' },
  { _msgId: 'm1', role: 'assistant', content: 'CHECKPOINT-CONTENT',
    thinking: 'CHECKPOINT-THINKING', toolRounds: [] },
]};
const PHASE = { phase: 'llm_thinking', detail: 'Thinking…', detailKey: '' };

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">' +
        '<div class="message" id="streaming-msg" data-msg-id="m1">' +
          '<div class="message-body" id="streaming-body"></div>' +
        '</div></div></div></body>',
  targets: [process.argv[2], process.argv[4]],   // health_stream_timer + streaming_ui
  globals: {
    activeConvId: 'c1',
    conversations: [conv],
    streamBufs: new Map(),
    /* deterministic painter stubs — SHARED by both arms, so any byte
     * difference comes from the DATA path, not the painters. */
    renderMarkdown: (s) => 'MD(' + s + ')',
    escapeHtml: (s) => String(s),
    t: (k) => k,
    Icon: () => '',
    isNearBottom: () => true,
    scrollToBottom: () => {},
    getToolRoundsFromMsg: (m) => (m && m.toolRounds) || [],
    _syncToolRoundsDOM: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderMcpLoginHintHtml: () => '',
    renderTurnProvenanceHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _getChatContainer: () => document.getElementById('chatContainer'),
    buildTurnNav: () => {},
    updateSendButton: () => {},
    _updateStreamTimerUI: () => {},
    renderToolRoundsHTML: () => '',
    renderCompactedBadge: () => '',
    _renderThinkingBlock: (th) => '<details>' + th + '</details>',
    _mdCache: { get: () => null, set: () => {} },
    requestAnimationFrame: (fn) => fn(),
  },
});

function freshBody() {
  /* Replace #streaming-body so streaming_ui's _streamZoneCache re-derives
   * the zone map for the new arm. */
  const sm = document.getElementById('streaming-msg');
  const old = document.getElementById('streaming-body');
  const fresh = old.cloneNode(false);
  fresh.innerHTML = '';
  sm.replaceChild(fresh, old);
  return fresh;
}
function snapshot() {
  const body = document.getElementById('streaming-body');
  const z = (n) => {
    const el = body.querySelector('[data-zone="' + n + '"]');
    return el ? el.innerHTML : '<no-zone>';
  };
  return { content: z('content'), thinking: z('thinking'), status: z('status') };
}

/* ── LIVE arm: the tab holding the SSE stream — buffer carries the checkpoint
 *    fields AND the live phase (deltas stamped both). ── */
streamBufs.set('c1', {
  content: 'CHECKPOINT-CONTENT',
  thinking: 'CHECKPOINT-THINKING',
  toolRounds: [],
  phase: PHASE,
});
updateStreamingUI(_streamFrameArg('c1'));
const live = snapshot();
check('live_painted', live.content.indexOf('CHECKPOINT-CONTENT') >= 0);

/* ── RECONNECT arm: cold-open — connectToTask re-seeds the buffer FROM the
 *    message checkpoint. The seed carries NO phase (phase is buffer runtime
 *    state; the message document never held it). ── */
freshBody();
streamBufs.set('c1', { content: '', thinking: '', toolRounds: [], phase: null });
updateStreamingUI(_streamFrameArg('c1'));
const recon = snapshot();
check('reconnect_painted_from_checkpoint', recon.content.indexOf('CHECKPOINT-CONTENT') >= 0);

/* ── THE ANCHOR: same logical instant ⇒ byte-identical zones. ── */
console.error('LIVE-content : ' + live.content.slice(0, 120));
console.error('RECON-content: ' + recon.content.slice(0, 120));
console.error('LIVE-status  : ' + live.status.slice(0, 200));
console.error('RECON-status : ' + recon.status.slice(0, 200));
check('ANCHOR_content_zone_byte_identical', live.content === recon.content);
check('ANCHOR_thinking_zone_byte_identical', live.thinking === recon.thinking);
check('ANCHOR_status_zone_byte_identical', live.status === recon.status);

/* ── NEUTER: a checkpoint-content difference MUST flip the content check —
 *    the comparator is load-bearing on both zones. ── */
conv.messages[1].content = 'CHECKPOINT-CONTENT-MUTATED';
freshBody();
streamBufs.set('c1', { content: 'MUTATED', thinking: '', toolRounds: [], phase: null });
updateStreamingUI(_streamFrameArg('c1'));
const mutated = snapshot();
check('NEUTER_content_difference_detected', mutated.content !== live.content);

report();
"""


def test_reconnect_vs_live_byte_parity():
    """§7.4 anchor — RED today on the STATUS ZONE only (the phase-home gap).

    The content/thinking checks are expected GREEN already (the checkpoint
    fallback covers them) — they pin that the gap is EXACTLY the status
    zone, not a vague "something differs somewhere". The whole test is RED
    until §7 moves phase into reducer live session state and the reconnect
    arm reads it from there; then all three zone checks flip GREEN together.
    """
    output = run_harness(
        target_js=HEALTH_TIMER,
        body_js=_BODY,
        extra_targets=[STREAMING_UI],
        min_pass=6,
        label='reconnect-parity-anchor',
    )
    # These two document the CURRENT divergence boundary precisely:
    assert 'PASS live_painted' in output, output
    assert 'PASS reconnect_painted_from_checkpoint' in output, output
    assert 'PASS NEUTER_content_difference_detected' in output, output
    # The failing-first anchor — all three zones must be byte-identical.
    # RED today on the status zone (phase is buffer-only state).
    failures = [ln for ln in output.splitlines() if ln.startswith('FAIL ')]
    assert not failures, (
        '§7.4 RED ANCHOR: live vs cold-open reconnect diverge — '
        + '; '.join(failures)
        + '\nThis is EXPECTED until the §7 streamBufs retirement lands phase '
        'in the reducer\'s live session state (plan §7.4). Do NOT silence '
        'this by weakening the comparison — fix the phase home.\n' + output)


if __name__ == '__main__':
    try:
        test_reconnect_vs_live_byte_parity()
        print('  PASS test_reconnect_vs_live_byte_parity')
    except Exception as e:  # noqa: BLE001
        print('  RED  test_reconnect_vs_live_byte_parity ::', str(e)[:400])
