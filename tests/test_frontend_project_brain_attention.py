"""jsdom regression for the Project Brain "Needs you" tab + the collab bar's
attention segment (project-brain-attention.js / presence.js).

The redesign's thesis in one line: **severity, not source, decides urgency.**

Before it, the always-visible collaboration bar led with `pendingDecisions`
(charter proposals) rendered with emphasis — but agents have self-committed
charter decisions since the 2026-07-12 de-gating, so a pending proposal blocks
nothing. Meanwhile an epic halted on a structured question — which
`project_dispatch` skips on EVERY heartbeat, so it never resolves on its own —
was not on the bar at all. The loudest signal was the least urgent one.

This suite pins the corrected behaviour end to end:

  • the Needs-you tab renders one card per attention item, in the SERVER's
    order (it must not re-sort — the SSOT's whole point);
  • a halted epic renders its question + answer controls INLINE, so the
    operator resolves it without hopping to the Board tab;
  • the empty state is a positive statement + the "waiting on their own gates"
    reassurance count, NOT a list of cooldown items;
  • the collab bar shows the aggregate `needsYou` count but reserves its
    ALARM (.collab-has-blocking) for `blocking > 0`;
  • an older backend (no needsYou field) still renders the legacy segment.

NCs: neuter the server-order preservation → a blocking-last payload renders
blocking-last; neuter the blocking-drives-emphasis rule → an advisory-only
project renders alarmed.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_ATTN_SRC = os.path.join(JS_DIR, 'project-brain-attention.js')
_PRESENCE_SRC = os.path.join(JS_DIR, 'presence.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract_panel_fragment():
    """Pull the REAL Project Brain overlay out of shipped index.html, so the
    test renders into the actual DOM the app ships (not a hand-built stub that
    can silently drift from it)."""
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    start = html.find('<div class="project-brain-overlay"')
    assert start != -1, 'project-brain-overlay not found in index.html'
    end = html.find('<div class="chat-container"', start)
    assert end > start, 'could not bound the project-brain overlay'
    return html[start:end]


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.t = global.t = (k) => k;
win.Icon = global.Icon = () => '<svg></svg>';
win.activeConvId = global.activeConvId = 'c-self';
// The attention module resolves the path via ProjectBrain._state — provide the
// minimal shim (the real project-brain.js is not loaded in this unit test).
let _selectedTab = '';
win.ProjectBrain = global.ProjectBrain = {
  _state: { path: '/proj/real' },
  _selectTab: (n) => { _selectedTab = n; },
  _wireClampToggles: () => {},
};
// Record every API call so the tests can assert WHICH backend route a control
// hits (the "one contract per action" invariant — no reimplementation).
const _calls = [];
win.Api = global.Api = { project: {
  boardAnswer: (p, id, conv, ans) => { _calls.push(['boardAnswer', id, ans]); return Promise.resolve({}); },
  commitCharter: (p, body) => { _calls.push(['commitCharter', body.resolves_proposal, body.add_decision]); return Promise.resolve({}); },
  dismissProposal: (p, id) => { _calls.push(['dismissProposal', id]); return Promise.resolve({}); },
  brainAttention: () => Promise.resolve({}),
}};

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain-attention.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const PBA = win.ProjectBrainAttention;
check('module_exposed', !!(PBA && typeof PBA.renderAttention === 'function'));
const body = win.document.getElementById('projectBrainAttentionBody');
check('real_panel_element_exists', !!body);

// ── Scenario: one blocking epic + one advisory proposal + one conflict,
//    in the SERVER's priority order (blocking first), plus 2 on cooldown. ──
PBA.renderAttention({
  blocking: 1, advisory: 2, needsYou: 3, waiting: 2,
  items: [
    { type: 'board_question', severity: 'blocking', id: 'pt_halted',
      title: 'Migrate the schema', question: 'Postgres or SQLite?',
      options: [{ label: 'Postgres' }, { label: 'SQLite' }],
      reason: '[human-gated] needs a call', blockCount: 3, tab: 'board' },
    { type: 'charter_proposal', severity: 'advisory', id: 'prop_1',
      text: 'Adopt the new parser', tab: 'charter' },
    { type: 'conflict', severity: 'advisory', id: 'src/shared.py',
      path: 'src/shared.py',
      text: 'conv A and conv B are concurrently editing src/shared.py',
      tab: 'peers' },
  ],
});

const html = body.innerHTML;
const cards = body.querySelectorAll('.pb-attn-card');
check('three_cards', cards.length === 3);
// SERVER ORDER PRESERVED — the blocking card is first. The module must not
// re-sort; the backend already ordered it (that is the SSOT contract).
check('blocking_card_first', cards[0] && cards[0].classList.contains('pb-attn-blocking'));
check('blocking_is_the_epic', cards[0] && cards[0].getAttribute('data-attn-type') === 'board_question');
check('advisory_cards_follow',
  cards[1] && cards[1].classList.contains('pb-attn-advisory') &&
  cards[2] && cards[2].classList.contains('pb-attn-advisory'));
// The halted epic renders its question + INLINE resolving controls.
check('question_rendered', html.indexOf('Postgres or SQLite?') !== -1);
check('option_chips', body.querySelectorAll('.pb-attn-act[data-act="answerOpt"]').length === 2);
check('answer_input', !!body.querySelector('.pb-attn-answer'));
check('answer_submit', !!body.querySelector('.pb-attn-act[data-act="answerSubmit"]'));
// The proposal renders commit + reject inline.
check('commit_btn', !!body.querySelector('.pb-attn-act[data-act="commit"]'));
check('reject_btn', !!body.querySelector('.pb-attn-act[data-act="reject"]'));
// The conflict is NOT resolvable by a button — it deep-links into Team.
check('conflict_deeplink', !!body.querySelector('.pb-attn-goto[data-goto-tab="peers"]'));
check('conflict_text_verbatim', html.indexOf('src/shared.py') !== -1);
// Cooldown epics are a muted reassurance FOOTNOTE, never cards (they need
// nothing from the human — listing them would devalue the surface).
check('waiting_footnote', !!body.querySelector('.pb-attn-waiting'));
check('waiting_not_a_card', cards.length === 3);
// Tab badge reflects the count AND the blocking alarm.
const badge = win.document.getElementById('pbTabCountAttention');
check('badge_count', badge && badge.textContent === '3' && badge.hidden === false);
check('badge_blocking_class', badge && badge.classList.contains('pb-tab-count-blocking'));

// ── Clicking an option chip must call boardAnswer with the option label ──
const chip = body.querySelector('.pb-attn-act[data-act="answerOpt"][data-idx="1"]');
if (chip) chip.click();
// ── Reject must call dismissProposal with the proposal id ──
const reject = body.querySelector('.pb-attn-act[data-act="reject"]');
if (reject) reject.click();
// ── The deep-link must switch the panel tab ──
const goto = body.querySelector('.pb-attn-goto[data-goto-tab="peers"]');
if (goto) goto.click();

setTimeout(() => {
  const answered = _calls.find(c => c[0] === 'boardAnswer');
  check('answer_hits_boardAnswer', !!answered && answered[1] === 'pt_halted');
  check('answer_sends_option_label', !!answered && answered[2] === 'SQLite');
  const dismissed = _calls.find(c => c[0] === 'dismissProposal');
  check('reject_hits_dismissProposal', !!dismissed && dismissed[1] === 'prop_1');
  check('deeplink_switches_tab', _selectedTab === 'peers');

  // ── Empty state: a POSITIVE statement + the reassurance count ──
  PBA.renderAttention({ blocking: 0, advisory: 0, needsYou: 0, waiting: 3, items: [] });
  const empty = body.innerHTML;
  check('empty_state', !!body.querySelector('.pb-attn-empty'));
  check('empty_no_cards', body.querySelectorAll('.pb-attn-card').length === 0);
  check('empty_mentions_waiting', empty.indexOf('attnEmptyWaiting') !== -1);
  const b2 = win.document.getElementById('pbTabCountAttention');
  check('empty_badge_hidden', b2 && b2.hidden === true);
  check('empty_badge_not_blocking', b2 && !b2.classList.contains('pb-tab-count-blocking'));

  // ── Advisory-only: the badge must NOT wear the blocking alarm ──
  PBA.renderAttention({ blocking: 0, advisory: 1, needsYou: 1, waiting: 0,
    items: [{ type: 'charter_proposal', severity: 'advisory', id: 'p2',
              text: 'Adopt Y', tab: 'charter' }] });
  const b3 = win.document.getElementById('pbTabCountAttention');
  check('advisory_badge_calm', b3 && b3.hidden === false &&
    !b3.classList.contains('pb-tab-count-blocking'));

  console.log(out.join('\n'));
  process.exit(0);
}, 30);
"""


