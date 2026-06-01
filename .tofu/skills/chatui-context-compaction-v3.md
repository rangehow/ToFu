---
name: chatui-context-compaction-v3
description: Two-layer context compression pipeline: micro-compact hot-tail + smart summary as synthetic tool result for prompt cache preservation
enabled: true
tags: [python, architecture, context-management, compaction]
created: 2026-03-23T08:46:19Z
updated: 2026-03-23T08:46:19Z
---

## Context Compaction Architecture — chatui project

### Pipeline: 2 layers, called before every LLM API call

```
run_compaction_pipeline(messages, current_round, task)
  ├─ Layer 1: micro_compact(messages, conv_id)       ← every round, zero LLM cost
  └─ Layer 2: smart_summary_compact(messages, task)   ← when tokens > 80% of usable context
```

### Layer 1 — Micro-compaction (hot-tail)
- Keeps last `MICRO_HOT_TAIL=10` tool results intact
- Older tool results: archived to `tool_output_full` DB table, replaced with placeholder
- Idempotent: checks `'compacted' in content[:80]` to skip already-processed
- Zero LLM cost

### Layer 2 — Smart summary as synthetic tool result
- Trigger: estimated tokens > 80% of (context_limit - 32K output - 8K reserve)
- Cooldown: 30s per conv_id
- Summary injected as `assistant{tool_calls:[context_compact]}` + `tool{result: summary}` pair
- This preserves ALL prefix messages (system prompt cache intact)
- Summary sits at tail = maximum salience
- As summary ages, Layer 1 will compact it like any other tool result

### Key design decisions
1. **No tool output interception**: intercepting read_files causes infinite loops
2. **No structural truncation layer**: removed tier/rank complexity; just micro-compact + summary
3. **Summary is a tool result, not a user message**: preserves prompt cache, natural lifecycle
4. **No round-gating**: pipeline runs at round 1; Layer 2 triggers on token count only
5. **DB-based storage**: `tool_output_full` + `transcript_archive` tables, keyed by conv_id

