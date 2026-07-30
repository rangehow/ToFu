"""Frontend guards for the goal ↔ north-star convergence (Project Brain).

WHY THIS IS A NODE-DRIVEN TEST AND NOT A PYTHON SOURCE SCAN
-----------------------------------------------------------
The backend computes the three-state promotion verdict; the FRONTEND decides
what the human sees and — critically — whether the "set as goal" button is
still offered. The measured bug this suite exists to prevent lives entirely on
the JS side: ``_buildWatchActions`` used to gate the promote button on
``!item.promoted``, so a DIVERGED goal (promoted once, then one side edited)
got the button back, and one click silently overwrote whichever side the human
had just edited. No Python assertion can see that — it is a property of the
shipped renderer's branch logic.

So each test below loads the REAL ``static/js/project-brain-status.js`` into a
node harness with a minimal DOM and drives the exported ``buildWatchItem`` /
``renderWatch``. A hand-written equivalent would be testing the harness.
"""

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, 'static', 'js', 'project-brain-status.js')

_HARNESS = r"""
// ── Minimal DOM good enough for the builders under test ──────────────
function mkEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: '', textContent: '', hidden: false, disabled: false,
    type: '', rows: 0, value: '', placeholder: '', title: '',
    children: [], _attrs: {}, _listeners: {}, parentNode: null,
    appendChild(c) { c.parentNode = el; el.children.push(c); return c; },
    removeChild(c) { el.children = el.children.filter(x => x !== c); return c; },
    setAttribute(k, v) { el._attrs[k] = String(v); },
    getAttribute(k) { return el._attrs[k] === undefined ? null : el._attrs[k]; },
    addEventListener(t, fn) { (el._listeners[t] = el._listeners[t] || []).push(fn); },
    click() { (el._listeners['click'] || []).forEach(fn => fn({})); },
    closest(sel) {
      const want = sel.replace(/^\./, '');
      let n = el;
      while (n) { if ((n.className || '').split(/\s+/).includes(want)) return n; n = n.parentNode; }
      return null;
    },
    querySelector(sel) {
      const want = sel.replace(/^\./, '');
      const walk = (n) => {
        for (const c of n.children) {
          if ((c.className || '').split(/\s+/).includes(want)) return c;
          const hit = walk(c); if (hit) return hit;
        }
        return null;
      };
      return walk(el);
    },
    get innerHTML() { return ''; },
    set innerHTML(_v) { el.children = []; },
  };
  return el;
}
const _byId = {};
global.document = {
  createElement: mkEl,
  createDocumentFragment: () => mkEl('fragment'),
  getElementById: (id) => _byId[id] || null,
};
global.window = {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;                       // labels come back as their key
global.Icon = () => '';
global.Api = { project: {
  brainWatchAddress: () => Promise.resolve({}),
  brainWatchUpdate: () => Promise.resolve({}),
  brainWatchPromote: (...a) => { global.__promoteCalls.push(a); return Promise.resolve({}); },
  brainWatchList: () => Promise.resolve({ items: [] }),
} };
global.__promoteCalls = [];

require(process.env.PB_STATUS_JS);
const PBS = global.window.ProjectBrainStatus;

// Collect every text node in a subtree (class list + textContent).
function flatten(node, out) {
  out = out || [];
  out.push({ cls: node.className || '', text: node.textContent || '' });
  (node.children || []).forEach(c => flatten(c, out));
  return out;
}
function hasClass(node, cls) { return flatten(node).some(n => (n.cls || '').split(/\s+/).includes(cls)); }
function textsOf(node) { return flatten(node).map(n => n.text).filter(Boolean); }

global.__probe = { PBS, flatten, hasClass, textsOf, mkEl };
"""


def _run(script: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available')
    src = _HARNESS + '\n' + script
    env = dict(os.environ, PB_STATUS_JS=_JS)
    proc = subprocess.run([node, '-e', src], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, (
        f'node harness failed\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}')
    return json.loads(proc.stdout.strip().splitlines()[-1])


_CTX = "{charterVersion: 7, charterContent: 'OLD GOAL TEXT'}"


def _item(**kw):
    base = {'item_id': 'w1', 'kind': 'goal', 'text': 'NEW GOAL TEXT',
            'status': 'open', 'promotionState': 'none', 'divergedSide': '',
            'promotedAudit': False, 'responses': []}
    base.update(kw)
    return json.dumps(base)


def test_active_goal_renders_the_live_badge_and_no_promote_button():
    """A goal that IS the live north star must say so, and must NOT offer the
    button again — re-promoting an already-live goal is a no-op write that
    bumps the charter version for nothing."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_item(promotionState='active')}, {_CTX});
      console.log(JSON.stringify({{
        live: __probe.hasClass(card, 'pb-watch-promoted'),
        diverged: __probe.hasClass(card, 'pb-watch-diverged'),
        promoteBtn: __probe.hasClass(card, 'pb-watch-btn-promote'),
        texts: __probe.textsOf(card),
      }}));
    """)
    assert out['live'] is True
    assert out['diverged'] is False
    assert out['promoteBtn'] is False
    assert 'projectBrain.watchIsNorthStar' in out['texts']


def test_diverged_goal_shows_diverged_not_unpromoted():
    """THE REGRESSION THIS FILE EXISTS FOR.

    Rendering a diverged goal as plain "not promoted" hands the human a button
    whose click silently overwrites whichever side they just edited. It must
    render the diverged badge and a REVIEW affordance instead."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem(
        {_item(promotionState='diverged', divergedSide='charter', promotedAudit=True)}, {_CTX});
      console.log(JSON.stringify({{
        live: __probe.hasClass(card, 'pb-watch-promoted'),
        diverged: __probe.hasClass(card, 'pb-watch-diverged'),
        texts: __probe.textsOf(card),
      }}));
    """)
    assert out['diverged'] is True, 'a diverged goal must be badged as diverged'
    assert out['live'] is False, 'a diverged goal must NOT claim to be live'
    assert 'projectBrain.watchReadopt' in out['texts'], (
        'a diverged goal must offer REVIEW & re-adopt, never a bare set-as-goal')


