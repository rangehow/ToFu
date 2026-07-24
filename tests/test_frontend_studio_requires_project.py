"""Frontend test — a project-less conversation is NEVER Studio.

Reported bug: click the Studio segment → the project panel pops up → close it
without picking a project → the dial still reads "Studio". The click path
itself is correct (``setChatMode('studio')`` with no project opens the panel
and deliberately does NOT flip the dial — pinned by
tests/test_frontend_studio_reopens_project.py). The dial was resurrected by
two interlocking holes:

1.  ``onProjectCleared()`` (and ``onProjectAttached()``) repainted the dial
    but never persisted it, so ``conv.chatMode`` kept the stale ``'studio'``
    with an empty ``projectPath`` after the project was cleared.
2.  ``_restoreConvToolState()`` trusted that stored tier verbatim —
    ``conv.chatMode || _deriveChatModeFromFlags(conv)`` — so the poisoned
    ``'studio'`` + no-``projectPath`` combination restored as Studio on the
    next reload / conv switch. Clicking Studio then only opens the panel
    (no dial change), and closing it leaves the wrongly-restored Studio in
    place — "还是 studio 模式".

Fix: (a) persist the tier immediately on attach/clear so ``conv.chatMode``
never lags the truth; (b) clamp on restore — a stored ``'studio'`` with no
``projectPath`` falls back to ``'chat'``, healing already-poisoned convs.

These tests drive the REAL shipped functions under node
(``_restoreConvToolState`` sliced from static/js/main.js,
``_deriveChatModeFromFlags`` / ``onProjectAttached`` / ``onProjectCleared``
from static/js/main/main_toolbar_ui.js). Two neuters prove the assertions are
load-bearing: without the restore clamp the poisoned conv restores as Studio;
without the persist hook the clear path reopens the poison.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
MAIN_JS = ROOT / "static" / "js" / "main.js"
TOOLBAR_JS = ROOT / "static" / "js" / "main" / "main_toolbar_ui.js"

RESTORE_CLAMP = "(_storedMode === 'studio' && !conv.projectPath) ? 'chat' : _storedMode"
PERSIST_HOOK = "if (typeof _saveConvToolState === 'function') _saveConvToolState();"


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


def _restore_fn() -> str:
    return _slice_fn(MAIN_JS.read_text(encoding="utf-8"),
                     "function _restoreConvToolState(conv) {")


def _derive_fn() -> str:
    return _slice_fn(TOOLBAR_JS.read_text(encoding="utf-8"),
                     "function _deriveChatModeFromFlags(conv) {")


def _toolbar_fn(name: str) -> str:
    return _slice_fn(TOOLBAR_JS.read_text(encoding="utf-8"),
                     f"function {name}() {{")


def _run(script: str) -> dict:
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── Harness A: restore path ─────────────────────────────────────────────
# Stubs for every bare call inside the real _restoreConvToolState. The
# typeof-guarded hooks (updateSubmenuCounts / updateContextBar /
# presenceRefresh / projectBrainRefresh / convInfluenceRefresh) are left
# undefined on purpose so the guard branches are exercised too.
RESTORE_HARNESS = """
const appliedLog = [];
const stub = () => {};
const config = {};
const serverModel = 'stub-model';
const _applyModelUI = stub, _applySearchModeUI = stub, _applyFetchEnabledUI = stub,
      _applyCodeExecUI = stub, _applyBrowserUI = stub, _applyDesktopUI = stub,
      _applyMemoryUI = stub, _applySchedulerUI = stub, _applySwarmUI = stub,
      _applyEndpointUI = stub, _applyAutopilotUI = stub, _applyFlowUI = stub,
      _applyImageGenToolUI = stub, _applyImageGenUI = stub,
      _applyHumanGuidanceUI = stub, _applyAutoTranslateUI = stub,
      _scheduleReflow = stub, convAutoTranslate = () => false;
