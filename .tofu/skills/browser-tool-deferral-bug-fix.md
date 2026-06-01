---
name: tool-deferral-no-static-phase
description: Design rule: Phase 1 static deferral removed — all user-selected frontend tools stay core; only Phase 2 dynamic threshold deferral remains as safety valve
enabled: true
tags: [tool-deferral, design-rule, frontend-tools]
created: 2026-04-03T15:10:51Z
updated: 2026-04-04T02:16:15Z
---

# Tool Deferral: No Static Phase — User-Selected Tools Never Deferred

## Design Rule (2026-04-04)
All tools the user explicitly enabled via frontend toggles (browserEnabled, schedulerEnabled,
imageGenEnabled, desktopEnabled, etc.) must NEVER be statically deferred.

## What Changed
- **Phase 1 (static deferral) was REMOVED** from `lib/tools/deferral.py`'s `partition_tools()`.
  Previously, tools in `DEFERRED_TOOL_HINTS` were auto-deferred regardless of user intent.
- **Phase 2 (dynamic threshold deferral) remains** as a safety valve — only triggers when
  tool definition tokens exceed 10% of context window (`_AUTO_DEFER_THRESHOLD_PCT`).
- `DEFERRED_TOOL_HINTS` is kept but now ONLY used for `tool_search` keyword matching
  (when Phase 2 dynamically defers tools and the model needs to find them).
- The hints dict was expanded to include ALL tool categories (browser, skills, error tracker,
  swarm, etc.) for comprehensive keyword matching.

## Why
1. `_assemble_tool_list()` already gates tools by user toggles — every tool in the list is user-selected
2. Silently deferring user-selected tools violated user intent (e.g., user enables browser → browser tools hidden)
3. Previous browser deferral bug caused model to waste 10+ rounds or use inferior fallbacks
4. Gemini models never called `tool_search`, so deferred tools were permanently unavailable

## Files Changed
- `lib/tools/deferral.py` — Removed Phase 1 loop, expanded `DEFERRED_TOOL_HINTS` for search
- `tests/test_cc_alignment.py` — Updated 3 tests to expect no static deferral

## Key Invariant
```
user enables toggle → tool in tool_list → tool in core (NOT deferred)
                      ↓ (only if Phase 2 token pressure triggers)
                      tool MAY be dynamically deferred → recoverable via tool_search
```

