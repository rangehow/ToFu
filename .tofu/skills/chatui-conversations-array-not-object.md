---
name: chatui-conversations-array-not-object
description: Bug pattern: chatui's `conversations` variable is an Array, not an Object/Map — must use .find(c => c.id === id) for lookup, never bracket access conversations[id]
enabled: true
tags: [javascript, debugging, frontend, endpoint, array-vs-object]
created: 2026-03-16T22:48:10Z
updated: 2026-03-16T22:48:10Z
---

# conversations is an Array, not a Map

In `chatui`, the global `conversations` variable (defined in `static/js/core.js`) is an **Array of objects**, where each element has an `.id` string property.

## Wrong Pattern
```javascript
// ❌ BUG: conversations is an Array, bracket access with string returns undefined
const conv = conversations[convId];
```

## Correct Pattern
```javascript
// ✅ CORRECT: use .find() to locate by id
const conv = conversations.find(c => c.id === convId);
```

## Why This Matters
- `convId` is a string like `"mmteq7sn9u83p5"`, not a numeric index
- Array bracket access with a non-numeric string returns `undefined`
- This silently breaks all code inside `if (conv) { ... }` blocks
- The bug is hard to detect because no error is thrown — it just silently skips

## Where This Was Found
- `static/js/ui.js` in the `endpoint_user_inject` SSE event handler
- Caused the entire multi-turn endpoint iteration rendering to be dead code
- All other places in the codebase correctly used `.find()`

