---
name: chatui-frontend-modular-architecture
description: Frontend JS module architecture: 15 files (main.js split into 8 modules + branch.js from ui.js), load order in index.html, cross-script var requirement for shared variables, function hoisting for forward references
enabled: true
tags: [javascript, frontend, architecture, modular, refactoring, main.js, cross-script, branch.js, ui.js]
created: 2026-03-31T15:07:53Z
updated: 2026-03-31T15:26:22Z
---

# ChatUI Frontend Modular JS Architecture

## File Structure (15 files)

```
static/js/
  idb-cache.js       349 lines  — IndexedDB conversation cache (IIFE, ConvCache)
  core.js          2,269 lines  — Shared state, conversations, pricing, apiUrl
  export-images.js   583 lines  — Export conversation as image
  branch.js        1,225 lines  — Branch conversations (extracted from ui.js)
  ui.js            5,297 lines  — Rendering, SSE streaming, DOM (was 6,518)
  log-clean.js       739 lines  — Log noise detection & 6-pass cleaning pipeline
  translation.js     255 lines  — Async translation polling & API calls
  upload.js          627 lines  — Image/PDF/doc upload, preview, VLM
  image-gen.js       754 lines  — Creative image generation mode
  project.js       1,120 lines  — Project Co-Pilot, folder browser, apply code
  skills.js          304 lines  — Skills modal & CRUD
  scheduler.js       156 lines  — Proactive agent scheduler panel
  myday.js         1,101 lines  — MyDay daily task report + cost dashboard
  settings.js      1,591 lines  — Settings modal
  main.js          3,460 lines  — Chat core, toolbar, conversation mgmt, init (was 8,527)
```

## Load Order (in index.html)

```html
<!-- Foundation -->
<script src="static/js/idb-cache.js"></script>
<script src="static/js/core.js"></script>
<script src="static/js/export-images.js"></script>
<script src="static/js/branch.js"></script>     <!-- BEFORE ui.js (ui references _activeBranch etc.) -->
<script src="static/js/ui.js"></script>

<!-- Feature modules (order-independent, all read from core/ui globals) -->
<script src="static/js/log-clean.js"></script>
<script src="static/js/translation.js"></script>
<script src="static/js/upload.js"></script>
<script src="static/js/image-gen.js"></script>
<script src="static/js/project.js"></script>
<script src="static/js/skills.js"></script>
<script src="static/js/scheduler.js"></script>
<script src="static/js/myday.js"></script>
<script src="static/js/settings.js"></script>

<!-- Orchestrator (loads last, depends on everything) -->
<script src="static/js/main.js"></script>
```

## Cross-Script Variable Rules

1. **`function` declarations** → automatically global (hoisted), accessible from all scripts
2. **`let`/`const` at top-level** → shared across `<script>` tags via global lexical environment
3. **`var` at top-level** → global AND on `window` object; use for variables that MUST be cross-file mutable (e.g., `pendingPdfTexts`, `_pendingLogClean`, `_hiddenIgModels`)
4. **Forward references OK** — functions defined in later-loading scripts can be called from earlier scripts' event handlers, as long as they're called at user-interaction time (not at script-load time)

## Key Cross-File Dependencies

| Variable | Defined in | Used by | Type |
|---|---|---|---|
| `conversations` | core.js | everywhere | `let` |
| `activeConvId` | core.js | everywhere | `let` |
| `pendingImages` | core.js | upload.js, main.js | `let` |
| `pendingPdfTexts` | upload.js | main.js, ui.js | `var` |
| `_pendingLogClean` | log-clean.js | main.js, ui.js | `var` |
| `_hiddenIgModels` | main.js | image-gen.js, settings.js | `var` |
| `_activeBranch` | branch.js | ui.js | `let` |
| `_branchStreams` | branch.js | ui.js | `const` |

## When Adding New Features

- If the feature is self-contained (≥100 lines, distinct domain), create a new `.js` file
- Add `<script>` tag in index.html between feature modules and main.js
- Use `var` for any state variable that other files need to read/write
- Never execute calls to functions from later-loading scripts at load time (only in event handlers)

