"""tests/test_frontend_local_control_merge.py — the merged "Local Control" surface.

WHAT THIS GUARDS
----------------
Desktop control and the browser bridge used to be two toolbar rows, two
mental models, and two very different levels of care: the browser row opened
a setup modal with a live status dot, while ``toggleDesktop`` was a blind
three-line flag flip with NO status check at all. Since
``lib/tools/registry/_build.py::_build_desktop`` ships ZERO desktop tools when
no agent is connected, a user could switch desktop control "on" and get an
enabled-looking toggle whose tools never existed.

They are now ONE entry (``#localControlToggle``) opening ONE modal
(``#localControlModal``) with two capability rows. The two backing wire flags
stay separate — only the surface merged.

The load-bearing behaviour, and what each test below pins:

  1. Exactly ONE install instruction is rendered per capability, chosen by
     DETECTED state — never a menu of every possible path. This is the whole
     point of the merge: minimum cognitive load.
  2. The desktop instruction comes from the BACKEND's ``setup_state``, because
     only the server process can see ``sys.frozen``. The three states render
     three DIFFERENT instructions, and only the remote one offers a token.
  3. A connected capability renders NO instruction at all.

DISCIPLINE (charter: "禁止在测试 harness 里手抄生产判据")
--------------------------------------------------------
The renderers are spliced OUT OF THE SHIPPED SOURCE at run time and evaluated
under jsdom — no predicate is hand-copied into this file, so the guard cannot
drift into asserting a world that production left behind. Symbols are located
by SEARCHING for their definition, not by hardcoding a line range, and the
three failure modes are separately diagnosable (deleted / duplicated / found).
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
JS_DIR = ROOT / "static" / "js"
INDEX_HTML = ROOT / "index.html"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"], cwd=ROOT,
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


def _find_defining_file(symbol: str) -> Path:
    """Locate the ONE file defining ``function <symbol>(``.

    Anchored on the SYMBOL, not a hardcoded path, so a legitimate module split
    re-points the guard instead of producing an unreadable false red. All three
    outcomes are separately diagnosable.
    """
    needle = f"function {symbol}("
    hits = [p for p in sorted(JS_DIR.rglob("*.js"))
            if not p.name.startswith("bundle-")
            and not p.name.startswith("feature-")
            and needle in p.read_text(encoding="utf-8")]
    if not hits:
        raise AssertionError(
            f"IMPLEMENTATION GONE: no file defines `{needle}`. If the merged "
            f"Local Control surface was removed, this guard is protecting "
            f"nothing — decide whether the protection should still exist "
            f"before repointing it.")
    if len(hits) > 1:
        raise AssertionError(
            f"SINGLE SOURCE COPIED: `{needle}` is defined in "
            f"{[str(p.relative_to(ROOT)) for p in hits]} — the merged surface "
            f"must have exactly one renderer per capability.")
    return hits[0]


def _slice_fn(symbol: str) -> str:
    """Brace-match the named function out of whichever file defines it."""
    src = _find_defining_file(symbol).read_text(encoding="utf-8")
    start = src.index(f"function {symbol}(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"could not brace-match {symbol}")


# Every helper the two renderers call, spliced from the same shipped source.
_SHIPPED_SYMBOLS = (
    "_lcT", "_lcEsc", "_lcSetStatus", "_lcSetSwitch",
    "_lcBrowserSetupState", "_lcRenderBrowser", "_lcRenderDesktop",
    "_lcMintToken",
)


def _shipped(extra_neuter=None) -> str:
    """Concatenate the shipped renderers; optionally apply a NEUTER rewrite."""
    body = "\n".join(_slice_fn(s) for s in _SHIPPED_SYMBOLS)
    if extra_neuter:
        neutered = extra_neuter(body)
        assert neutered != body, "NEUTER substitution did not apply"
        body = neutered
    return body


# Markup mirroring the shipped modal's two capability rows.
MODAL_HTML = """
<div id="localControlModal">
  <div class="lc-cap">
    <span class="lc-cap-status" id="lcBrowserStatus">
      <span class="browser-status-dot disconnected"></span>
      <span class="lc-status-text">checking</span>
    </span>
    <button class="lc-switch" id="lcBrowserSwitch" aria-checked="false"></button>
    <div class="lc-cap-setup" id="lcBrowserSetup"></div>
  </div>
  <div class="lc-cap">
    <span class="lc-cap-status" id="lcDesktopStatus">
      <span class="browser-status-dot disconnected"></span>
      <span class="lc-status-text">checking</span>
    </span>
    <button class="lc-switch" id="lcDesktopSwitch" aria-checked="false"></button>
    <div class="lc-cap-setup" id="lcDesktopSetup"></div>
  </div>
</div>
"""

HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body>{html}</body>`);
    global.document = dom.window.document;
    global.window = dom.window;
    global.browserEnabled = false;
    global.desktopEnabled = false;
    global.t = (k) => k;                    // i18n absent -> fallback strings
    global.escapeHtml = (s) => String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    global.Api = {{ desktop: {{ mintToken: () => Promise.resolve({{token:'T'}}) }} }};
    global.showToast = () => {{}};
    global._safeClipboardWrite = () => Promise.resolve();
    global.downloadBrowserExtension = () => {{}};
    global._applyBrowserLnaWarning = () => {{}};

    {shipped}

    // Render each DESKTOP setup_state into a fresh slot and capture what the
    // user would actually see.
    const desktopStates = ['connected', 'tray', 'local_source', 'remote'];
    const desktop = {{}};
    for (const st of desktopStates) {{
      document.getElementById('lcDesktopSetup').innerHTML = '';
      _lcRenderDesktop({{ connected: st === 'connected', setup_state: st }});
      const el = document.getElementById('lcDesktopSetup');
      desktop[st] = {{
        steps: el.querySelectorAll('.lc-step').length,
        text: el.textContent.trim(),
        hasMintButton: !!el.querySelector('#lcMintBtn'),
        dotConnected: !!document.querySelector(
          '#lcDesktopStatus .browser-status-dot.connected'),
      }};
    }}

    // Same for the BROWSER row: connected / on-disk folder / remote download.
    const browser = {{}};
    const bcases = {{
      connected:     {{ connected: true, clients: [{{client_id:'abcdef123'}}], secondsAgo: 2 }},
      load_unpacked: {{ connected: false, extensionPath: '/srv/tofu/browser_extension' }},
      download:      {{ connected: false }},
    }};
    for (const k of Object.keys(bcases)) {{
      document.getElementById('lcBrowserSetup').innerHTML = '';
      _lcRenderBrowser(bcases[k]);
      const el = document.getElementById('lcBrowserSetup');
      browser[k] = {{
        steps: el.querySelectorAll('.lc-step').length,
        text: el.textContent.trim(),
        hasPath: el.textContent.includes('/srv/tofu/browser_extension'),
        hasDownloadBtn: !!el.querySelector('#lcExtDownloadBtn'),
      }};
    }}

    console.log(JSON.stringify({{ desktop, browser }}));
""")


