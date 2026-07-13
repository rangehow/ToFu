---
name: cache-tracking-prefix-mutation-mutators
description: Prefix mutators must pick the RIGHT notify — notify_compaction ONLY for expected drops, notify_history_rewrite for re-billing mutations (labels without masking the cost)
enabled: true
tags: [cache_tracking, logging, convention, cost-visibility]
created: 2026-05-13T14:50:24Z
updated: 2026-07-08T00:00:00Z
---

# Cache-tracking prefix mutation discipline

`lib/tasks_pkg/cache_tracking.py:detect_cache_break` raises an ANONYMOUS
`⚠ PREFIX MUTATION DETECTED … content hash changed without compaction`
WARNING whenever the hash of the cache prefix (`messages[0 : len -
EDITABLE_TAIL_COUNT]`) changes between rounds. That line is a LEADING
INDICATOR whose only value is when the cause is UNKNOWN — it pollutes
error.log AND flags a real prompt-cache re-bill.

## ⚠ Two different signals — pick the RIGHT one (this is the load-bearing rule)

There are TWO notify functions, and they are NOT interchangeable. Choosing
wrong either spams a false alarm or (worse) HIDES a real cost.

| Function | Sets | Effect on `detect_cache_break` | Use ONLY when |
|---|---|---|---|
| `notify_compaction(cid)` | `compaction_pending` | **Blanket-suppresses ALL 4 break detectors + skips the wire diff + can flip `_wire_proven_identical→True`.** | The prefix genuinely SHRANK/DROPPED and the cache read-drop is EXPECTED and FREE (L2 force-summary, ancient-round drop). |
| `notify_history_rewrite(cid)` | `history_rewrite_pending` | **NAMES the cause + silences ONLY the anonymous leading-indicator warning. Does NOT gate any break classifier and does NOT skip the wire diff.** The re-bill is still detected, attributed to the exact `msg[i].field`, counted in `total_breaks`, and surfaced as `prefix_mutation`. | The prefix BYTES CHANGED and it actually RE-BILLS (in-place edit of an in-prefix message: per-turn profile/detail splice, reconcile, committed-dict projection). |

**The trap (the `notify_compaction` masking anti-pattern):** a mutation that
re-bills is NOT a drop. Silencing it with `notify_compaction` launders a real,
recurring cost into a false "server-side — PROVEN" verdict / invisibility —
exactly the "backend must be the single source of truth for cost" violation.
If your code changed prefix bytes and the body gets re-billed, you want
`notify_history_rewrite` (label, don't mask). Reserve `notify_compaction` for
genuine expected drops.

## Known prefix-mutating call sites

| Function | Location | Signal |
|---|---|---|
| `inject_relevant_memories` (memory prefetch) | `lib/memory/prefetch.py` | `notify_compaction` (drop semantics — audit if this should be history_rewrite) |
| `inject_attachments` (per-turn reminders) | `lib/tasks_pkg/attachments.py` | `notify_compaction` |
| user-profile / detail splice | `lib/tasks_pkg/system_context.py:~730` | **`notify_history_rewrite`** (fixed 2026-07-08 — was `notify_compaction`, which masked the re-bill) |
| L1/L2 compaction pipeline | `lib/tasks_pkg/compaction/_pipeline.py` (NOTE: old `compaction.py:3344` path is gone post-refactor) | `notify_compaction` ONLY for prefix-touching edits (see `l1-compaction-notify-masks-detection` — default out-of-prefix L1 must NOT notify) |

## How the anonymous warning is gated (2026-07-08)

`detect_cache_break` keeps `_prefix_mutated=True` on a hash change (so the
CONFIRMED, NAMED break still fires), but the anonymous `PREFIX MUTATION
DETECTED …without compaction` `logger.warning` is now gated on
`if not _was_history_rewrite:`. So a `notify_history_rewrite` mutation is
silent-on-the-anonymous-line yet fully detected + attributed downstream.

## Guardrail

When adding a NEW per-turn injection helper that touches the cache prefix:
1. If it DROPS/shrinks the prefix and the read-drop is expected+free →
   `notify_compaction`.
2. If it EDITS bytes in-place and re-bills → `notify_history_rewrite`
   (NEVER `notify_compaction` — that hides the cost).
3. Or prove the mutated index is always `>= len - EDITABLE_TAIL_COUNT` (in the
   editable tail) so no signal is needed at all.

Guarded by `tests/test_cache_prefix_stability.py`:
`test_profile_splice_labels_without_suppressing_the_rebill` (3-arm:
control alarm fires; history_rewrite silences the alarm YET surfaces
`prefix_mutation` + increments `total_breaks`; compaction masks both) and
`test_history_rewrite_does_not_flip_proven_server_side`.
