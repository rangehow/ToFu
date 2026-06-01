---
name: stale-partial-translation-mid-stream-bug
description: Bug fix: translation triggered mid-stream on partial content produces stale translatedContent that blocks re-translation when full content arrives in finishStream
enabled: true
tags: [translation, bug-fix, streaming, stale-detection]
created: 2026-04-01T13:41:47Z
updated: 2026-04-01T13:41:47Z
---

# Stale Partial Translation Bug (Mid-Stream)

## Root Cause
When a multi-round tool-calling task runs for several minutes, the frontend may trigger translation on **partial content** (e.g. 465 chars) from intermediate rounds. When the task completes with the full response (e.g. 10,127 chars), the stale 153-char translation blocks re-translation via TWO guards:

1. **Frontend `finishStream`**: `!lastMsg._translateTaskId` guard runs BEFORE stale detection → entire auto-translate block is skipped because `_translateTaskId` was already set during mid-stream translation
2. **Server-side `_maybe_auto_translate_assistant`**: `if existing_tc and len(existing_tc) > 0: return` → any non-empty translation is considered complete

## Fix Applied (3 locations)

### 1. `static/js/ui.js` — `finishStream()`
Move stale detection BEFORE the `_translateTaskId` guard. If `translatedContent.length < content.length * 0.15` AND `content.length > 500`, clear all translate markers and allow re-translation.

### 2. `static/js/translation.js` — `_resumePendingTranslations()`  
Add stale detection in Phase 0 before the `msg.translatedContent || msg._translateTaskId` break condition.

### 3. `lib/tasks_pkg/manager.py` — `_maybe_auto_translate_assistant()`
Instead of just checking `len(existing_tc) > 0`, compare `tc_len < content_len * 0.15` to detect stale partial translations.

## Detection Threshold
`translatedContent.length < content.length * 0.15` — normal EN→CN compression is 40-60%, so 15% clearly indicates partial content was translated. Only applies to responses > 500 chars to avoid false positives on short messages.

## Diagnostic Query
```sql
-- Find stale partial translations in DB
SELECT id, i, tc_len, content_len, ratio
FROM (
  SELECT c.id, 
    ordinality - 1 as i,
    length(msg->>'translatedContent') as tc_len,
    length(msg->>'content') as content_len,
    length(msg->>'translatedContent')::float / nullif(length(msg->>'content'), 0) as ratio
  FROM conversations c,
    jsonb_array_elements(messages::jsonb) WITH ORDINALITY AS t(msg, ordinality)
  WHERE msg->>'role' = 'assistant'
    AND msg->>'translatedContent' IS NOT NULL
    AND length(msg->>'content') > 500
) sub
WHERE ratio < 0.15;
```

