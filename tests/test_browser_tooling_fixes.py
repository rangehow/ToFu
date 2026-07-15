"""Drift-guard tests for the 2026-07-15 browser-tooling fixes.

Three fixes, each asserted present AND (where meaningful) with a neuter check
that the guard bites on regression:

  1. Screenshot decoupled from window size: _screenshotFullPageCDP forces a
     desktop viewport via Emulation.setDeviceMetricsOverride before capture and
     ALWAYS clears it in finally (before detach) so the user's real page layout
     is never corrupted — on success, capture-error, and empty-shot paths.
  2. browser_fill_form uses type_text (clears the field first) not
     keyboard_input (appends), so changing origin A→B replaces cleanly; plus
     description hard-steers to it for 2+ fields, with reverse pointers on
     browser_click / browser_keyboard.
  3. browser_create_tab / browser_navigate tell the model to web_search for the
     real URL when unsure instead of guessing a domain.

JS cannot run under pytest here, so the screenshot fix is verified by
structural analysis of the extracted function body (call ordering), following
the repo's existing verify-then-write discipline (see test_tool_audit_tranche1).
"""

import os
import re

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


def _extract_fn_body(src, fn_signature):
    """Return the brace-balanced body of a JS function starting at fn_signature."""
    start = src.index(fn_signature)
    brace = src.index('{', start)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
        i += 1
    raise AssertionError('unbalanced braces for ' + fn_signature)


# ── 1. Screenshot viewport override + guaranteed clear ──────────────

def test_screenshot_forces_viewport_override_before_capture():
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _screenshotFullPageCDP(')

    i_override = body.index('setDeviceMetricsOverride')
    i_capture = body.index('Page.captureScreenshot')
    assert i_override < i_capture, (
        'the device-metrics override must be applied BEFORE the capture so the '
        'page reflows to the forced desktop viewport first')

    # The override must be a plain desktop render: no scaling, not mobile.
    assert 'deviceScaleFactor: 1' in body
    assert 'mobile: false' in body


def test_screenshot_always_clears_override_in_finally_before_detach():
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _screenshotFullPageCDP(')

    # The clear must live in the finally block, guarded by `overridden`, and
    # must run BEFORE detach (clear needs the live debugger session). Key on
    # the actual sendCommand CALL — the string also appears in a comment and a
    # catch-log, which must not satisfy the guard.
    i_finally = body.rindex('} finally {')
    m_clear = re.search(
        r"sendCommand\(\s*target\s*,\s*'Emulation\.clearDeviceMetricsOverride'\s*\)",
        body)
    assert m_clear is not None, 'must actually call clearDeviceMetricsOverride'
    i_clear = m_clear.start()
    i_detach = body.index('chrome.debugger.detach')
    assert i_finally < i_clear < i_detach, (
        'the clearDeviceMetricsOverride CALL must be in finally and precede detach')
    assert 'if (overridden)' in body, (
        'clear must be guarded so we only clear when we actually overrode')

    # `overridden` is only set true AFTER the override call resolves, so a
    # failed override never triggers a bogus clear.
    assert re.search(r'overridden\s*=\s*true', body)
    assert re.search(r'overridden\s*=\s*false', body), (
        'a rejected override must reset the flag so finally skips the clear')


def test_screenshot_override_clear_neuter_bites():
    # Neuter: a body without the clear call in finally is exactly the pre-fix
    # regression that would leave the user's page shrunk/emulated. The guard
    # (test above) keys on clearDeviceMetricsOverride being present in finally.
    src = _src('browser_extension/background.js')
    body = _extract_fn_body(src, 'async function _screenshotFullPageCDP(')
    # Remove ONLY the sendCommand clear call (leave the comment) — this is the
    # exact pre-fix regression. The load-bearing test keys on this call, so it
    # would then raise (bites).
    neutered = re.sub(
        r"await chrome\.debugger\.sendCommand\(target, 'Emulation\.clearDeviceMetricsOverride'\);",
        'void 0;', body)
    assert re.search(
        r"sendCommand\(\s*target\s*,\s*'Emulation\.clearDeviceMetricsOverride'\s*\)",
        neutered) is None, 'sanity: neuter removed the clear CALL'


# ── 2. fill_form clears + reverse pointers ──────────────────────────

def test_fill_form_type_uses_type_text_not_append():
    src = _src('lib/browser/advanced.py')
    body = src[src.index('def fill_form_sequential'):src.index('def fill_form_sequential') + 2000]
    # The 'type' branch must send type_text with clearFirst, NOT keyboard_input.
    assert "'type_text'" in body
    assert "'clearFirst': True" in body


def test_fill_form_type_branch_no_longer_appends_via_keyboard():
    # Neuter/regression sentinel: the old code path used keyboard_input to type
    # the value (which appends). That specific pattern must be gone from the
    # 'type' branch so a pre-filled field is replaced, not concatenated.
    src = _src('lib/browser/advanced.py')
    start = src.index("if field_type == 'type':")
    branch = src[start:start + 800]
    # The regression is CALLING keyboard_input to send the value. Match the
    # actual command dispatch (the word may still appear in an explanatory
    # comment, which is fine).
    assert "send_browser_command('keyboard_input'" not in branch, (
        "the 'type' branch must not send keyboard_input for the value (that "
        "appends onto the existing field value)")


def test_fill_form_description_steers_multi_field():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_FILL_FORM
    desc = ADVANCED_BROWSER_TOOL_FILL_FORM['function']['description']
    assert '2+' in desc or 'MULTIPLE' in desc
    assert 'CLEARED' in desc or 'cleared' in desc


def test_click_and_keyboard_point_back_to_fill_form():
    from lib.tools.browser import BROWSER_TOOL_CLICK, BROWSER_TOOL_KEYBOARD
    click_desc = BROWSER_TOOL_CLICK['function']['description']
    kbd_desc = BROWSER_TOOL_KEYBOARD['function']['description']
    assert 'browser_fill_form' in click_desc, (
        'click must reverse-point to fill_form for multi-field forms')
    assert 'browser_fill_form' in kbd_desc, (
        'keyboard must reverse-point to fill_form (it appends)')


# ── 3. search-first URL guidance ────────────────────────────────────

def test_create_tab_and_navigate_tell_model_to_search_first():
    from lib.tools.browser import BROWSER_TOOL_CREATE_TAB, BROWSER_TOOL_NAVIGATE
    for tool in (BROWSER_TOOL_CREATE_TAB, BROWSER_TOOL_NAVIGATE):
        desc = tool['function']['description']
        assert 'web_search' in desc, (
            f"{tool['function']['name']} must instruct web_search when unsure")
        assert 'guess' in desc.lower() or 'memory' in desc.lower(), (
            'must explicitly forbid guessing the URL from memory')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
