---
name: mid-stream-translation-partial-content-bug
description: Bug fix: _resumePendingTranslations fires during active streaming (page reload/conv switch), translating partial content → stale incomplete translation that finishStream never overwrites; guard with activeStreams check + stale detection at <20% ratio
enabled: true
tags: [javascript, translation, streaming, bug-fix, race-condition, partial-content, finishStream]
created: 2026-03-24T15:09:57Z
updated: 2026-03-24T15:09:57Z
---

## Bug: Mid-Stream Translation of Partial Content

### Root Cause
`_resumePendingTranslations()` Phase 0 can fire during an active stream when:
1. User switches to a conv that needs `_needsLoad` while it's streaming
2. Tab visibility change triggers `_resumePendingTranslations`
3. Boot/page reload calls `_resumePendingTranslations` for the active conv

Phase 0 sees the last assistant message has partial content (e.g. 174 chars after R2 of a 9-round tool-use sequence), starts translation on that partial content. Translation completes quickly (45 chars). When `finishStream()` runs later with full content (5401 chars), it sees `lastMsg.translatedContent` is already set and **skips re-translation**.

### Timeline (real case: conv mn4q5ih41cl6fi)
- 22:43:38 — R1: 96 chars streamed
- 22:43:55 — R2: +80 chars = ~176 total  
- **22:44:04 — _resumePendingTranslations fires (page reload), starts translating 174 chars**
- 22:44:07 — Translation done: 174→45 chars
- 22:45:32 — R9: stream finishes with 5401 chars total
- finishStream → `!lastMsg.translatedContent` → FALSE → skips translation
- Result: 5401 chars English with only 45 chars Chinese translation (0.8%)

### Fix (2 layers)

**Layer 1 — Prevention:** Guard `_resumePendingTranslations` with `activeStreams.has(convId)` check at the top:
```javascript
if (activeStreams.has(convId)) {
    console.log(`[TranslateTask] ⏭ Skipping — stream active`);
    return;
}
```
Also guard the `_needsLoad` `.then()` callback:
```javascript
if (!activeStreams.has(id)) _resumePendingTranslations(id);
```

**Layer 2 — Safety net in finishStream:** Detect stale partial translations by ratio:
```javascript
const hasStaleTranslation = lastMsg.translatedContent &&
    lastMsg.translatedContent.length < lastMsg.content.length * 0.2;
if (hasStaleTranslation) {
    delete lastMsg.translatedContent;
    delete lastMsg._translatedCache;
    delete lastMsg._translateDone;
}
```

### Files Modified
- `static/js/main.js` — `_resumePendingTranslations()` + `loadConversation()` _needsLoad path
- `static/js/ui.js` — `finishStream()` auto-translate section

