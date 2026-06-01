---
name: compaction-turn-based-redesign
description: Turn abstraction replaces pair-count in lib/tasks_pkg/compaction.py (2026-04-26)
enabled: true
tags: [compaction, architecture, bug-fix]
created: 2026-04-26T15:01:36Z
updated: 2026-04-26T15:01:36Z
---

# Compaction turn-based redesign (2026-04-26)

## The bug (conv=modearkif6k9tr, task=690e2774)

`_find_pair_boundary` in `lib/tasks_pkg/compaction.py` treated
`_KEEP_RECENT_PAIRS=8` as an EXACT count: "preserve 8 pairs OR
preserve nothing". For an agentic conversation with only 4 user
turns but 362 messages (many tool rounds per turn), it returned
`len(messages)`, swallowing the live user query into the summary.
Log line: `[Compact] preserving 0 recent`.

Compounding: `_get_context_limit` didn't recognise `claude-opus-4.7`,
so it fell through `'claude' in model` → 200k, triggering force-compact
at ~145k tokens on what is actually a 1M-context model.

## The fix — turn abstraction

**Turn** = `[user_msg, ...all subsequent non-user messages until next user]`.
Atomic unit. One agentic turn can have 100+ tool messages; old "pair"
abstraction couldn't express this.

New API in `compaction.py`:
- `_find_turn_boundary(messages, *, budget_tokens=inf, max_turns=16)` —
  core impl. HARD INVARIANT: current (last) turn always preserved in
  full, regardless of size. Older turns added newest→oldest while
  under `budget_tokens` AND `max_turns`.
- `_find_pair_boundary(messages, keep_recent=N)` — legacy wrapper,
  maps `keep_recent` → `max_turns`. Preserved for BC tests.
- `_PRESERVE_BUDGET_RATIO=0.30`, `_MAX_PRESERVE_TURNS=16` — new
  constants. `_KEEP_RECENT_PAIRS` kept for legacy wrapper only.

## Other fixes bundled

1. **Refusal path**: `execute_compact_tool` now returns "skipped"
   string WITHOUT mutating messages if no user message exists, or
   if boundary would preserve nothing.
2. **Cooldown deferred**: `_summary_cooldowns[conv_id] = time.time()`
   is set AFTER passing all guards, not at function top. Refused
   compactions don't claim the 30s slot.
3. **Log level**: refusal paths use `logger.error` (not info) so
   they show in `logs/error.log`.
4. **Context limit**: `_get_context_limit` now probes
   `lib.model_info.is_claude_opus_47()` and regex-matches
   `(opus|sonnet)-4\.[6-9]` and higher → 1M tokens. Prevents silent
   downgrade of new Claude releases.
5. **Relevance-format filter**: `_format_messages_for_summary` now
   only includes `user` messages + `assistant` messages with non-empty
   natural-language content. Tool results (`role='tool'`) and
   tool-call-only assistant messages are dropped. The old "→ grep_search(...)"
   decoration is gone — a relevance-rating cheap model doesn't need
   tool invocation details and they just inflate input tokens.

## Tests

`tests/test_compaction_improvements.py`:
- `TestTurnBoundary` (7 tests) — current-turn invariant, budget cap,
  max_turns cap, no-user refusal, always-on-user-index invariant.
- `TestPairBoundaryBackwardCompat` (2 tests) — regression for the
  exact modearkif6k9tr scenario.
- `TestRelevanceFormatFilter` (6 tests) — tool-msg exclusion,
  tool-call-only assistant drop, reasoning_content leak prevention.
- `TestContextLimitDetection` (3 tests) — opus/sonnet 4.6/4.7/4.8
  variants, older-Claude still 200k.
- `TestCompactRefusalGuards` (2 tests) — messages untouched on
  refusal, cooldown not set on refusal.

92/92 unit tests pass (72 legacy + 20 new).

## Reactive compact migration

`reactive_compact` used `keep_recent_pairs=2` (old API, still works
via wrapper). Now ALSO passes `preserve_budget_tokens=int(usable*0.10)`
for a tighter verbatim-preservation budget when the API has already
rejected the request as too long. Both knobs stay for defence-in-depth.

