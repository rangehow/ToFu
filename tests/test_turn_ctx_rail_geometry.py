"""The per-turn context rail can never be clipped, in ANY pane state.

WHY THIS FILE EXISTS
────────────────────
The rail (`.turn-ctx`, built by ``static/js/info-rail.js``) used to be an
absolutely-positioned capsule anchored at ``left:calc(100% + 24px)`` off the
centred reading column, made visible by ``@media (max-width:1280px)``.

That predicate asks about the WINDOW. The space the rail actually lands in is::

    viewport − sidebar − request-inspector-drawer

The sidebar has FOUR widths (0 collapsed / 280 plain / 332 rail-collapsed /
430 has-rail) and ``body.ri-open .chat-container{margin-right:min(780px,94vw)}``
removes up to another 780px. Measured in a real browser across all 64
combinations (8 widths × 4 sidebar states × drawer open/closed) BEFORE the fix:

    shown-but-clipped : 35 / 64   (bar 33, hover panel 35)
    hidden-but-room   :  0 / 64

Zero states were hidden while having room, so **no viewport threshold can be
correct** — the predicate was blind to two of the three variables. The fix
gives the rail a grid track the pane OWNS and drives visibility with a
``@container`` query on ``.chat-container`` (the element whose used width
actually equals the available space; a query on ``.chat-wrapper`` would be
blind in the 32 drawer-open cells, where wrapper and container widths differ
by exactly 780px).

WHAT THIS ASSERTS (charter: assert the RESULT, not the threshold)
────────────────────────────────────────────────────────────────
1. **The invariant** — in every one of the 64 states, no rail element's right
   edge exceeds the pane's right edge, INCLUDING after the "+N" overflow is
   expanded. (A collapsed-only assertion would have passed while the old hover
   panel was still broken in 35 states.)
2. **The complement** — in roomy states the rail is actually PRESENT and
   NON-EMPTY. Without this, "hide it everywhere" would satisfy (1) and stay
   green forever.
3. **The height bound** — a one-line user turn must not be inflated by the
   rail beyond a fixed multiple of its own content. Not clipping but adding
   100px of whitespace per turn would trade "overflow" for "ugly".
4. **No context is lost** — where the rail has no track (narrow pane / drawer
   open), the compact `.tctx-fold` summary is shown instead.

None of these name a CSS constant or a source literal, so re-tuning the
breakpoint, the rail width or the clamp keeps them green while a real
regression turns them red.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

_WIDTHS = (1280, 1366, 1440, 1512, 1600, 1728, 1920, 2560)

#: (label, JS that puts the sidebar into that state). Mirrors the four real
#: widths the sidebar can have — see `.sidebar` / `.has-rail` in styles.css.
_SIDEBAR_STATES = (
    ('collapsed', "s.classList.remove('has-rail','rail-collapsed'); s.classList.add('collapsed')"),
    ('plain', "s.classList.remove('collapsed','has-rail','rail-collapsed')"),
    ('rail-collapsed', "s.classList.remove('collapsed'); s.classList.add('has-rail','rail-collapsed')"),
    ('has-rail', "s.classList.remove('collapsed','rail-collapsed'); s.classList.add('has-rail')"),
)

#: A snapshot with MANY tools — the shape that used to overflow worst, and the
#: one that exercises the "+N" overflow toggle.
_FAT_SNAPSHOT = """{
  model: 'claude-opus-5',
  depth: 'think',
  modes: [{label:'Autopilot', tone:'mode'}, {label:'Swarm', tone:'mode'}],
  tools: [
    {label:'Search ×N', tone:'search'}, {label:'Fetch', tone:'net'},
    {label:'Memory', tone:'ai'}, {label:'MCP: 12306-train', tone:'mcp'},
    {label:'MCP: github ×26', tone:'mcp'}, {label:'MCP: github-batch ×2', tone:'mcp'},
    {label:'MCP: hope ×50', tone:'mcp'}, {label:'MCP: llm ×30', tone:'mcp'},
    {label:'MCP: overleaf ×21', tone:'mcp'}, {label:'MCP: xuecheng ×32', tone:'mcp'}
  ],
  roots: [{short:'INS/chatui', path:'/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/chatui', ro:false}]
}"""

#: Plant one SHORT user turn carrying that snapshot, through the production
#: renderer (`renderTurnCtxNote`) — never a hand-written copy of its markup.
_PLANT = """() => {
    const inner = document.querySelector('.chat-inner');
    if (!inner) return 'no .chat-inner';
    if (typeof renderTurnCtxNote !== 'function') return 'no renderTurnCtxNote';
    document.querySelectorAll('.message.__probe').forEach(n => n.remove());
    const html = renderTurnCtxNote(%s);
    if (!html) return 'renderer returned empty';
    const d = document.createElement('div');
    d.className = 'message user-msg __probe';
    d.innerHTML = '<div class="message-avatar"></div>'
                + '<div class="message-content"><div class="message-header">'
                + '<span class="message-role">You</span></div>'
                + '<div class="message-body"><div class="md-content user-content">hi</div></div>'
                + '</div>' + html;
    inner.appendChild(d);
    return 'ok';
}""" % _FAT_SNAPSHOT

#: Measure the pane and every rail element inside the probe turn.
_PROBE = """() => {
    const msg  = document.querySelector('.message.__probe');
    const cont = document.querySelector('.chat-container');
    if (!msg || !cont) return null;
    const contR = cont.getBoundingClientRect();
    const rail  = msg.querySelector('.turn-ctx');
    const fold  = msg.querySelector('.tctx-fold');
    const shown = (el) => !!el && getComputedStyle(el).display !== 'none';
    const railShown = shown(rail);
    // Widest right edge among the rail and EVERY descendant it paints.
    let worst = null, worstSel = '';
    if (railShown) {
        const all = [rail, ...rail.querySelectorAll('*')];
        for (const el of all) {
            if (getComputedStyle(el).display === 'none') continue;
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            if (worst === null || r.right > worst) {
                worst = r.right;
                worstSel = el.className || el.tagName;
            }
        }
    }
    const body = msg.querySelector('.message-content');
    return {
        vw: window.innerWidth,
        paneRight: contR.right,
        paneWidth: contR.width,
        railShown: railShown,
        foldShown: shown(fold),
        foldText: fold ? (fold.textContent || '').trim() : '',
        railRight: worst,
        worstSel: String(worstSel),
        railChips: rail ? rail.querySelectorAll('.tctx-chip').length : 0,
        railText: rail ? (rail.textContent || '').trim().length : 0,
        msgHeight: msg.getBoundingClientRect().height,
        bodyHeight: body ? body.getBoundingClientRect().height : 0,
    };
}"""

#: Click every "+N" toggle so the EXPANDED state is measured too — the old
#: hover panel clipped in MORE states (35) than the collapsed bar (33), so a
#: collapsed-only sweep would have passed while the real breakage remained.
_EXPAND = """() => {
    const btns = document.querySelectorAll('.message.__probe [data-tctx-more]');
    btns.forEach(b => b.click());
    return btns.length;
}"""

#: Force every overflow group back to hidden. WITHOUT THIS the sweep is wrong:
#: the toggle persists across viewport changes, so every state measured after
#: the first expansion reports the EXPANDED geometry while claiming to be
#: collapsed — the observation-window defect, in state rather than in time.
_COLLAPSE = """() => {
    document.querySelectorAll('.message.__probe .tctx-overflow').forEach(o => {
        o.setAttribute('hidden', '');
    });
    document.querySelectorAll('.message.__probe [data-tctx-more]').forEach(b => {
        const o = b.parentNode.querySelector('.tctx-overflow');
        b.setAttribute('aria-expanded', 'false');
        if (o) b.textContent = '+' + o.children.length;
    });
}"""

#: A rail must not inflate a one-line turn without bound. 2.5× the bubble's
#: own content height is generous (the bubble is ~60px, the rail ~150px with
#: everything shown) yet still catches "the rail is now 400px tall".
_MAX_HEIGHT_RATIO = 2.5


def _sweep(page):
    """Return one measurement row per (drawer × sidebar × width) state."""
    rows = []
    for drawer in (False, True):
        page.evaluate("() => { %s }" % (
            "openRequestInspector()" if drawer else "closeRequestInspector()"))
        page.wait_for_timeout(250)
        for sb_label, setup in _SIDEBAR_STATES:
            page.evaluate("() => { const s = document.querySelector('.sidebar');"
                          " if (s) { %s; } }" % setup)
            for w in _WIDTHS:
                page.set_viewport_size({'width': w, 'height': 900})
                page.wait_for_timeout(120)
                for expanded in (False, True):
                    if expanded:
                        page.evaluate(_EXPAND)
                    else:
                        page.evaluate(_COLLAPSE)
                    page.wait_for_timeout(80)
                    m = page.evaluate(_PROBE)
                    assert m is not None, 'probe turn vanished from the DOM'
                    m['drawer'] = drawer
                    m['sidebar'] = sb_label
                    m['expanded'] = expanded
                    rows.append(m)
    return rows


def test_rail_never_clipped_in_any_pane_state(page):
    """THE INVARIANT + its complement + the height bound, over 64 states."""
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function("typeof renderTurnCtxNote === 'function'", timeout=20000)
    planted = page.evaluate(_PLANT)
    assert planted == 'ok', f'could not plant the probe turn: {planted}'

    rows = _sweep(page)
    states = {(r['drawer'], r['sidebar'], r['vw']) for r in rows}
    assert len(states) == 64, f'expected 64 pane states, swept {len(states)}'

    # ── 1. No rail element may cross the pane's right edge, ever. ──
    clipped = [r for r in rows
               if r['railShown'] and r['railRight'] is not None
               and r['railRight'] > r['paneRight'] + 0.5]

    # ── 2. COMPLEMENT: where the pane is roomy the rail must really be there
    #      and carry content. Without this, hiding it everywhere passes (1). ──
    roomy = [r for r in rows if r['paneWidth'] >= 1368]
    missing = [r for r in roomy if not r['railShown'] or r['railText'] < 10]

    # ── 3. Height bound: a one-line turn must not balloon. Scoped to the
    #      COLLAPSED rail: expanding is an explicit user request to see more,
    #      so growth there is the feature, not the defect. ──
    too_tall = [r for r in rows
                if not r['expanded']
                and r['bodyHeight'] > 0
                and r['msgHeight'] > r['bodyHeight'] * _MAX_HEIGHT_RATIO]

    # ── 4. No context lost: no rail track → the fold summary stands in. ──
    lost = [r for r in rows
            if not r['railShown'] and (not r['foldShown'] or len(r['foldText']) < 5)]

    print(f'\n  states swept              : {len(states)} (×2 collapsed/expanded = {len(rows)} rows)')
    print(f'  rail CLIPPED              : {len(clipped)}')
    print(f'  roomy states w/o rail     : {len(missing)} (of {len(roomy)} roomy rows)')
    print(f'  turns inflated > {_MAX_HEIGHT_RATIO}×      : {len(too_tall)}')
    print(f'  no rail AND no fold       : {len(lost)}')
    if rows:
        _w = max(r['paneWidth'] for r in rows)
        _n = min(r['paneWidth'] for r in rows)
        print(f'  pane width range          : {_n:.0f} … {_w:.0f}px')

    assert not clipped, (
        'the rail crosses the pane\'s right edge in %d state(s) — this is the '
        'very overflow the container query is supposed to make impossible:\n  '
        % len(clipped)
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d expanded=%-5s railRight=%.0f > paneRight=%.0f  (%s)'
            % (r['drawer'], r['sidebar'], r['vw'], r['expanded'],
               r['railRight'], r['paneRight'], r['worstSel'][:40])
            for r in clipped[:12]))

    assert not missing, (
        'the rail is absent or empty in %d roomy state(s) — "hide it '
        'everywhere" must NOT be a way to satisfy the no-clip invariant:\n  '
        % len(missing)
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d paneWidth=%.0f railShown=%s textLen=%d'
            % (r['drawer'], r['sidebar'], r['vw'], r['paneWidth'],
               r['railShown'], r['railText'])
            for r in missing[:12]))

    assert not too_tall, (
        'the rail inflates a one-line turn past %.1f× its own content height '
        'in %d state(s) — not clipping is not enough, it must also look '
        'right:\n  ' % (_MAX_HEIGHT_RATIO, len(too_tall))
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d expanded=%-5s msg=%.0f body=%.0f'
            % (r['drawer'], r['sidebar'], r['vw'], r['expanded'],
               r['msgHeight'], r['bodyHeight'])
            for r in too_tall[:12]))

    assert not lost, (
        'in %d state(s) the rail is hidden AND no fold summary replaces it — '
        'the turn\'s context would be silently lost:\n  ' % len(lost)
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d paneWidth=%.0f fold=%r'
            % (r['drawer'], r['sidebar'], r['vw'], r['paneWidth'], r['foldText'][:40])
            for r in lost[:12]))


def test_overflow_toggle_bounds_the_chip_count(page):
    """The "+N" toggle really gates chips, and expanding really reveals them.

    Guards the height bound's mechanism: if every chip rendered up-front the
    rail height would be driven by the number of connected MCP servers.
    """
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function("typeof renderTurnCtxNote === 'function'", timeout=20000)
    page.set_viewport_size({'width': 1920, 'height': 900})
    page.evaluate("() => { const s = document.querySelector('.sidebar');"
                  " if (s) s.classList.add('collapsed'); }")
    assert page.evaluate(_PLANT) == 'ok'
    page.wait_for_timeout(200)

    before = page.evaluate(
        "() => document.querySelectorAll('.message.__probe .tctx-chip:not(.tctx-overflow .tctx-chip)').length")
    total = page.evaluate(
        "() => document.querySelectorAll('.message.__probe .tctx-chip').length")
    btns = page.evaluate(_EXPAND)
    page.wait_for_timeout(150)
    visible_after = page.evaluate("""() => {
        const els = document.querySelectorAll('.message.__probe .tctx-chip');
        let n = 0;
        els.forEach(e => { if (e.offsetParent !== null) n++; });
        return n;
    }""")

    print(f'\n  chips total={total} visible-before={before} '
          f'toggles={btns} visible-after={visible_after}')

    assert btns >= 1, (
        'a 10-tool snapshot produced no "+N" toggle — the chip count is '
        'unbounded, so the rail height is driven by how many MCP servers '
        'happen to be connected')
    assert before < total, (
        f'all {total} chips rendered up-front ({before} visible) — the bound '
        f'is not in effect')
    assert visible_after > before, (
        f'clicking "+N" revealed nothing ({before} → {visible_after}); the '
        f'hidden chips are unreachable, so the information is lost rather '
        f'than collapsed')