const _applyChatModeUI = (m) => { appliedLog.push(m); };
__DERIVE_FN__
__RESTORE_FN__
_restoreConvToolState(__CONV__);
console.log(JSON.stringify({
  applied: appliedLog.length ? appliedLog[appliedLog.length - 1] : null,
  calls: appliedLog.length,
}));
"""


def _run_restore(conv: dict, restore_src: str | None = None) -> dict:
    script = (RESTORE_HARNESS
              .replace("__DERIVE_FN__", _derive_fn())
              .replace("__RESTORE_FN__", restore_src or _restore_fn())
              .replace("__CONV__", json.dumps(conv)))
    return _run(script)


# ── Harness B: attach/clear persistence ─────────────────────────────────
TIER_HOOK_HARNESS = """
let applied = null;
let saved = 0;
let chatMode = __CHAT_MODE__;
const _applyChatModeUI = (m) => { applied = m; };
const _saveConvToolState = () => { saved++; };
__FN__
__CALL__();
console.log(JSON.stringify({ applied, saved }));
"""


def _run_tier_hook(fn_name: str, chat_mode: str, fn_src: str | None = None) -> dict:
    script = (TIER_HOOK_HARNESS
              .replace("__CHAT_MODE__", json.dumps(chat_mode))
              .replace("__FN__", fn_src or _toolbar_fn(fn_name))
              .replace("__CALL__", fn_name))
    return _run(script)


# ═══════════════════════════════════════════════════════════════════════
# Restore clamp — a stored 'studio' with no projectPath must not survive
# ═══════════════════════════════════════════════════════════════════════

def test_restore_poisoned_studio_without_project_falls_back_to_chat():
    """The exact poisoned state from the reported bug: chatMode='studio' but
    no projectPath (left behind when the project was cleared before the
    fallback was persisted). Restore must heal it to chat."""
    out = _run_restore({"chatMode": "studio", "projectPath": ""})
    assert out["calls"] == 1
    assert out["applied"] == "chat", (
        "a project-less conversation must NEVER restore as Studio — the "
        "stored tier is poisoned and must be clamped"
    )


def test_restore_legit_studio_with_project_survives():
    """Studio + an attached project is the ONE legitimate studio state."""
    out = _run_restore({"chatMode": "studio", "projectPath": "/mnt/work/repo"})
    assert out["applied"] == "studio", (
        "clamping must not over-fire: a stored studio WITH a projectPath is valid"
    )


def test_restore_legacy_conv_without_chatMode_derives_from_project():
    """Pre-feature convs have no chatMode — derivation (project ⟺ studio)
    must still work."""
    out = _run_restore({"projectPath": "/mnt/work/repo"})
    assert out["applied"] == "studio"
    out = _run_restore({})
    assert out["applied"] == "chat"


def test_restore_clamp_leaves_legacy_non_studio_tiers_untouched():
    """The clamp must ONLY touch 'studio' — legacy air/pro tiers flow through
    to _applyChatModeUI (which normalises them) unchanged."""
    out = _run_restore({"chatMode": "air"})
    assert out["applied"] == "air", (
        "the clamp fired on a non-studio tier — it must be studio-specific"
    )


def test_NC_without_clamp_poisoned_conv_restores_as_studio():
    """Neuter: strip the restore clamp and the poisoned conv restores as
    Studio — proving the clamp is the only thing standing between a stale
    conv.chatMode and the reported bug."""
    fn = _restore_fn()
    assert RESTORE_CLAMP in fn, "harness stale: clamp expression not found"
    neutered = fn.replace(RESTORE_CLAMP, "_storedMode")
    assert neutered != fn
    out = _run_restore({"chatMode": "studio", "projectPath": ""},
                       restore_src=neutered)
    assert out["applied"] == "studio", (
        "NEUTER must reproduce the bug: without the clamp, poisoned "
        "chatMode='studio' + empty projectPath restores as Studio"
    )


# ═══════════════════════════════════════════════════════════════════════
# Attach / clear persistence — conv.chatMode must never lag the truth
# ═══════════════════════════════════════════════════════════════════════

def test_on_project_cleared_persists_chat_fallback():
    """Source of the poison: clearing a project while in studio repainted the
    dial but left conv.chatMode='studio' on disk. The fallback must be saved."""
    out = _run_tier_hook("onProjectCleared", "studio")
    assert out["applied"] == "chat"
    assert out["saved"] == 1, (
        "onProjectCleared must persist the chat fallback immediately — "
        "otherwise the stale 'studio' tier is resurrected on the next restore"
    )


def test_on_project_cleared_noop_when_already_chat():
    """No tier change → no needless persist."""
    out = _run_tier_hook("onProjectCleared", "chat")
    assert out["applied"] is None
    assert out["saved"] == 0


def test_on_project_attached_persists_studio_promotion():
    """Symmetric: the promotion to studio must also be durable right away,
    not whenever the next unrelated toggle happens to save."""
    out = _run_tier_hook("onProjectAttached", "chat")
    assert out["applied"] == "studio"
    assert out["saved"] == 1


def test_on_project_attached_noop_when_already_studio():
    out = _run_tier_hook("onProjectAttached", "studio")
    assert out["applied"] is None
    assert out["saved"] == 0


def test_NC_on_project_cleared_without_persist_reopens_poison():
    """Neuter: drop the persist hook from onProjectCleared — the save count
    falls to 0, which is exactly the old shape that poisoned conv.chatMode."""
    fn = _toolbar_fn("onProjectCleared")
    assert PERSIST_HOOK in fn, "harness stale: persist hook not found"
    neutered = fn.replace(PERSIST_HOOK, "")
    assert neutered != fn
    out = _run_tier_hook("onProjectCleared", "studio", fn_src=neutered)
    assert out["applied"] == "chat", "the repaint itself must still happen"
    assert out["saved"] == 0, (
        "NEUTER must reproduce the poison: repaint without persist leaves "
        "conv.chatMode stale"
    )


# ═══════════════════════════════════════════════════════════════════════
# Static pin — the clamp must live in the shipped restore path
# ═══════════════════════════════════════════════════════════════════════

def test_restore_clamp_present_in_main_js():
    assert RESTORE_CLAMP in _restore_fn(), (
        "static/js/main.js _restoreConvToolState lost the studio⟺project "
        "clamp — a stored 'studio' with no projectPath would restore as Studio"
    )
