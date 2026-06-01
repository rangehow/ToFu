---
name: compaction-defaults-2026-04-19-relaxed
description: Relaxed compaction defaults (2026-04-19) to reduce aggressiveness after SWE-bench analysis showed thinking-strip and tool-result compaction hurt model performance
enabled: true
tags: [compaction, hyperparameters, context-management, config-change, swebench]
created: 2026-04-19T10:46:02Z
updated: 2026-04-19T10:46:02Z
---

# Compaction Defaults — Relaxed on 2026-04-19

All changes approved by user after SWE-bench vs Claude-Code gap analysis
showed Tofu's aggressive compaction (especially thinking-block stripping)
was hurting model performance on iterative coding tasks.

## New defaults in `lib/tasks_pkg/compaction.py`

| Constant | Old | **New** | Reason |
|---|---:|---:|---|
| `MICRO_HOT_TAIL` | 30 | **60** | Keep more recent tool results uncompressed (most runs <60 tool calls) |
| `MICRO_COMPACT_THRESHOLD` | 500 | **2000** | Only compact bulky outputs; leave small reads untouched |
| `_SUMMARY_TRIGGER_RATIO` | 0.80 | **0.90** | Delay lossy L2 summary — 1M ctx × 10% = 100k headroom still safe |
| `_KEEP_RECENT_PAIRS` | 4 | **8** | When summary fires, preserve more verbatim turns |
| `_THINKING_HOT_TAIL` | 4 | **20** | Preserve reasoning_content scratchpad — stripping mid-task forces re-derivation |

## Unchanged (not compression knobs)
- `_OUTPUT_RESERVE = 32_000`, `_COMPACTION_RESERVE = 8_000` — correctness margins
- `_DEFAULT_CONTEXT_LIMIT = 1_000_000`
- `_SUMMARY_COOLDOWN = 30.0`s
- Phase C image-strip — critical OOM protection (see memory `micro-compact-image-strip-bug-fix`), must NOT be relaxed

## Why the thinking-block change matters
Phase A of `micro_compact` strips `reasoning_content` from every assistant
message older than `_THINKING_HOT_TAIL`. On a 30-turn SWE-bench run with
old default of 4, that erased ~26 thinking blocks. The model lost its
scratchpad for "why did I pick this fix" and tended to produce larger,
over-engineered patches (verified in django-12325 case study — Tofu's
1034-byte patch over-generalized while CC's 629-byte patch was a
one-line conditional).

## Audit log entries
All 5 changes logged via `audit_log('config_change', ...)` in
`logs/audit.log` with `approved_by='user'`.

## Server restart required
Changes take effect on next `server.py` restart (constants bound at
import). Per CLAUDE.md §12, do NOT auto-restart server.py — user will
restart when convenient.

## If rollback needed
`git log -p lib/tasks_pkg/compaction.py` for the 2026-04-19 commit,
or simply change the 5 constants back to their old values above.

