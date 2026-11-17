"""Real-browser surface sweep — open each panel, assert no HARD JS errors.

WHY THIS FILE EXISTS
────────────────────
Measured 2026-07-28: ``static/js`` ships **157 modules** and the suite has
**377 jsdom/source-anchor frontend tests**, but only **40 real-browser tests**,
all concentrated on chat / new-conversation / search-mode. Nine top-level
panels had ZERO real-browser coverage: paper, skills, settings, orchestration,
artifacts, myday, scheduler, translation, optimizer.

jsdom structurally cannot see this bug class — it does not load the real
bundle, does not run real CSS, and does not make real requests. An uncaught
exception while a panel builds **aborts the rest of that script**, so later
handlers silently never bind. That is precisely the "I clicked and nothing
happened" class this project keeps rediscovering by hand.

WHAT EACH TEST ASSERTS (charter: assert the RESULT, not the implementation)
──────────────────────────────────────────────────────────────────────────
1. Opening the panel through its **production entry point** raises no HARD JS
   error (uncaught exception / unexpected console.error / failed request).
2. The panel's **container actually appears in the DOM** — otherwise "no error"
   would also pass for an entry point that silently does nothing.

Both are outcomes. Neither names a JS constant or a source literal, so a
reasonable refactor of any panel keeps these green while a real breakage
turns them red.

ENTRY POINTS ARE DISCOVERED, NOT ASSUMED
────────────────────────────────────────
The epic proposed ``switchSettingsTab('providers'/'memory')``. Measured
against ``index.html``: those tab ids **do not exist**. The real set is the 14
below, read off the production buttons. Same for the modals — ``openMyDay`` /
``openScheduler`` / ``openArtifacts`` do not exist either; the real entries are
``openDailyReport`` / ``openOrchestration`` / ``openMemoryModal`` /
``openMobileOptimizer``. Guessing would have produced tests that pass by
doing nothing.

COST
────
Deliberately seconds, not minutes: one page load, then each panel is opened in
turn on the SAME page. The whole settings sweep covers 14 tabs in one test.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]


# ── Production entry points (verified against index.html / static/js) ────
#
# Read off the actual `onclick="switchSettingsTab('X')"` buttons in index.html.
# `switchSettingsTab` marks the matching `#settingsTab_<id>` pane `.active`,
# which is the RESULT we assert.
_SETTINGS_TABS = (
    'general', 'api', 'preset', 'mcp', 'skills', 'knowledge', 'search', 'speech',
    'translate', 'oauth', 'network', 'devices', 'feishu', 'preferences',
    'advanced',
)

#: (label, runtime action that opens it, CSS selector proving it opened).
#: Every function here was confirmed to exist as a top-level definition; the
#: names the epic guessed at (openMyDay/openScheduler/openArtifacts) do not.
_MODAL_PANELS = (
    ('memory', 'openMemoryModal', '#memoryModal.open'),
    ('myday', 'openDailyReport', '#dailyReportModal.open'),
    # orchModal is built lazily by openOrchestration() rather than living in
    # index.html, so the selector doubles as proof the builder ran.
    ('orchestration', 'openOrchestration', '#orchModal'),
)


def _wait_ready(page, timeout=20000):
    page.wait_for_selector('#userInput', state='visible', timeout=timeout)
    page.wait_for_function(
        "window.TofuModules?.version === 3"
        " && typeof window.TofuModules.invokeFeature === 'function'",
        timeout=timeout,
    )


def _invoke_action(page, name, *args):
    """Drive the v3 command port used by production data-tofu-action seams."""
    return page.evaluate(
        """async ({name, args}) => {
          const modules = window.TofuModules;
          if (modules.canInvokeFeature(name)) {
            return await modules.invokeFeature(name, args, () => {});
          }
          const action = modules.resolveAction(name);
          if (typeof action !== 'function') {
            throw new Error(`No frontend action registered for ${name}`);
          }
          return await action(...args);
        }""",
        {'name': name, 'args': list(args)},
    )


def _hard_errors(page):
    return list(getattr(page, '_tofu_js_errors', []))


def _drain(page):
    """Clear captured errors so each panel is attributed independently."""
    getattr(page, '_tofu_js_errors', []).clear()


def test_settings_tabs_open_without_js_errors(page):
    """All 14 settings tabs: opening + switching raises no HARD JS error.

    One test for all tabs on one page load — the sweep must stay seconds, not
    minutes (the whole visual ring is 2m55s and must not regress).

    Check errors during both opening and tab activation. Shared configuration
    loads on opening; independent panels load on their first visit. Clearing
    the buffer before judging either phase would conceal a renderer failure.
    """
    _wait_ready(page)
    _drain(page)
    _invoke_action(page, 'openSettings')
    # Shared initial configuration has asynchronous projections of its own.
    page.wait_for_timeout(2500)
    open_errs = _hard_errors(page)
    assert not open_errs, (
        'openSettings() raised %d JavaScript error(s) while building the '
        'initial settings panes:\n  %s'
        % (len(open_errs), '\n  '.join(open_errs[:10])))

    failures = {}
    missing_pane = []
    for tab in _SETTINGS_TABS:
        _drain(page)
        _invoke_action(page, 'switchSettingsTab', tab)
        page.wait_for_timeout(350)
        errs = _hard_errors(page)
        if errs:
            failures[tab] = errs
        # RESULT check: the pane really became active. Without this, a no-op
        # entry point would also produce "no errors" and pass.
        active = page.evaluate(
            f"() => !!document.querySelector('#settingsTab_{tab}.active')")
        if not active:
            missing_pane.append(tab)

    print(f'\n  swept {len(_SETTINGS_TABS)} settings tabs')
    print('  errors during openSettings(): none')
    print(f'  tabs with HARD JS errors : {sorted(failures) or "none"}')
    print(f'  tabs whose pane not shown: {missing_pane or "none"}')

    assert not failures, (
        'settings tabs raised JavaScript errors:\n' + '\n'.join(
            f'  [{t}] {"; ".join(e)[:400]}' for t, e in failures.items()))
    assert not missing_pane, (
        f'switchSettingsTab did not activate a pane for: {missing_pane} — the '
        f'tab button exists but its #settingsTab_<id> panel does not render')


@pytest.mark.parametrize('label,opener,selector', _MODAL_PANELS,
                         ids=[p[0] for p in _MODAL_PANELS])
def test_modal_panel_opens_without_js_errors(page, label, opener, selector):
    """Each modal panel opens cleanly AND actually appears in the DOM."""
    if label == 'orchestration':
        # Feature flags ride the /api/v1/server-config boot response
        # (loadFeatureFlags(data.feature_flags)); the dedicated
        # /api/v1/features endpoint is only a fallback for an invalid
        # piggyback, so mocking it alone never reaches the loader.
        def _force_debug_flags(route):
            response = route.fetch()
            body = response.json()
            payload = body.get('data', body)
            payload['feature_flags'] = {
                'debug_mode': True, 'optimizer_enabled': True}
            route.fulfill(response=response, json=body)

        page.route('**/api/v*/server-config**', _force_debug_flags)
        page.reload()
    _wait_ready(page)
    _drain(page)
    _invoke_action(page, opener)
    page.wait_for_timeout(1200)

    errs = _hard_errors(page)
    assert not errs, (
        f'opening the {label} panel raised {len(errs)} JavaScript error(s):\n  '
        + '\n  '.join(errs[:8]))

    appeared = page.evaluate(f"() => !!document.querySelector({selector!r})")
    assert appeared, (
        f'{opener} produced no JS error but {selector} never appeared — the '
        f'entry point silently did nothing, which "no errors" alone cannot catch')
    if label == 'orchestration':
        chip = page.locator('[data-pkind="fanout"]')
        chip.wait_for(state='visible')
        chip.click()
        page.locator('#orchInspector textarea').first.wait_for(state='visible')
        assert page.locator('#orchInspector').inner_text().strip()
        page.screenshot(path='/tmp/tofu-unification-studio.png', full_page=False)



def test_paper_reading_mode_opens_without_js_errors(page):
    """Reading mode is the largest zero-coverage surface (6 sub-tabs).

    Only the shell is opened here: the six sub-tabs are paper-hash scoped and
    need a real document loaded, so asserting them would need a fixture paper.
    The shell alone already exercises paper-reader.js + arxiv.js boot.
    """
    _wait_ready(page)
    _drain(page)
    _invoke_action(page, 'togglePaperMode')
    page.wait_for_timeout(1500)

    errs = _hard_errors(page)
    assert not errs, (
        'entering reading mode raised JavaScript error(s):\n  '
        + '\n  '.join(errs[:8]))

    shown = page.evaluate(
        "() => { const el = document.getElementById('paperModeContainer');"
        "  return !!el && getComputedStyle(el).display !== 'none'; }")
    assert shown, (
        'togglePaperMode() raised no error but #paperModeContainer is not '
        'displayed — reading mode did not actually open')


def test_every_actionable_inline_onclick_handler_is_defined(page):
    """No actionable `onclick="foo()"` may call an undefined function.

    This is the generic form of the "click does nothing" bug: the handler is
    wired in HTML, the function was deleted or renamed, and nothing fails until
    a user clicks. Nothing in the suite checked it, and it costs one evaluate.

    Found on first run: `index.html:1812` wires the mobile "定时任务" item to
    `toggleScheduler()`, which does not exist anywhere — `scheduler.js` was
    removed as a dead panel but this call site survived the removal.

    Resolution runs INSIDE the browser against the real bundle, so it cannot
    drift from what users actually load (charter: no hand-copied symbol lists).

    Feature panels are loaded on demand.  Their hidden HTML is present in the
    shell before the owning Vite domain installs its inner handlers, while the
    user-reachable opener is a bridge stub that loads that domain.  Treating
    those deliberately dormant controls as actionable produced false failures
    for every lazy domain at once.  The panel tests above open those domains;
    this generic sweep guards controls a user can actually click now.
    """
    _wait_ready(page)
    undefined = page.evaluate("""() => {
        const bad = [];
        document.querySelectorAll('[onclick]').forEach(el => {
            // A lazy panel's dormant markup is not actionable until its owner
            // is loaded. getClientRects() accounts for display:none on the
            // element itself or any ancestor without copying CSS knowledge.
            if (el.getClientRects().length === 0) return;
            const code = el.getAttribute('onclick') || '';
            // Leading identifier of each `;`-separated statement that looks
            // like a bare call: `foo()` / `foo(args)`.
            code.split(';').forEach(stmt => {
                const m = stmt.trim().match(/^([A-Za-z_$][\\w$]*)\\s*\\(/);
                if (!m) return;
                const name = m[1];
                if (['if','for','while','switch','return','typeof','function']
                        .includes(name)) return;
                if (typeof window[name] !== 'function'
                    && typeof window.TofuModules.resolveAction(name) !== 'function') {
                    bad.push(name + '  (on #' + (el.id || el.className || '?') + ')');
                }
            });
        });
        return [...new Set(bad)];
    }""")
    print(f'\n  inline onclick handlers resolving to nothing: {undefined or "none"}')
    assert not undefined, (
        'these inline onclick handlers call functions that do not exist in the '
        'shipped bundle — clicking them throws and does nothing:\n  '
        + '\n  '.join(undefined))
