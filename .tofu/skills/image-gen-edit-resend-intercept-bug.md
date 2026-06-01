---
name: image-gen-edit-resend-intercept-bug
description: Bug fix: saveEditAndResend in image-gen mode ran via /api/chat/regenerate (text task) — added intercept mirroring regenerateFromUser
enabled: true
tags: [javascript, frontend, bug-fix, image-gen, edit, regenerate, routing, ui.js]
created: 2026-04-28T13:26:04Z
updated: 2026-04-28T13:26:04Z
---

# Image Gen Mode: Edit/Resend Routing Bug (2026-04-28)

## Symptom
Editing a message (pencil → Save & Resend) or regenerating from user message in
image generation mode invoked the normal text chat flow — request went to
`/api/chat/regenerate` and was executed as a chat task instead of calling the
image generation API.

## Root Cause
Two entry points for re-running a user message:
- `regenerateFromUser(idx)` in `static/js/main.js` — **already had** an
  image-gen intercept (`if _isIgConv || _isIgMsg → generateImageDirect()`).
- `saveEditAndResend(idx)` in `static/js/ui.js` — **missing** the equivalent
  intercept. It fell through to `/api/chat/regenerate`.

## Fix
Added a mirror intercept in `saveEditAndResend` before the VLM-wait step.
When conv is in image-gen mode OR the edited msg is an image-gen msg
(`msg._isImageGen` / content starts with `🎨 `):
1. Apply edits locally, truncate to BEFORE this msg (generateImageDirect
   re-pushes it), save, re-render.
2. Seed `#userInput` textarea with the edited prompt.
3. Seed `pendingImages` from edited images so they become source images for
   editing-style generation.
4. Call `_applyImageGenUI(true)` if not already in mode, then `generateImageDirect()`.

## Detection
Scan `saveEditAndResend` and `regenerateFromUser` for `_isImageGen` /
`imageGenMode` intercepts — they must be kept in sync. Similar pattern applies
to any other "conversation mode" (paper mode, endpoint mode, etc.).

## Key Files
- `static/js/ui.js` → `saveEditAndResend(idx)` — intercept added
- `static/js/main.js` → `regenerateFromUser(idx)` — reference intercept
- `static/js/image-gen.js` → `generateImageDirect()` — target handler

## Note
`bundle-*.js` is auto-regenerated from source by `lib/js_bundler.py` at server
startup — don't edit it manually.

