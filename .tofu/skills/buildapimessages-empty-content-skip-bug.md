---
name: buildApiMessages-empty-content-skip-bug
description: Bug fix: buildApiMessages skips assistant messages when content is empty string (falsy), breaking user↔assistant alternation and causing consecutive USER messages in context
enabled: true
tags: [javascript, frontend, bug-fix, buildApiMessages, empty-string, falsy, user-assistant-alternation]
created: 2026-03-20T05:14:33Z
updated: 2026-03-20T05:14:33Z
---

# buildApiMessages Empty Content Skip Bug

## Problem
In `static/js/main.js` `buildApiMessages()`, the assistant message branch used:
```javascript
} else if (msg.role === "assistant" && msg.content) {
```

When `msg.content` is `""` (empty string), it's **falsy** in JavaScript, so the entire assistant message is **skipped**. This causes:
- **Consecutive USER messages** in the API messages array
- **Lost tool history** (searchRounds/toolSummary not processed)
- **Broken context** for subsequent LLM calls

## When Does content Stay Empty?
- **Swarm mode**: LLM calls `spawn_agents` tool → no text delta → content stays `""`
- **Tool-only turns**: LLM makes tool calls but streams no text before `done`
- **Any turn** where `assistantMsg.content` is initialized as `""` and never updated

## Fix
Remove the `msg.content` truthy check. Always process assistant messages. Use fallback chain for content:
```javascript
const assistantContent = msg.content || toolCtx || "(tool-use turn)";
messages.push({ role: "assistant", content: assistantContent });
```

Priority: actual content > tool summary > placeholder string.

## Key Lesson
JavaScript falsy check (`if (str)`) fails for empty strings. For checking "does this message have content?", use explicit length check (`msg.content.length > 0`) or always handle the empty case.