def _run(shipped: str) -> dict:
    script = HARNESS.format(shipped=shipped,
                            html=MODAL_HTML.replace("`", "\\`"))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Scan-surface report ────────────────────────────────────────────────
# charter: "扫描类守卫必须先验证扫描面" — print what was actually spliced
# before asserting anything about it.

def test_scan_surface_report(capsys):
    """Print which symbols were located and where, before any assertion."""
    located = {s: str(_find_defining_file(s).relative_to(ROOT))
               for s in _SHIPPED_SYMBOLS}
    with capsys.disabled():
        print("\n[scan surface] shipped renderers spliced from:")
        for sym, path in located.items():
            print(f"  {sym:<24} <- {path}")
    assert len(located) == len(_SHIPPED_SYMBOLS)


# ── 1. The merge itself: ONE entry, not two ────────────────────────────

def test_the_two_toolbar_rows_became_one():
    """#browserToggle / #desktopToggle are gone; one merged row replaces them.

    Asserts the RESULT the user sees (a single entry), not any implementation
    detail of how it is wired.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="localControlToggle"' in html, "merged toolbar entry missing"
    assert 'id="browserToggle"' not in html, (
        "#browserToggle still present — the rows did not merge, they "
        "multiplied")
    assert 'id="desktopToggle"' not in html, (
        "#desktopToggle still present — the rows did not merge")
    # Mobile must not keep a second, unmerged pair.
    assert 'id="mobileLocalControl"' in html, "merged mobile row missing"
    assert 'id="mobileBrowser"' not in html and 'id="mobileDesktop"' not in html, (
        "mobile sheet still carries the two separate rows")


def test_there_is_exactly_one_setup_modal():
    """The old standalone #browserModal must not survive alongside the merged one."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="localControlModal"' in html
    assert 'id="browserModal"' not in html, (
        "#browserModal still exists — two setup modals is the duplication "
        "this merge exists to remove")