def test_goal_click_previews_replacement_and_never_promotes_directly():
    """Clicking a goal's button must OPEN THE PREVIEW, not call the API.

    The charter's content column is a single slot, so a goal promotion is a
    replacement — a direct call would destroy the existing north star with no
    confirmation."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_item()}, {_CTX});
      const btn = card.querySelector('.pb-watch-btn-promote');
      btn.click();
      const flat = __probe.flatten(card);
      console.log(JSON.stringify({{
        promoteCalls: global.__promoteCalls.length,
        previewOpened: __probe.hasClass(card, 'pb-watch-replace'),
        texts: flat.map(n => n.text).filter(Boolean),
      }}));
    """)
    assert out['promoteCalls'] == 0, (
        'clicking a goal must NOT call promote directly — it must preview first')
    assert out['previewOpened'] is True
    # Both sides of the replacement must be visible before the human commits.
    assert 'OLD GOAL TEXT' in out['texts'], 'the text being REPLACED must be shown'
    assert 'NEW GOAL TEXT' in out['texts'], 'the text replacing it must be shown'


def test_replacement_confirm_submits_the_rendered_charter_version():
    """expected_version is a HARD gate on the content path, so the preview must
    submit the version it rendered from — otherwise the human confirms against
    one text and overwrites another."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem({_item()}, {_CTX});
      card.querySelector('.pb-watch-btn-promote').click();
      card.querySelector('.pb-watch-btn-confirm').click();
      console.log(JSON.stringify({{ calls: global.__promoteCalls }}));
    """)
    assert len(out['calls']) == 1
    item_id, _conv, version = out['calls'][0]
    assert item_id == 'w1'
    assert version == 7, 'must submit the charterVersion the preview rendered from'


def test_concern_keeps_the_direct_decision_path():
    """A concern is NOT a goal: appends commute, so it needs no replacement
    preview and must still promote in one click."""
    out = _run(f"""
      const card = __probe.PBS.buildWatchItem(
        {_item(kind='concern', text='Artifacts may desync')}, {_CTX});
      card.querySelector('.pb-watch-btn-promote').click();
      console.log(JSON.stringify({{
        calls: global.__promoteCalls.length,
        previewOpened: __probe.hasClass(card, 'pb-watch-replace'),
      }}));
    """)
    assert out['calls'] == 1, 'a concern must promote directly (appends commute)'
    assert out['previewOpened'] is False


def test_renderer_never_reads_the_stale_promoted_boolean():
    """The stored flag records that a promotion once happened; it stays true
    after the charter is deleted. Rendering it is how the badge came to lie."""
    out = _run(f"""
      // promotedAudit=true but the live verdict says 'none' (charter deleted).
      const card = __probe.PBS.buildWatchItem(
        {_item(promotionState='none', promotedAudit=True)}, {_CTX});
      console.log(JSON.stringify({{
        live: __probe.hasClass(card, 'pb-watch-promoted'),
        diverged: __probe.hasClass(card, 'pb-watch-diverged'),
      }}));
    """)
    assert out['live'] is False, (
        'the badge must follow the computed verdict, not the stored boolean')
    assert out['diverged'] is False


def test_source_has_no_bare_promoted_branch():
    """Complement to the behavioural tests: the renderer must not regrow a
    branch on the stale boolean. `promotedAudit` (the renamed audit field) is
    allowed to APPEAR in the payload mapping; what is banned is branching on a
    bare `item.promoted`."""
    with open(_JS, encoding='utf-8') as f:
        src = f.read()
    assert 'item.promoted)' not in src, (
        'project-brain-status.js branches on the stale `item.promoted` boolean '
        '— render item.promotionState (the computed verdict) instead')
    assert 'item.promotionState' in src, 'the computed verdict must be rendered'
