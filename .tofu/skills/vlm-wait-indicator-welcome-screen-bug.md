---
name: vlm-wait-indicator-welcome-screen-bug
description: main.js sendMessage: render user bubble & remove welcome BEFORE awaiting VLM parse, else indicator appears under welcome card
enabled: true
tags: [frontend, ui, pdf, vlm]
created: 2026-05-06T10:34:50Z
updated: 2026-05-06T10:34:50Z
---

# sendMessage VLM-wait must run AFTER user bubble is rendered

## Symptom
On a fresh conversation, after uploading PDFs and pressing Send, the
"VLM parsing: file.pdf 30/64 …" indicator appears INSIDE the welcome
screen area (Tofu logo / suggestion chips still visible above it),
making it look like the welcome page itself is parsing. The user
bubble doesn't show up until VLM finishes.

## Root cause
`static/js/main.js` `sendMessage()` previously did:

1. `await _waitForVlmParsing(_tempUserMsg, convId, -1)`  — this
   appends `#vlm-wait-indicator` into `#chatInner`
2. THEN remove `#welcome` and render the user bubble.

So during step 1 the indicator is mounted while the welcome card is
still present. Result: spinner under welcome page, no user bubble.

## Fix (2026-05-06)
Reorder in `sendMessage()`: build `userMsg`, push onto `conv.messages`,
remove `#welcome`, insert user-bubble HTML into `#chatInner`, THEN call
`_waitForVlmParsing(userMsg, convId, userMsgIdx)`. The indicator now
sits below a real user bubble in a normal chat view.

The edit-mode equivalent in `static/js/ui.js` (`saveEditAndResend`) is
fine — it always runs in an existing conversation, no welcome screen.

## Related — actual VLM slowness
Separate issue: 8.9 MB / 64-page PDFs take 100s+ in
`pymupdf4llm.to_markdown(pages=[i], table_strategy='lines')` per-page
loop. `routes/upload.py /api/pdf/parse` calls `parse_pdf` without a
`progress_callback`, so the upload progress bar sticks at 5% the whole
time. Paper mode in `routes/paper.py` already has the SSE progress
plumbing (`parse_progress` events) — same pattern can be applied to
`/api/pdf/parse`. See memory `paper-mode-pdf-parse-progress`.

