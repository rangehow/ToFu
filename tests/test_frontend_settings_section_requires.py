"""Frontend test — the degraded-section contract (settings/section_requires.js).

THE DEFECT (found by the owner in the live UI, 2026-07-29): the "Brand mascot"
settings block ships its heading + description as static server-spliced HTML,
while its tiles are painted by JS from core/brand_logo.js. A running server
holds the bundle manifest it booted with, so a NEWLY added module is absent
from the served bundle until a restart — and the user was shown a title, a
description, and an EMPTY BOX. A control that looks available and does nothing.

Every existing brand-logo guard tests the module's own behaviour; none of them
could see this, because the failure only exists when the module is ABSENT.
That is exactly what this file tests: what the UI looks like when the JS is
not there.

The contract is generic (any block may declare `data-requires`), so the tests
drive it with a synthetic section as well as the real shipped markup.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "static" / "js" / "settings" / "section_requires.js"
PANEL = ROOT / "static" / "settings_panels" / "general.html"
STYLES = ROOT / "static" / "styles.css"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"],
                       cwd=ROOT, capture_output=True, check=True)
        return True
    except Exception:
        return False


def _logo_section_html() -> str:
    """The REAL shipped markup for the brand-mascot block (anchored slice)."""
    src = PANEL.read_text(encoding="utf-8")
    start = src.index('<div class="settings-section-needs-js"')
    end = src.index("</div>", src.index('settings-section-js-missing', start))
    return src[start:end + len("</div>")] + "</div>"


HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{body}</body>`,
                          {{ url: 'http://localhost:15000/' }});
    global.window = dom.window;
    global.document = dom.window.document;
    {defines}
    // ---- BEGIN real shipped module ----
    {source}
    // ---- END real shipped module ----
    const degraded = window.applySectionRequirements();
    const sec = document.querySelector('.settings-section-needs-js');
    const picker = document.getElementById('logoSkinPicker');
    const notice = document.querySelector('.settings-section-js-missing');
    console.log(JSON.stringify({{
      degradedCount: degraded,
      hasDegradedClass: sec ? sec.classList.contains('degraded') : null,
      pickerEmpty: picker ? picker.children.length === 0 : null,
      noticeText: notice ? notice.textContent.trim() : null,
    }}));
""")


def _run(body: str, defines: str = "") -> dict:
    script = HARNESS.format(body=body, defines=defines,
                            source=MODULE.read_text(encoding="utf-8"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_missing_module_degrades_the_real_logo_section():
    """The exact live failure: brand_logo.js absent → the block must NOT sit
    there as an empty box; it must be marked degraded so CSS hides the control
    and reveals the notice."""
    out = _run(_logo_section_html())          # no listLogoSkins defined
    assert out["degradedCount"] == 1, "the block must be counted as degraded"
    assert out["hasDegradedClass"] is True, (
        "with its module missing the section MUST carry .degraded — otherwise "
        "the user sees a title + description + empty box (a dead control)"
    )
    assert out["pickerEmpty"] is True, "precondition: no JS means no tiles"
    assert out["noticeText"], "a human-readable notice must be present to show"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_present_module_leaves_the_section_fully_usable():
    out = _run(_logo_section_html(),
               defines="global.window.listLogoSkins = function () { return []; };")
    assert out["degradedCount"] == 0
    assert out["hasDegradedClass"] is False, (
        "with the module present the section must render normally"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_contract_is_generic_not_a_logo_special_case():
    """Any block can declare a dependency — adding a JS-gated section is one
    attribute, not new code. Also covers the multi-symbol form."""
    body = ('<div class="settings-section-needs-js" data-requires="someFn otherFn">'
            '<div class="theme-picker" id="logoSkinPicker"></div>'
            '<div class="settings-section-js-missing">needs restart</div></div>')
    both_missing = _run(body)
    assert both_missing["hasDegradedClass"] is True
    one_missing = _run(body, defines="global.window.someFn = function () {};")
    assert one_missing["hasDegradedClass"] is True, (
        "a block must degrade unless EVERY declared symbol is present"
    )
    all_present = _run(body, defines=("global.window.someFn = function () {};"
                                      "global.window.otherFn = function () {};"))
    assert all_present["hasDegradedClass"] is False


def test_css_actually_hides_the_control_and_shows_the_notice():
    """The class flip is only meaningful if the stylesheet acts on it —
    otherwise the guard would pass while the user still sees the empty box."""
    css = STYLES.read_text(encoding="utf-8")
    assert re.search(r"\.settings-section-js-missing\{[^}]*display:none", css), (
        "the notice must be hidden by default"
    )
    assert re.search(r"\.settings-section-needs-js\.degraded>\.theme-picker[^{]*\{[^}]*display:none", css), (
        "a degraded section MUST hide its control (the empty box must go away)"
    )
    assert re.search(r"\.settings-section-needs-js\.degraded>\.settings-section-js-missing\{[^}]*display:block", css), (
        "a degraded section MUST reveal its notice"
    )


def test_the_shipped_logo_block_declares_its_dependency():
    """Guard the wiring itself: the real panel markup must carry the contract,
    or the mechanism above protects nothing in production."""
    html = PANEL.read_text(encoding="utf-8")
    assert 'data-requires="listLogoSkins"' in html, (
        "the brand-mascot block must declare the symbol it needs"
    )
    assert "settings-section-js-missing" in html, "it must ship a notice element"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_requirement_check_removed_leaves_the_dead_control():
    """Neuter: make the check always pass → the live defect returns (a block
    whose module is missing still presents itself as usable)."""
    src = MODULE.read_text(encoding="utf-8")
    poisoned = src.replace("if (typeof window[names[i]] === 'undefined') return false;",
                           "/* neutered */")
    script = HARNESS.format(body=_logo_section_html(), defines="", source=poisoned)
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["hasDegradedClass"] is False, (
        "NEUTER must leak: without the symbol check a section with a missing "
        "module is left looking usable — the exact dead control the owner hit"
    )
