---
name: frontend-main-decomposition
description: main.js (5144 LOC) decomposed into static/js/main/ subpackage of 8 files; main.js kept as bootstrap (1017 LOC)
enabled: true
tags: [refactor, frontend, javascript, convention]
created: 2026-05-28T06:59:43Z
updated: 2026-05-28T06:59:43Z
---

# `static/js/main.js` Decomposition (2026-05-28)

Second giant frontend file split. Same recipe as `frontend-ui-decomposition`
but with one critical constraint: **main.js MUST stay last** in
`_BUNDLE_FILES` (it boots the app via a trailing IIFE), so we extracted
the body OUT into sibling files and kept main.js as a **slim bootstrap
orchestrator**.

## Before

Single 5144-LOC `static/js/main.js` with:
- L1-58: globals, TAB_ID, BASE_PATH, log helpers (boot-time state)
- L59-662: tool-toolbar UI helpers (`_applyModelUI`, reflow, thinking,
  search, image-gen, conv-tool-state save/restore)
- L663-2206: conversation lifecycle, send pipeline, autopilot, queue UI
- L2207-2773: regenerate / continue
- L2774-3327: toolbar UI (model dropdown, presets, autoTranslate)
- L3328-4087: folders, sidebar, mobile UI (huge mobile backend section)
- L4088-4274: input handling, theme, sidebar search
- L4275-4798: `initActiveTasks` + `_ensureNewest` (heavy startup-resume)
- L4799-5144: boot IIFE + final `_markScriptsLoaded()`

## After

main.js kept as 1017 LOC bootstrap (head + pointer comment + tail).
The body extracted into 8 cohesive sibling files:

```
static/js/main/
  main_conv_lifecycle.js     455 LOC — newChat, loadConversation,
                                       deleteConversation, duplicateConversation,
                                       _build*Config helpers
  main_translating_bubble.js 119 LOC — _renderTranslatingBubble +
                                       send-translate-status poll
  main_send_pipeline.js     1000 LOC — startAssistantResponse, sendMessage,
                                       _attachAutopilotFollowup, _waitForVlmParsing,
                                       pending queue UI, _checkForQueuedTask,
                                       _refreshServerQueue
  main_regen_continue.js     577 LOC — regenerateFromUser, continueAssistant,
                                       _buildToolHistoryRound
  main_toolbar_ui.js         564 LOC — _populateModelDropdown,
                                       _loadServerConfigAndPopulate,
                                       _maybeAutoOpenSettings, presets,
                                       autoTranslate, submenu, browser/endpoint/
                                       autopilot toggles
  main_folders_mobile.js     770 LOC — folder picker / tabs / drag-drop +
                                       sidebar + mobile sheet +
                                       _updateMobileBackendSection (196 LOC)
  main_input_handling.js     197 LOC — handleKeyDown, _wrapSelectionNoTranslate,
                                       theme, sidebar search
  main_init_tasks.js         534 LOC — initActiveTasks, _ensureNewest
```

Total extracted: 4216 LOC. main.js: 1017 LOC. Sum: 5233 (vs original 5144;
+89 LOC = 8 × 10-line banners + 9-line pointer comment).

## Pure source split — body byte-equivalent

Every code line is unchanged. The only added text is:
- 8 × 10-line banner comments at the top of each extracted file
- 1 × 9-line pointer comment inside main.js explaining where to find the
  extracted symbols.

Verified at split time:
```
extracted bodies: 4136 lines (matches L663-L4798 = 4136 lines exactly)
new main.js head+pointer+tail = 1017 lines (= 662 + 9 pointer + 346 tail)
sum = 5153 = 5144 original + 9 pointer overhead
```

## Bundler ordering (CRITICAL)

`lib/js_bundler.py::_BUNDLE_FILES` order:

```python
'agent-backend.js',
# ── main/ subpackage (split 2026-05-28 from monolithic main.js) ──
'main/main_conv_lifecycle.js',
'main/main_translating_bubble.js',
'main/main_send_pipeline.js',
'main/main_regen_continue.js',
'main/main_toolbar_ui.js',
'main/main_folders_mobile.js',
'main/main_input_handling.js',
'main/main_init_tasks.js',
# Orchestrator (MUST be last) — boot IIFE that wires the app
'main.js',
'compaction-viewer.js',
'context-bar.js',
```

The 8 main/* files MUST appear before main.js; the boot IIFE in main.js
references their symbols.

`index.html` updated similarly (8 new `<script>` tags before main.js).

## Verification

- All 8 extracted files: `node -c` clean.
- New (slim) main.js: `node -c` clean.
- Bundler builds (`bundle-76409697.js`).
- 25 sampled symbols verified present in the bundle:
  `newChat`, `loadConversation`, `deleteConversation`, `duplicateConversation`,
  `sendMessage`, `startAssistantResponse`, `regenerateFromUser`, `continueAssistant`,
  `_populateModelDropdown`, `_loadServerConfigAndPopulate`,
  `_showFolderPicker`, `_initFolderTabs`, `_updateMobileBackendSection`,
  `handleKeyDown`, `applyTheme`, `initActiveTasks`, `_ensureNewest`,
  `renderPendingQueueUI`, `_checkForQueuedTask`, `_refreshServerQueue`,
  `_renderTranslatingBubble`, `_startSendTranslateStatusPoll`,
  `_applyModelUI`, `_saveConvToolState`, `_restoreConvToolState`.
- All 86 backend tests pass (frontend_api_isolation + api_response +
  request_parser + json_store).
- All 10 translate migration tests + 14 paper migration tests pass.

## Boundary lessons

- The `let _sendTranslateStatusTimer = null` declaration AND its
  preceding 5-line `// ─── Poll loop for...` doc-comment block must
  travel together — splitting between them leaves an orphan comment
  in the previous file.
- main.js's L662 closes `_resetToolsToDefaults`. That's where the
  "tool UI helpers" cluster ends and "conv lifecycle" begins.
- main.js's boot IIFE starts at L4800 (`(function init() {`) and
  references `_loadServerConfigAndPopulate`, `_applyModelUI`,
  `applyTheme`, `initActiveTasks`, `_ensureNewest`, `_initFolderTabs`,
  `handleKeyDown`, etc. — all of which now live in extracted files.
  The bundler's load order makes them all available by the time the
  IIFE runs.

## Next decompositions

| File | Current LOC | Pattern |
|---|---|---|
| `static/js/settings.js` | 4892 | settings UI tabs (provider, oauth, mcp, agents, mt-test, …) |
| `static/js/core.js` | 3919 | network, state, markdown, folders, IDB cache |