# ── 2. ONE instruction per state (the cognitive-load contract) ─────────

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_each_desktop_state_renders_exactly_one_instruction():
    out = _run(_shipped())["desktop"]

    # Connected: nothing to install. An instruction here would be noise.
    assert out["connected"]["steps"] == 0, (
        "a connected agent must show NO install instruction")
    assert out["connected"]["text"] == ""

    # Each unconnected state: exactly one, never a menu.
    for st in ("tray", "local_source", "remote"):
        assert out[st]["steps"] == 1, (
            f"setup_state={st} rendered {out[st]['steps']} instructions — "
            f"must be exactly ONE next action")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_three_states_give_three_different_instructions():
    """If they all said the same thing, the detection would be decorative."""
    out = _run(_shipped())["desktop"]
    texts = {st: out[st]["text"] for st in ("tray", "local_source", "remote")}
    assert len(set(texts.values())) == 3, (
        f"states must differ, got {texts}")
    # The packaged-app case is the one-click path: tray, no token.
    assert "tray" in texts["tray"].lower() or "托盘" in texts["tray"]


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_only_the_remote_state_offers_a_token():
    """A token is meaningless unless the user's machine is not this machine."""
    out = _run(_shipped())["desktop"]
    assert out["remote"]["hasMintButton"] is True, (
        "the remote case is the only one needing a token — it must offer one")
    for st in ("connected", "tray", "local_source"):
        assert out[st]["hasMintButton"] is False, (
            f"setup_state={st} must NOT ask for a token")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_deprecated_cli_flow_is_never_taught():
    """`python -m lib.desktop_agent` is the SUPERSEDED path.

    desktop/launcher.py's own docstring says the in-process tray agent
    "Replaces the old 'install a second program and run python -m
    lib.desktop_agent' flow". Teaching a four-flag CLI invocation in a setup
    dialog is the opposite of minimising cognitive load.
    """
    out = _run(_shipped())["desktop"]
    for st, info in out.items():
        assert "lib.desktop_agent" not in info["text"], (
            f"setup_state={st} teaches the deprecated CLI flow")
        for flag in ("--allow-write", "--allow-exec", "--allow-gui",
                     "--allow-all", "--bridge-secret"):
            assert flag not in info["text"], (
                f"setup_state={st} exposes the raw CLI flag {flag}")


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_browser_row_picks_the_actionable_instruction():
    """On-disk folder when the backend says so; download otherwise."""
    out = _run(_shipped())["browser"]

    assert out["connected"]["steps"] == 0, (
        "a connected extension must show NO install instruction")

    # extensionPath is only sent for a loopback peer (routes/api_v1/browser.py),
    # i.e. exactly when the folder really is reachable from the user's Chrome.
    assert out["load_unpacked"]["steps"] == 1
    assert out["load_unpacked"]["hasPath"] is True, (
        "on-disk case must show the folder to load")
    assert out["load_unpacked"]["hasDownloadBtn"] is False, (
        "a download button here would be a second, redundant path")

    # No path -> remote Chrome cannot load a server-side folder.
    assert out["download"]["steps"] == 1
    assert out["download"]["hasDownloadBtn"] is True
    assert out["download"]["hasPath"] is False


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_connected_state_lights_the_dot():
    """Complement: the merge must not lose the live status signal itself."""
    out = _run(_shipped())["desktop"]
    assert out["connected"]["dotConnected"] is True, (
        "a connected agent must light the status dot")


