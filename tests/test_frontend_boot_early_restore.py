"""Frontend boot first-open seamlessness (ChatGPT-parity).

A returning user must land DIRECTLY in their last-active conversation the moment
the page opens — not stare at the "New Chat" welcome screen for the whole server
round-trip on a slow / flaky tunnel, then watch it snap to their conversation.

Two seams make this seamless and are guarded here as SOURCE INVARIANTS (no jsdom
— matches the repo's node-free source-assertion convention for boot wiring the
end-to-end harness can't exercise):

  1. main.js — right after `hydrateSidebarFromCache()` resolves (the cache-first
     sidebar paint, BEFORE the server round-trip), boot opens the restored
     conversation via `loadConversation(_restoredConvId)`. loadConversation
     paints cache-first (real messages in a few ms) or an instant skeleton, so
     the user sees their conversation immediately. Gated on:
       • `!activeConvId`  (user hasn't already navigated — don't yank them),
       • the EXACT restored id EXISTS in the cache-hydrated list (never the
         conversations[0] fallback — that stays in the post-server
         _bootRestoreActiveConv where the server list is authoritative).

  2. main_init_tasks.js — `_ensureNewest()` must NOT repaint over an in-flight
     first-open skeleton. When boot restored a conv that is still mid
     initial-switch-load (`_initialSwitchLoad` + `_needsLoad` + empty messages),
     a full renderChat there would paint the generic "Loading conversation…"
     welcome over the nicer shimmer skeleton — a downgrade flash. The guard
     leaves the skeleton up; the load's own `.then` lands the real messages.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN_JS = REPO / "static" / "js" / "main.js"
INIT_JS = REPO / "static" / "js" / "main" / "main_init_tasks.js"


def test_boot_restores_last_conv_from_cache_before_server():
    """After hydrateSidebarFromCache resolves, boot opens the restored conv via
    loadConversation — inside the .then, gated on !activeConvId + the restored
    id existing in the (cache-painted) conversations list."""
    src = MAIN_JS.read_text()
    # The early-restore lives in the hydrateSidebarFromCache().then(...) block.
    m = re.search(r"hydrateSidebarFromCache\(\)\s*\.then\(\s*\(\)\s*=>\s*\{([\s\S]*?)\}\)\s*\.catch", src)
    assert m, "hydrateSidebarFromCache().then(...).catch() block not found"
    block = m.group(1)
    assert "loadConversation(_restoredConvId)" in block, \
        "boot must open the restored conv from cache before the server round-trip"
    # Gated on not-yet-navigated + restored id present in the hydrated list.
    assert "!activeConvId" in block, "early-restore must be gated on !activeConvId"
    assert "_restoredConvId" in block and "conversations.find" in block, \
        "early-restore must verify the restored id exists in the cache-painted list"
    # It must open the restored id by identity match — never a positional
    # fallback. (Guarded by the c.id === _restoredConvId find above; the
    # conversations[0] fallback deliberately stays in the post-server
    # _bootRestoreActiveConv, not this pre-server cache path.)
    assert "c.id === _restoredConvId" in block, \
        "early cache path must open the restored id by identity, not a positional fallback"


def test_early_restore_is_only_in_cache_then_not_synchronous():
    """The loadConversation(_restoredConvId) early-open must live INSIDE the
    async hydrate .then (so the cached sidebar is painted first), not fire
    synchronously at boot before the cache list exists."""
    src = MAIN_JS.read_text()
    then_start = src.find("hydrateSidebarFromCache().then(")
    assert then_start != -1
    # The newChat() welcome paint happens earlier in boot; ensure the restore
    # call is positioned AFTER the hydrate .then opens (not before it).
    call_pos = src.find("loadConversation(_restoredConvId)")
    assert call_pos != -1 and call_pos > then_start, \
        "loadConversation(_restoredConvId) must be inside the hydrate .then block"


def test_ensure_newest_preserves_inflight_skeleton():
    """_ensureNewest must skip its full renderChat when the active conv is still
    mid first-open load (skeleton painted): _initialSwitchLoad + _needsLoad +
    empty messages. Otherwise it repaints the generic loading welcome over the
    skeleton (downgrade flash)."""
    src = INIT_JS.read_text()
    m = re.search(r"function _ensureNewest\s*\(\)\s*\{([\s\S]*?)\n\}", src)
    assert m, "_ensureNewest not found"
    body = m.group(1)
    # The skeleton-preservation guard gates the renderChat(c) call.
    assert re.search(
        r"_initialSwitchLoad[\s\S]{0,80}_needsLoad[\s\S]{0,80}messages\.length\s*===\s*0[\s\S]{0,80}renderChat\(c\)",
        body,
    ), "_ensureNewest must guard renderChat(c) against an in-flight first-open skeleton"


if __name__ == "__main__":
    test_boot_restores_last_conv_from_cache_before_server(); print("PASS early-restore")
    test_early_restore_is_only_in_cache_then_not_synchronous(); print("PASS in-then")
    test_ensure_newest_preserves_inflight_skeleton(); print("PASS skeleton-guard")
    print("ALL GREEN")