def _write_and_run(harness_src, js_src, tag):
    frag_file = os.path.join(HERE, '_attn_frag_%s.html' % tag)
    harness = os.path.join(HERE, '_attn_harness_%s.js' % tag)
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_panel_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(['node', harness, js_src, ROOT, frag_file],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_attention_tab_renders_and_resolves_inline():
    output = _write_and_run(_HARNESS, _ATTN_SRC, 'main')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'attention-tab failures:\n' + output
    for must in ('PASS three_cards', 'PASS blocking_card_first',
                 'PASS blocking_is_the_epic', 'PASS question_rendered',
                 'PASS option_chips', 'PASS answer_submit',
                 'PASS commit_btn', 'PASS reject_btn',
                 'PASS conflict_deeplink', 'PASS waiting_footnote',
                 'PASS waiting_not_a_card', 'PASS badge_blocking_class',
                 'PASS answer_hits_boardAnswer', 'PASS answer_sends_option_label',
                 'PASS reject_hits_dismissProposal', 'PASS deeplink_switches_tab',
                 'PASS empty_state', 'PASS empty_mentions_waiting',
                 'PASS advisory_badge_calm'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_server_order_is_preserved():
    """NC: make the renderer re-sort items by type instead of trusting the
    server's order → a blocking-first payload no longer renders blocking-first
    → blocking_card_first FAILS.

    This is the SSOT contract: the panel and the collab bar must express ONE
    judgment. A frontend that re-derives priority is exactly how the bar's
    count and the panel's list drift apart.

    The neuter sorts ADVISORY-FIRST explicitly. An earlier draft used an
    alphabetical sort by `type` and the test still passed — 'board_question'
    happens to sort before 'charter_proposal', so the blocking card stayed
    first by luck. A neuter has to actually invert the property under test.
    """
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    for (var i = 0; i < items.length; i++) {\n      var it = items[i] || {};"
    assert anchor in original, 'render-loop anchor not found (source changed?)'
    patched = original.replace(
        anchor,
        ("    items = items.slice().sort(function (a, b) {  // NC (client re-sort)\n"
         "      var ra = a.severity === 'blocking' ? 1 : 0;\n"
         "      var rb = b.severity === 'blocking' ? 1 : 0;\n"
         "      return ra - rb; });\n"
         "    for (var i = 0; i < items.length; i++) {\n      var it = items[i] || {};"),
        1)
    copy_path = os.path.join(HERE, '_attn_nc_order.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_HARNESS, copy_path, 'ncorder')
        assert 'FAIL blocking_card_first' in output, \
            ('NC: a client-side re-sort must break the server-order '
             'contract:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_blocking_drives_badge_alarm():
    """NC: drive the badge alarm off the raw COUNT instead of `blocking` → an
    advisory-only project renders alarmed → advisory_badge_calm FAILS.

    This is the inversion fix stated as a contract: a pending charter proposal
    must not make the UI shout, because it stops nothing.
    """
    with open(_ATTN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      el.classList.toggle('pb-tab-count-blocking', blocking > 0);"
    assert anchor in original, 'badge-alarm anchor not found (source changed?)'
    patched = original.replace(
        anchor,
        "      el.classList.toggle('pb-tab-count-blocking', n > 0);  // NC (count-driven)",
        1)
    copy_path = os.path.join(HERE, '_attn_nc_alarm.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _write_and_run(_HARNESS, copy_path, 'ncalarm')
        assert 'FAIL advisory_badge_calm' in output, \
            ('NC: a count-driven alarm must make an advisory-only project '
             'render alarmed:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_ATTN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-attention.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  The collaboration bar's attention segment
# ════════════════════════════════════════════════════════════════════


def _extract_strip_fragment():
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    needle = 'id="presenceStrip"'
    i = html.find(needle)
    assert i != -1, 'presenceStrip not found in index.html'
    start = html.rfind('<div', 0, i)
    end = html.find('</div>', i) + len('</div>')
    return html[start:end]


_BAR_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.t = global.t = (k, params) => {
  if (params && typeof params === 'object') {
    let s = k;
    for (const key of Object.keys(params)) s += ' ' + params[key];
    return s;
  }
  return k;
};
win.activeConvId = global.activeConvId = 'c-self';
win.getActiveConv = global.getActiveConv = () => ({ id: 'c-self', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
let _openArg = null;
win.openProjectBrain = global.openProjectBrain = (o) => { _openArg = o || null; };
win.Api = global.Api = { project: {} };

eval(fs.readFileSync(SRC, 'utf8'));  // presence.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const CB = win.CollabBar;
const stripEl = win.document.getElementById('presenceStrip');

// ── BLOCKING: one halted epic + one proposal → bar shows 2, reads urgent ──
CB._setSummary('/proj/real', {
  epicsOpen: 0, epicsClaimed: 1, epicsDone: 0,
  pendingDecisions: 1, needsYou: 2, blocking: 1, advisory: 1, waiting: 0,
  activePeers: 0, charterExists: true, peerEpics: {},
});
CB._setPeers('/proj/real', []);
CB._render();
let html = stripEl.innerHTML;
check('needsyou_segment', html.indexOf('collab-seg-needsyou') !== -1);
check('needsyou_count_is_aggregate', html.indexOf('needsYouBlocking 2') !== -1);
check('blocking_seg_class', html.indexOf('collab-seg-blocking') !== -1);
check('bar_reads_blocking', html.indexOf('collab-has-blocking') !== -1);
// The legacy class is kept as an ALIAS so existing styling/selectors survive.
check('legacy_decisions_alias', html.indexOf('collab-seg-decisions') !== -1);
// Clicking hands the count to the panel so it lands on the Needs-you tab.
const inner = stripEl.querySelector('.collab-bar-inner');
if (inner) inner.click();
check('click_passes_needsyou', !!_openArg && _openArg.needsYou === 2);

// ── ADVISORY-ONLY: a proposal blocks nothing → the bar must stay CALM ──
CB._setSummary('/proj/real', {
  epicsOpen: 0, epicsClaimed: 1, epicsDone: 0,
  pendingDecisions: 1, needsYou: 1, blocking: 0, advisory: 1, waiting: 0,
  activePeers: 0, charterExists: true, peerEpics: {},
});
CB._render();
html = stripEl.innerHTML;
check('advisory_segment_present', html.indexOf('collab-seg-needsyou') !== -1);
check('advisory_wording', html.indexOf('needsYou 1') !== -1);
check('advisory_bar_calm', html.indexOf('collab-has-blocking') === -1);
check('advisory_seg_not_blocking', html.indexOf('collab-seg-blocking') === -1);

// ── LEGACY BACKEND: no needsYou field → the old segment still renders ──
CB._setSummary('/proj/real', {
  epicsOpen: 0, epicsClaimed: 1, epicsDone: 0, pendingDecisions: 2,
  activePeers: 0, charterExists: true, peerEpics: {},
});
CB._render();
html = stripEl.innerHTML;
check('legacy_fallback_segment', html.indexOf('decisionsAwaiting 2') !== -1);

console.log(out.join('\n'));
process.exit(0);
"""


def _run_bar(js_src, tag):
    frag_file = os.path.join(HERE, '_attn_bar_frag_%s.html' % tag)
    harness = os.path.join(HERE, '_attn_bar_harness_%s.js' % tag)
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(_extract_strip_fragment())
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_BAR_HARNESS)
    try:
        proc = subprocess.run(['node', harness, js_src, ROOT, frag_file],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_collab_bar_attention_segment():
    output = _run_bar(_PRESENCE_SRC, 'main')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'collab-bar attention failures:\n' + output
    for must in ('PASS needsyou_segment', 'PASS needsyou_count_is_aggregate',
                 'PASS bar_reads_blocking', 'PASS legacy_decisions_alias',
                 'PASS click_passes_needsyou', 'PASS advisory_bar_calm',
                 'PASS advisory_seg_not_blocking', 'PASS legacy_fallback_segment'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_bar_alarm_keys_on_blocking_not_count():
    """NC: drive the bar's alarm off the raw needsYou COUNT instead of
    `blocking` → an advisory-only project renders alarmed → advisory_bar_calm
    FAILS.

    This is THE regression guard for the original inversion: the bar used to
    emphasise a pending-proposal count, which stops nothing.
    """
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    const hasBlocking = !!(summary && (summary.blocking || 0) > 0);"
    assert anchor in original, 'bar-alarm anchor not found (source changed?)'
    patched = original.replace(
        anchor, "    const hasBlocking = needsYou > 0;  // NC (count-driven)", 1)
    copy_path = os.path.join(HERE, '_attn_bar_nc.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_bar(copy_path, 'nc')
        assert 'FAIL advisory_bar_calm' in output, \
            ('NC: a count-driven bar alarm must make an advisory-only project '
             'render alarmed:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_PRESENCE_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped presence.js must be byte-identical'