# ── 3. NEUTER — prove the detection is load-bearing ────────────────────

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_ignoring_setup_state_collapses_the_three_paths():
    """Make the desktop renderer ignore the backend's verdict.

    If the guard stays green after this, it was never testing the detection —
    only that *something* renders.
    """
    out = _run(_shipped(
        lambda s: s.replace("switch (d.setup_state) {", "switch ('remote') {")
    ))["desktop"]
    texts = {st: out[st]["text"] for st in ("tray", "local_source", "remote")}
    assert len(set(texts.values())) == 1, (
        "NEUTER should have collapsed every state to one instruction")
    # And the specific consequence: a tray user gets told to mint a token.
    assert out["tray"]["hasMintButton"] is True


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_showing_every_path_at_once_breaks_the_one_action_rule():
    """A menu of all three instructions must be rejected, not tolerated."""
    out = _run(_shipped(lambda s: s.replace(
        "case 'tray':\n      // Packaged desktop app: the agent runs IN-PROCESS. One click, no token,\n      // no second program to install.\n      setup.innerHTML = '<p class=\"lc-step\">'",
        "case 'tray':\n      setup.innerHTML = '<p class=\"lc-step\">x</p><p class=\"lc-step\">y</p>' + '<p class=\"lc-step\">'",
    )))["desktop"]
    assert out["tray"]["steps"] > 1, "NEUTER did not inject extra instructions"


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NEUTER_rendering_an_instruction_while_connected_is_noise():
    """Complement to the 'exactly one' rule: connected must render ZERO.

    Without this, deleting the whole instruction block would keep every other
    test green while making the modal useless.
    """
    out = _run(_shipped(
        lambda s: s.replace("case 'connected':\n      setup.innerHTML = '';",
                            "case 'connected':\n      setup.innerHTML = '<p class=\"lc-step\">z</p>';")
    ))["desktop"]
    assert out["connected"]["steps"] == 1, (
        "NEUTER did not inject an instruction into the connected state")


# ── 4. Live polling — the 15s connection window ────────────────────────

def test_the_modal_polls_while_open():
    """`is_desktop_agent_connected()` is a 15s window, and a user enabling the
    tray agent while looking at this dialog must see the dot flip WITHOUT
    reopening it. The old _checkBrowserStatus was one-shot-on-open; that
    limitation must not be inherited.

    Asserts the RESULT (a repeating refresh that is torn down on close),
    located by symbol rather than by line.
    """
    src = _find_defining_file("openLocalControlModal").read_text(encoding="utf-8")
    open_fn = _slice_fn("openLocalControlModal")
    close_fn = _slice_fn("closeLocalControlModal")
    assert "setInterval" in open_fn, (
        "opening the modal must start a repeating refresh — a one-shot check "
        "cannot show the dot flipping within the 15s connection window")
    assert "clearInterval" in close_fn, (
        "closing the modal must stop the poll, or a background tab keeps "
        "hitting the server forever")
    m = re.search(r"_LC_POLL_MS\s*=\s*(\d+)", src)
    assert m, "poll cadence must be a named constant"
    assert int(m.group(1)) < 15000, (
        f"poll cadence {m.group(1)}ms is not shorter than the 15s connection "
        f"window — a state change could be missed entirely")
