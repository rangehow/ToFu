---
name: chatui-edit-mode-shared-state-unification
description: Edit mode shares pendingImages/pendingPdfTexts/renderImagePreviews with main input via backup/restore pattern; translation always checks for Chinese (not just when originalContent exists); no duplicate arrays/handlers/rendering/upload buttons
enabled: true
tags: [javascript, frontend, edit-mode, shared-state, architecture, attachments, translation]
created: 2026-03-27T05:48:45Z
updated: 2026-03-27T08:08:05Z
---

# ChatUI Edit Mode — Shared State Unification

## Architecture
When editing a message, the edit box **reuses** the main input's shared state instead of maintaining parallel arrays:

### Shared State
- `pendingImages` / `pendingPdfTexts` — **single source of truth** for attachments
- `renderImagePreviews()` — **single rendering function** for `#imagePreviews` container (in the fixed input bar, always visible)
- Drag-and-drop / paste handlers — go through the **same path** as main input
- `detectLogNoise` / `showLogCleanBanner` / `applyLogClean` — shared via `_getActiveTextarea()`
- **No upload buttons** in edit area — just like the main input, files are added via drag-and-drop or paste only

### Backup/Restore Pattern
When `startEditMessage(idx)` is called:
1. **Backup** current main input state (`pendingImages`, `pendingPdfTexts`, `#userInput.value`) into `_editBackupImages`, `_editBackupPdfs`, `_editBackupInput`
2. **Load** message's existing attachments into shared state: `pendingImages = [...msg.images]`
3. **Render** via shared `renderImagePreviews()` — same chips in the input bar's `#imagePreviews`

When save or cancel:
1. **Collect** from shared state: `msg.images = [...pendingImages]`
2. **Restore** main input from backup via `_restoreInputFromBackup()`

### Translation Alignment (CRITICAL)
- `saveEditOnly`: **Always** checks `_convAutoTranslate` + Chinese detection, then fire-and-forget translate task. Sets `msg.content = t` first, then `msg.originalContent = t` if Chinese detected. Previously had a bug where it only triggered translation when `msg.originalContent` already existed.
- `saveEditAndResend`: uses `conv.autoTranslate` (per-conv), **BLOCKING** poll with translate indicator (matches `sendMessage` exactly)
- Never reads global `autoTranslate` — always reads `conv.autoTranslate`

### Key Files
- `static/js/ui.js` — `startEditMessage`, `cancelEditMessage`, `saveEditOnly`, `saveEditAndResend`, `_restoreInputFromBackup`
- `static/js/main.js` — `renderImagePreviews`, `processImageFile`, `parsePdfToServer`, drag-and-drop handler, `_getActiveTextarea`

### What Was Removed
- `_editNewImages` / `_editNewPdfs` arrays (parallel state)
- `_handleEditImageAdd` / `_handleEditPdfAdd` (duplicate handlers)
- `_updateEditAttachSummary` (duplicate rendering)
- `edit-attach-summary` / `edit-chips` / `edit-attachments-row` HTML/CSS
- Upload ＋🖼️ / ＋📄 buttons (no upload buttons — matches main input)

### Bug Fixed (2026-03-28)
`saveEditOnly` only translated when `msg.originalContent` existed → editing a never-translated message with autoTranslate=ON skipped translation entirely. Fix: unconditionally set `msg.content = t`, then check for Chinese text regardless of prior `originalContent` state.

