---
name: cost-prefetch-must-include-apiRounds
description: Per-render cost-fetch flood after server-side cost migration: prefetch missed apiRounds[*].usage
enabled: true
tags: [frontend, performance, cost, bug-fix, migration]
created: 2026-05-29T10:02:29Z
updated: 2026-05-29T10:02:29Z
---

# Cost prefetch flood: include apiRounds[*].usage

## Symptom
Frontend laggy after server-side cost migration. Access log shows
100+ `POST /api/v1/messages/cost` requests within 1-2 seconds on every
`renderChat()` (vs only a handful of batched `/cost/batch` calls).

## Root cause
After cost math moved server-side, `static/js/core/cost.js`
`_prefetchConvCosts(conv)` only batches each `m.usage`. But
`renderFinishInfo` in `static/js/ui/finish_info.js:126` ALSO calls
`calcCostCny(rd.usage, ...)` for **every entry in `msg.apiRounds`**
(per-round breakdown in cost-tag tooltip). Those round usages weren't
seeded by the prefetch → cache miss → each one fires its own
single-shot fetch in parallel.

For a conv with 50 assistant msgs averaging 2 rounds each → ~100
parallel `/cost` requests per render.

## Fix
Extend `_prefetchConvCosts` to also `_push()` every `rd.usage` from
`m.apiRounds[]` into the batch. Use `_seen` Set to dedup fingerprints
across both message-level and round-level usages. Provider id falls
back to the message-level provider if the round didn't carry one.

## Lesson
When migrating frontend math → server endpoint, audit ALL call sites
that hit the math, not just the message-level renderer. The round-
level tooltip path was easy to miss.

