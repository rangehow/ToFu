---
name: frontend-ui-decomposition
description: ui.js (8932 LOC) decomposed into static/js/ui/ subpackage of 11 cohesive files; byte-equivalent split
enabled: true
tags: [refactor, frontend, javascript, convention]
created: 2026-05-28T06:47:24Z
updated: 2026-05-28T06:47:24Z
---

# `static/js/ui.js` Decomposition (2026-05-28)

First giant frontend file split. The pattern in
`js-bundler-extraction-pattern` applies — pure file concatenation, all
symbols share `window` scope, no imports / exports needed.

## Before

Single 8932-LOC `static/js/ui.js` mixing 145 top-level definitions:
- Sidebar conversation list + folder tabs + search
- Streaming-message rendering (autopilot VU events, surgical DOM)
- `renderChat` + `renderMessage` (the message-rendering core)
- Selection popup + reply quotes + conv references
- `renderFinishInfo` (usage/cost) + file-changes bar
- Tool-round rendering (web_search, fetch_url, code_exec, project,
  browser, image_gen, swarm) — biggest cohesive block at 1340 LOC
- Per-message actions (copy / delete / translate)
- Edit-message flow (edit-and-resend, edit-only)
- Turn navigation
- Streaming UI zones + `updateStreamingUI` + `_syncToolRoundsDOM` +
  swarm panels + `finishStream`
- SSE chat-stream pipeline (`connectToTask` / `_trySSE` /
  `_pollFallback`) — biggest single block at 2772 LOC
- `updateSendButton`

## After

```
static/js/ui/
  conversation_list.js    653 LOC — sidebar conv list, folder tabs, search
  streaming_render.js     551 LOC — autopilot VU events, surgical DOM, lazy load
  chat_render.js          792 LOC — renderChat + renderMessage + msg fingerprint
  popups.js               202 LOC — selection popup, reply quotes, conv refs
  finish_info.js          418 LOC — renderFinishInfo + file-changes bar
  tool_rounds.js         1351 LOC — _isRound* + _renderUnifiedToolLine + renderToolRoundsHTML
  message_actions.js      325 LOC — copy / delete / translate
  edit_message.js         475 LOC — saveEditAndResend, saveEditOnly
  turn_nav.js             217 LOC — buildTurnNav, scrollToTurn, updateActiveTurn
  streaming_ui.js        1261 LOC — _ensureStreamZones, updateStreamingUI, _syncToolRoundsDOM, finishStream, _autoTranslateHumanGuidance
  sse_pipeline.js        2782 LOC — connectToTask, _trySSE, _pollFallback, updateSendButton
```

Total: 9027 LOC (vs 8932 — increase from 11 × 10-line per-file banners).

## Pure source split — byte-identical

Verified at split time: concatenating the 11 new files (after stripping
their banners) produces a byte-identical body to the original
`ui.js[L16-8932]`. **Zero semantic change.**

## Key file boundaries chosen

Each split point lands exactly at:
- A `function name() {` declaration (or its preceding `/** */` doc), OR
- A top-level `let/const NAME = ` statement, OR
- A section banner comment introducing the next block.

This means each new file starts at a clean unit boundary and ends with
a closing `}` (or trailing comment).

## Bundler wiring (already done)

`lib/js_bundler.py::_BUNDLE_FILES` replaces the single `'ui.js'` entry
with the 11 ordered subdir paths:

```python
'artifacts.js',
# ── ui/ subpackage (split 2026-05-28 from monolithic ui.js) ──
'ui/conversation_list.js',
'ui/streaming_render.js',
'ui/chat_render.js',
'ui/popups.js',
'ui/finish_info.js',
'ui/tool_rounds.js',
'ui/message_actions.js',
'ui/edit_message.js',
'ui/turn_nav.js',
'ui/streaming_ui.js',
'ui/sse_pipeline.js',
'conv_view.js',  # consumes renderMessage etc. → must come AFTER ui/
```

`index.html` also updated — the single `<script defer src="static/js/ui.js">`
tag becomes 11 tags, one per file (the bundler is the production path;
the `<script>` tags are only used in the dev fallback when bundling fails).

## Old `ui.js` deleted

Once the 11 subpackage files passed the byte-equivalence check, the old
file was removed. Bundle hash stays effectively the same (same content,
just split across more source files; the per-file `// ═══ name ═══`
banners change the hash slightly).

## Regression-test wiring

`tests/test_frontend_api_isolation.py::_scan_all` was updated to walk
the entire `static/js/` tree (was: `os.listdir`-only, missed subdirs).
BASELINE keys now use posix paths like `'ui/sse_pipeline.js'` so any
future legacy `fetch('/api/...')` leak in a new file under `ui/` is
caught immediately. The end state stays `BASELINE = {}` — no migration
needed for this PR (the old ui.js had already been API-isolation-clean).

## Verification

- All 11 new files parse cleanly (`node -c <file>`).
- Bundler builds successfully (`bundle-726f6893.js`); 18 sampled
  ui.js symbols present in the output (`renderChat`, `renderMessage`,
  `renderConversationList`, `renderToolRoundsHTML`, `connectToTask`,
  `_trySSE`, `_pollFallback`, `updateStreamingUI`, `finishStream`,
  `updateSendButton`, `renderFinishInfo`, `renderFolderTabs`,
  `deleteTurn`, `translateMessage`, `saveEditAndResend`, `buildTurnNav`,
  `scrollToTurn`, `_initSelectionPopup`).
- `tests/test_frontend_api_isolation.py` 4/4 pass after the
  subdir-walking update.
- 86/86 backend tests pass.
- 10/10 translate migration tests + 14/14 paper migration tests pass.

## Next decompositions (in suggested order)

| File | Current LOC | Pattern |
|---|---|---|
| `static/js/main.js` | 5172 | bootstrap + chat send/abort + queue + init paths |
| `static/js/settings.js` | 4892 | settings UI tabs (provider, oauth, mcp, agents, mt-test, …) |
| `static/js/core.js` | 3919 | network + state + markdown + folders + IDB cache |

Each can be split following this pattern: identify cohesive function
clusters, slice at function boundaries, verify byte-identity post-split,
add to bundler / index.html, run isolation test.

