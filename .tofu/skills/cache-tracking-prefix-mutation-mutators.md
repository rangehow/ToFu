---
name: cache-tracking-prefix-mutation-mutators
description: Functions mutating cache prefix MUST call notify_compaction(conv_id) to suppress false positives
enabled: true
tags: [cache_tracking, logging, convention]
created: 2026-05-13T14:50:24Z
updated: 2026-05-13T14:50:24Z
---

# Cache-tracking prefix mutation discipline

`lib/tasks_pkg/cache_tracking.py:detect_cache_break` raises a
`PREFIX MUTATION DETECTED` WARNING (logged to error.log) whenever the
hash of `messages[0:N-2]` changes between rounds without a corresponding
`notify_compaction(conv_id)` call. This both pollutes error.log AND
indicates a real prompt-cache invalidation cost.

**Any code that legitimately mutates a message inside the cache prefix
MUST call `notify_compaction(conv_id)` immediately after the mutation.**

Known prefix-mutating call sites (all live in the round prologue, BEFORE
`detect_cache_break` runs at end of round):

| Function | Location | Notify? |
|---|---|---|
| `inject_relevant_memories` (memory prefetch) | `lib/memory/prefetch.py:570` | yes (added) |
| `inject_attachments` (per-turn reminders) | `lib/tasks_pkg/attachments.py:198` | yes (added 2026-05-13) |
| `run_compaction_pipeline` (L1/L2 compact) | `lib/tasks_pkg/compaction.py:3344` | yes |

The bug pattern: mutator appends to "the last user message" walking
backward from the end. After the first tool round, the original user
turn is at index 1 — well INSIDE `messages[0:msg_count-2]`. So every
subsequent round that re-enters the mutator triggers the warning unless
`notify_compaction` is called.

Symptom: WARNING `[CacheTrack] conv=X call=N ⚠ PREFIX MUTATION DETECTED`
firing on regularly spaced rounds (e.g. every 5 rounds = the
`_get_modified_files_attachment` throttle) and `total_breaks` climbing
unexpectedly in the cache diagnostics. Found 458+ such warnings in
error.log on 2026-05-13, 232 alone for one swe-bench conversation.

When adding a NEW per-turn injection helper, audit the new function
against this list and either (a) call `notify_compaction` after the
real mutation OR (b) prove the mutated index is always >= msg_count-2.

