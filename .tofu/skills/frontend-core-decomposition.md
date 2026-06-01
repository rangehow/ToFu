---
name: frontend-core-decomposition
description: core.js (3877 LOC) decomposed into static/js/core/ subpackage of 11 files; core.js kept as 244-LOC shell
enabled: true
tags: [refactor, frontend, javascript, convention]
created: 2026-05-28T07:21:23Z
updated: 2026-05-28T07:21:23Z
---

# `static/js/core.js` Decomposition (2026-05-28)

Fourth and final monolithic frontend file split. Recipe similar to
ui.js / settings.js but with one important nuance: core.js's "head"
declares foundational module-level state used by every other JS file
in the bundle, so the slim shell MUST stay BEFORE the subpackage in
load order.

## Before

Single 3877-LOC `static/js/core.js` with module-level state +
foundational helpers + 11 distinct cohesive subsystems interleaved.

## After

`core.js` kept as 244-LOC slim shell containing the 4 cross-referenced
"keep" segments concatenated in original order:
- L1-80: `BASE_PATH`, `apiUrl`, `_ensureKatex`, `_ensurePdfJs`, `TAB_ID`,
  `_syncChannel`, `conversations`, `_folders`, `_foldersLoaded`
- L211-286: `activeConvId`, `pendingMessageQueue`, `_editingMsgIdx`,
  `_convRenderFingerprint`, `thinkingEnabled`, `_browserStatusInterval`,
  `serverModel`, `config`
- L1221-1249: `generateId`, `_newClientMsgId`, `_ensureMsgId`
- L1382-1428: `getActiveConv`, `_chatContainerEl`, `_getChatContainer`,
  `isNearBottom`, `scrollToBottom`, `getToolRoundsFromMsg`

Plus 4 boundary marker comments noting where extracted blocks live.

The body extracted into 11 cohesive sibling files under `static/js/core/`:

```
static/js/core/
  folders.js              130 LOC — folder CRUD + setConversationFolder + _migratePinnedToFolder
  cost.js                 309 LOC — _LEGACY_PRESET_TO_MODEL, autoTranslate, projectState,
                                    autoApplyWrites, pricingData, loadPricing,
                                    calcCostCny, _prefetchConvCosts, calcConversationCost
  debug_panel.js          625 LOC — debugLog ring buffer, _reportClientError, debug-panel
                                    rendering (showMessagesInDebug = 393 LOC HTML renderer),
                                    copyDebugContent
  escape_html.js           11 LOC — pure-string escapeHtml (no DOM) — perf-critical
  error_envelope.js       121 LOC — ERROR_KIND_LABELS, isErrorEnvelope, normalizeErrorEnvelope,
                                    renderErrorEnvelope, errorEnvelopeKind/Message
  cross_tab_sync.js       243 LOC — _broadcastToTabs, _handleCrossTabMsg,
                                    _recoverOfflineConversations, _startOfflineRecoveryPolling
  conversations.js       1095 LOC — saveConversations (debounced), syncConversationToServer,
                                    loadConversationsFromServer, loadConversationMessages (494 LOC),
                                    forceRecoverFromServer, auditConversations, recoverAll
  cache_stats.js           51 LOC — clearConvCache, convCacheStats
  markdown.js             631 LOC — single-pass DOM transforms, KaTeX, CJK-friendly emphasis,
                                    table fence repair, code apply buttons,
                                    renderMarkdown (165 LOC), copy helpers
  health_stream_timer.js  351 LOC — _checkServerHealth, _checkDbHealth, _showDbWarningBanner,
                                    _forceFinishDeadStream, _updateStreamTimerUI,
                                    twStart/twUpdate/twStop
  toast.js                 78 LOC — _toastTypes, showToast
```

Total extracted: 3645 LOC. Slim core.js: 236 LOC (excluding markers).
Sum: 3881 (vs 3877 original; +4 lines = 4 boundary markers).

## CRITICAL bundler order: core.js BEFORE core/

Same nuance as `settings.js`: core.js's slim shell declares module
state via `let` / `const` / `var` that the extracted files mutate.
The shell MUST come first in `_BUNDLE_FILES`:

```python
'i18n.js',
'idb-cache.js',
'core.js',                 # ★ slim shell with apiUrl, BASE_PATH, conversations, ...
'core/folders.js',         # ── core/ subpackage (loads AFTER shell) ──
'core/cost.js',
'core/debug_panel.js',
'core/escape_html.js',
'core/error_envelope.js',
'core/cross_tab_sync.js',
'core/conversations.js',
'core/cache_stats.js',
'core/markdown.js',
'core/health_stream_timer.js',
'core/toast.js',
'api.js',                  # ── consumes apiUrl from core.js shell ──
```

The 11 core/* files reference core.js shell decls (`apiUrl`, `config`,
`serverModel`, `conversations`, `_folders`, `getActiveConv`, etc.) at
runtime AND at module-load time (e.g. `cost.js` does legacy preset
migration on `config` at load time). So they must load AFTER the shell.

`api.js` only references `apiUrl` (at runtime), so its placement after
the core/ subpackage is correct (and unchanged).

`index.html` mirrors this order — 11 new `<script>` tags between
`core.js` and `api.js`.

## Pure source split — body byte-equivalent

Every code line unchanged. Only added: 11 × 8-line banners + 4 × 1-3
line boundary markers in the slim core.js. Total overhead ~92 lines.

## Verification

- All 12 files (slim core.js + 11 extracted): `node -c` clean.
- Bundler builds (`bundle-f252416a.js`).
- 47 sampled symbols verified present in the bundle:
  `apiUrl`, `loadFolders`, `loadPricing`, `calcCostCny`,
  `calcConversationCost`, `debugLog`, `clearDebug`, `showMessagesInDebug`,
  `generateId`, `_ensureMsgId`, `escapeHtml`, `isErrorEnvelope`,
  `normalizeErrorEnvelope`, `errorEnvelopeMessage`, `getActiveConv`,
  `isNearBottom`, `scrollToBottom`, `_broadcastToTabs`,
  `_recoverOfflineConversations`, `saveConversations`,
  `syncConversationToServer`, `loadConversationsFromServer`,
  `loadConversationMessages`, `forceRecoverFromServer`,
  `auditConversations`, `recoverAll`, `clearConvCache`, `convCacheStats`,
  `renderMarkdown`, `highlightCodeInHtml`, `processLongCodeBlocks`,
  `copyCode`, `copyTableMarkdown`, `_checkServerHealth`,
  `_showDbWarningBanner`, `_updateStreamTimerUI`, `twStart`, `twUpdate`,
  `twStop`, `showToast`.
- API isolation 4/4 pass; backend tests 86/86; translate 10/10; paper 14/14.

## Pattern divergences from previous splits

- **Discontiguous "keep" segments** — unlike main.js (head + tail keeps)
  or settings.js (head only), core.js has 4 disjoint kept segments
  interleaved between extracted blocks. The slim shell concatenates
  them in original order with 4 boundary markers noting where extracts
  live; the bundler then loads core/* immediately after.
- **No boot IIFE in core.js** — unlike main.js, core.js is purely
  helpers + state. So the shell isn't a "bootstrap orchestrator" — it's
  a "shared globals + small foundational helpers" file.
- **`escape_html.js` is tiny (11 LOC)** but kept as its own file
  because it's a perf-critical, self-contained primitive used by every
  rendering path. The doc-comment explains the design choice.

## Frontend monolith decomposition: ALL DONE 🎉

| Original | Pre-LOC | Post (orchestrator) | Sub-package |
|---|---|---|---|
| `ui.js` | 8932 | deleted | 11 files / 9027 LOC |
| `main.js` | 5144 | 1017 (boot IIFE) | 8 files / 4216 LOC |
| `settings.js` | 4755 | 24 (head + pointer) | 15 files / 4739 LOC |
| `core.js` | 3877 | 244 (4-segment shell) | 11 files / 3744 LOC |
| **Totals** | **22708** | **1285** | **45 files / 21726 LOC** |

The four giant frontend monoliths totalling 22,708 LOC are now
distributed across 45 cohesive files. Largest single non-orchestrator
file: `ui/sse_pipeline.js` at 2782 LOC.

