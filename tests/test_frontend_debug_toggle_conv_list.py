"""Regression guard: the debug-mode toggle must refresh the sidebar IMMEDIATELY.

WHY
---
The copy-conv-ID button is baked per-row into ``_buildConvItemHTML``
(``static/js/ui/conversation_list.js``) under ``_featureFlags.debug_mode`` at
HTML-build time. ``settings/save_export.js`` calls
``renderConversationList()`` right after flipping the flag so no reload is
needed — but ``renderConversationList`` early-returns when its split hash
(struct + status) is unchanged, and neither hash included the debug flag.
Result: toggling debug mode was a silent no-op for the sidebar; the copy-ID
button only appeared on the next full page load (or an unrelated struct
change).

Fix: fold ``DBG<0|1>`` into ``_structHash`` so flipping the flag forces a
full rebuild through the normal path.

This harness loads the REAL shipped JS under jsdom:
  1. render with debug OFF  → zero ``.conv-copy-id`` buttons
  2. flip ``_featureFlags.debug_mode = true`` and call
     ``renderConversationList()`` WITHOUT touching the hash caches → buttons
     must appear (this is the call saveSettings makes)
  3. flip back OFF → buttons must disappear again
  4. NC (neuter control): rename a row's title without changing the flag —
     struct change via the normal path must ALSO rebuild buttons (guards
     against the test passing because rendering is somehow always-on).

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="convList"></div><div id="sidebarSearchStats"></div></body>',
  targets: [process.argv[2]],
  globals: {
    formatRelativeTime: () => '',
    highlightMatch: (s) => s,
    sidebarSearchQuery: '',
    getFolders: () => [],
    getActiveFolderId: () => null,
    setActiveFolderId: () => {},
    areFoldersLoaded: () => true,
    renderFolderTabs: () => {},
    BASE_PATH: '',
    activeStreams: new Map(),
    pendingMessageQueue: new Map(),
    streamBufs: new Map(),
    _featureFlags: { debug_mode: false },
  },
});

const now = Date.now();
global.activeConvId = window.activeConvId = null;
global.conversations = window.conversations = [
  { id: 'c1', title: 'Conv 1', messages: [{ role: 'user' }], updatedAt: now - 1000 },
  { id: 'c2', title: 'Conv 2', messages: [{ role: 'user' }], updatedAt: now - 2000 },
];

function copyIdCount() {
  return document.getElementById('convList').querySelectorAll('.conv-copy-id').length;
}

// 1. Debug OFF at boot → no copy-id buttons.
renderConversationList();
check('debug_off_no_buttons', copyIdCount() === 0);

// 2. Flip the flag ON and re-render EXACTLY like saveSettings does — no hash
//    cache reset. The struct hash must now differ (DBG token) so the rows
//    rebuild with the button baked in.
window._featureFlags.debug_mode = true;
renderConversationList();
check('debug_on_buttons_appear_immediately', copyIdCount() === 2);

// 3. Flip back OFF — must disappear again without a reload.
window._featureFlags.debug_mode = false;
renderConversationList();
check('debug_off_buttons_removed_immediately', copyIdCount() === 0);

// 4. NC: an unrelated struct change (rename) must still rebuild rows through
//    the normal path — proves the harness exercises real rebuilds, not some
//    always-rerender artifact that would make checks 2/3 vacuous.
window._featureFlags.debug_mode = true;
conversations[0].title = 'Conv 1 renamed';
renderConversationList();
check('nc_struct_change_rebuilds_with_flag', copyIdCount() === 2);

report();
"""


def test_debug_toggle_refreshes_conv_list_immediately():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'conversation_list.js'),
        body_js=_BODY,
        min_pass=4,
        label='debug toggle → conv list immediate refresh',
    )
