---
name: timestamp-skills-injection-cache-kill
description: A/B tested: date-only in system prompt (Arm C) is 76.7% cheaper than current behavior; full datetime in system prompt is CATASTROPHIC (+300% cost); date changes 1x/day vs HH:MM every minute
enabled: true
tags: [cache, performance, timestamp, anthropic, bug-fix]
created: 2026-04-06T15:37:16Z
updated: 2026-04-06T16:12:39Z
---

# Timestamp Placement for Cache Efficiency — A/B Test Results

## Problem
Injecting "Current date and time: 2026-04-06 15:45 UTC" into the user message
kills cache in three ways:
1. **Intra-task**: Re-injecting every round changes the cached prefix bytes
2. **Inter-task**: Cached prefix has timestamp, new task's history has clean messages
3. **Non-project**: System prompt below 4096-token Opus minimum → BP1-BP2 ignored

## A/B Test Results (Real API, aws.claude-opus-4.6, 8 rounds each)

| Rank | Arm | Strategy | Cost | Cache% | Savings |
|---|---|---|---|---|---|
| 🥇 | **C** | **Date-only in system prompt** | **$0.36** | **85.7%** | **+76.7%** |
| 🥈 | B | Timestamp in user msg R0 only | $0.39 | 84.2% | +75.1% |
| 🥉 | A | Full timestamp in user msg every round | $0.49 | 77.9% | +68.6% |
| 💀 | **D** | **Full datetime in system prompt** | **$1.55** | **12.4%** | **CATASTROPHIC** |

### Key Insight: Date-only vs Full Datetime
- **Date-only** (`Current date: 2026-04-06`) changes once per UTC day → system prompt
  is perfectly stable for BP1-BP2 (1h TTL). Cost: $0.36.
- **Full datetime** in system prompt changes EVERY MINUTE → system prompt cache breaks
  every round → 0% cache in Task 1, 300%+ cost increase. Cost: $1.55.
- The difference is 4.3x cost (!) — never put minute-level timestamps in the system prompt.

## Fix Implemented
- Date-only injection in `_inject_system_contexts()` step 4.5 (system prompt)
- Format: `Current date: 2026-04-06` — changes once per UTC day
- Decoupled from `search_enabled` — model always knows today's date
- `inject_search_addendum_to_user()` converted to legacy cleanup-only function
- Old timestamps stripped from user messages for clean cache prefix

## Cancel-Search Safe
Previously, timestamp was only injected when `search_enabled=True`. If user
clicked "cancel search" (searchMode=off), the model had no date awareness.
Now the date is in the system prompt unconditionally — always available.

## Inter-Task Cache (Phase 2 Results)
- Arm C Task 2: **97.9% cache** — system prompt + tools fully cached from Task 1
- Arm A Task 2: 86.0% cache — BP1-BP2 (system) cached, BP4 (tail w/ timestamp) missed
- Arm D Task 2: 49.4% — system prompt cache from Task 1 partially usable

## When Full Time Is Needed
If the model needs exact current time for a search query, it can use the
`web_search` tool. The date-only format is sufficient for 99% of use cases
(date-dependent queries, scheduling, etc.).

## Test Script
```bash
python debug/test_timestamp_placement_ab.py --rounds 6 --arms A,B,C,D
python debug/test_timestamp_placement_ab.py --dry-run  # Preview logic
```

