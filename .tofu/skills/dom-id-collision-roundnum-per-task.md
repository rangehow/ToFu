---
name: dom-id-collision-roundnum-per-task
description: Bug pattern: DOM element IDs based on roundNum collide across messages because roundNum restarts per task — use relative DOM traversal (this.parentElement) instead of getElementById
enabled: true
tags: [frontend, bug, dom, tool-rendering]
created: 2026-04-08T02:40:52Z
updated: 2026-04-08T02:40:52Z
---

# DOM ID Collision: roundNum is Per-Task, Not Per-Conversation

## The Bug
`roundNum` in `searchRounds` restarts from 1 for each new task (each assistant message).
When a conversation has multiple assistant messages with tool usage, DOM element IDs
like `id="pcmd-r${roundNum}-wrap"` collide across messages.

`document.getElementById()` returns the **first** matching element in document order,
so clicking "Show output" on a later message's command would toggle the element from
an earlier message instead — appearing empty or non-functional.

## The Fix
Use **relative DOM traversal** instead of global ID lookups:
```javascript
// ❌ BAD — global ID collision
var w = document.getElementById('pcmd-r5-wrap');

// ✅ GOOD — relative to clicked element
var w = this.parentElement;  // toggle is direct child of wrap
```

## When This Applies
Any inline `onclick` handler in tool round rendering that uses `document.getElementById`
with a roundNum-based ID. Always prefer `this.closest()`, `this.parentElement`, or
`this.nextElementSibling` for DOM-local operations.

## Verified Data
In production, conversations with 9+ assistant messages had 39+ roundNum collisions
(round numbers 1-52 repeated across messages).

