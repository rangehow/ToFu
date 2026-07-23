"""Frontend test — the Studio dial must ALWAYS open the project panel.

Regression: when the standalone project launcher button was folded into the
Air/Pro/Studio capability dial, the Studio segment became the ONLY affordance
for attaching / managing a project. But `setChatMode('studio')` only called
`openProjectModal()` when NO project was attached — once a project was already
attached, clicking Studio again just re-selected the mode and returned. That
left a conv already in Studio with no way to CHANGE its project path (clicking
Studio repeatedly was a silent no-op).

The fix: the Studio segment always opens the project panel — waiting for a real
attach when none exists, and reopening the panel (path management) when one is
already attached. This test drives the REAL shipped `setChatMode` under node
and asserts the panel opens in BOTH states. A neuter that early-returns without
opening in the has-project branch must fail the has-project case.
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
TOOLBAR_JS = ROOT / "static" / "js" / "main" / "main_toolbar_ui.js"


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _slice_fn(src: str, signature: str) -> str:
    """Brace-match slice a named function body out of a source file."""
    start = src.index(signature)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"could not slice {signature}")


def _set_chat_mode_fn() -> str:
    return _slice_fn(TOOLBAR_JS.read_text(encoding="utf-8"),
                     "function setChatMode(mode) {")


HARNESS = textwrap.dedent("""
    // Minimal stubs the shipped setChatMode references.
    let openCount = 0;
    let applied = null;
    let saved = 0;
    global.projectState = {projectState_js};
    global.openProjectModal = () => {{ openCount++; }};
    global._applyChatModeUI = (m) => {{ applied = m; }};
    global._saveConvToolState = () => {{ saved++; }};
    global.clearProject = () => {{}};
    global.debugLog = () => {{}};
    global.chatMode = 'chat';

    {set_chat_mode}

    setChatMode('studio');

    console.log(JSON.stringify({{ openCount, applied, saved }}));
""")


def _run(set_chat_mode: str, project_state: dict) -> dict:
    script = HARNESS.format(set_chat_mode=set_chat_mode,
                            projectState_js=json.dumps(project_state))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


NO_PROJECT = {"active": False, "path": ""}
WITH_PROJECT = {"active": True, "path": "/mnt/work/repo"}


def test_studio_opens_panel_when_no_project():
    out = _run(_set_chat_mode_fn(), NO_PROJECT)
    assert out["openCount"] == 1, "Studio must open the project panel to attach"
    assert out["applied"] is None, "dial must NOT flip to studio before a real attach"


def test_studio_reopens_panel_when_project_already_attached():
    """The reported bug: an already-attached project could not change its path
    because clicking Studio again was a no-op."""
    out = _run(_set_chat_mode_fn(), WITH_PROJECT)
    assert out["openCount"] == 1, (
        "Studio must reopen the project panel even when a project is already "
        "attached, so the path can be changed"
    )
    assert out["applied"] == "studio", "dial stays in studio when a project is attached"
    assert out["saved"] == 1, "tool state should persist when re-selecting studio"


def test_NC_early_return_without_open_breaks_has_project_case():
    """Neuter: restore the old behavior (return before opening in the
    has-project branch) → the reported bug reappears (panel never opens)."""
    fn = _set_chat_mode_fn()
    # Re-insert an early return right after the has-project apply block, before
    # the unconditional openProjectModal call — mirroring the pre-fix code.
    neutered = fn.replace(
        "      debugLog('Mode: Studio (project attached)', 'success');\n    }\n"
        "    if (typeof openProjectModal === 'function') openProjectModal();\n"
        "    return;",
        "      debugLog('Mode: Studio (project attached)', 'success');\n"
        "      return;\n    }\n"
        "    if (typeof openProjectModal === 'function') openProjectModal();\n"
        "    return;",
    )
    assert neutered != fn, "neuter substitution did not apply"
    out = _run(neutered, WITH_PROJECT)
    assert out["openCount"] == 0, (
        "NEUTER must reproduce the bug: with the early return, an already-"
        "attached project never reopens the panel"
    )
