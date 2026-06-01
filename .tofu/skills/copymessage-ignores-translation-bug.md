---
name: copyMessage-ignores-translation-bug
description: Bug fix: copyMessage() always copied msg.content (English original) even when translation was displayed — now copies translatedContent or originalContent based on what's shown on screen
enabled: true
tags: [javascript, bug-fix, translation, copy, clipboard, ui]
created: 2026-03-23T12:14:38Z
updated: 2026-03-23T12:14:38Z
---

# copyMessage() Ignores Translation — Copies English Instead of Displayed Chinese

## The Bug
`copyMessage()` in `static/js/ui.js` hardcoded `msg.content` for clipboard:
```js
navigator.clipboard.writeText(msg.content || "")
```

But the rendering logic uses `msg.translatedContent` when translation is active:
```js
const showTrans = !isUser && msg.translatedContent && msg._showingTranslation !== false;
```

Result: user sees Chinese, copies English.

## The Fix
Copy what's displayed:
```js
const isUser = msg.role === "user";
const showTrans = !isUser && msg.translatedContent && msg._showingTranslation !== false;
let textToCopy;
if (showTrans) {
    textToCopy = msg.translatedContent;       // Assistant: Chinese translation
} else if (isUser && msg.originalContent) {
    textToCopy = msg.originalContent;          // User: original Chinese input
} else {
    textToCopy = msg.content || "";            // Default
}
```

Note: `copyBilingualOriginal()` is a separate function that intentionally copies the original English — leave it unchanged.

## Key Pattern
Any function that reads message content for output (copy, export, share) must mirror the rendering logic's translation-awareness. Check for:
- `msg.translatedContent` + `msg._showingTranslation` for assistant messages
- `msg.originalContent` for user messages with auto-translated input

