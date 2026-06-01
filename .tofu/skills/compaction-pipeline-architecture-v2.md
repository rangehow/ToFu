---
name: context-compaction-pipeline-v3
description: Context compaction pipeline: L1 micro-compact (automatic, cold tools) + force-triggered pure LLM summary with selective turn compression (NOT in model's tool list, only orchestrator-injected at 80% threshold)"
enabled: true
tags: [python, compaction, context-management, architecture, llm-summary, selective-compression]
created: 2026-03-23T08:03:01Z
updated: 2026-03-25T08:04:32Z
---

# Context Compaction Pipeline v3

## Architecture

### Layer 1 — Micro-compaction (every round, zero LLM cost)
- Runs automatically in `run_compaction_pipeline()` before every LLM call
- Keeps **hot tail of 30** most recent tool results intact
- Cold tool results replaced with placeholder: `[tool_name result compacted — was N chars]`
- Skips results < 500 chars or already compacted
- Pure string replacement, no LLM cost

### Force-Triggered LLM Summary (at 80% of usable context)
- **NOT in the model's tool list** — the model never calls this voluntarily
- Triggered by `force_compact_if_needed()` when tokens > 80% of usable context
- Pure LLM summary via cheap model with **selective turn compression**

## Key Design Decisions

### Why NOT in the tool list
The model was calling `context_compact` voluntarily at 50k tokens (way below any threshold), wasting a round and triggering only mechanical Phase 1 truncation. Removing it from the tool list prevents this entirely.

### Synthetic tool pair injection
When force-compact fires, it injects a synthetic `assistant` (with tool_call) + `tool` (with result) message pair. The model sees the summary naturally as a tool response. The old messages before the boundary are deleted.

### Selective turn compression prompt
The cheap model rates each historical turn on a 0-3 relevance scale to the current query:
- 🟢 CRITICAL (3) → preserve verbatim details
- 🟡 USEFUL (2) → compress to 1-3 sentences
- 🟠 TANGENTIAL (1) → one-line mention or drop
- ⚪ IRRELEVANT (0) → drop entirely

Output sections: Active Context, Background, Decisions & User Preferences, Working State, Recently Accessed Files

## File Layout
- `lib/tasks_pkg/compaction.py` — All compaction logic
- `lib/tools/compact.py` — Empty (legacy, no longer exports tools)
- `lib/tasks_pkg/model_config.py` — COMPACT_TOOL removed from tool list
- `lib/tasks_pkg/executor.py` — Handler removed (model can't call it)

## Key Constants (in compaction.py)
- `MICRO_HOT_TAIL = 30` — L1 hot tail size
- `_SUMMARY_TRIGGER_RATIO = 0.80` — force-compact fires at 80% of usable context
- `_KEEP_RECENT_PAIRS = 4` — preserve last 4 user-assistant pairs verbatim
- `_SUMMARY_COOLDOWN = 30.0` — seconds between consecutive summary attempts
- `_SUMMARY_MAX_TOKENS = 3000` — max output for summary LLM call

