---
name: chatui-bull-bear-debate-engine
description: Bull vs Bear debate engine for fund autopilot: parallel dual-agent debate (bull/bear) before final mega-prompt synthesis, inspired by TradingAgents (UCLA/MIT), reduces confirmation bias
enabled: true
tags: [python, fund, autopilot, debate, multi-agent, architecture]
created: 2026-03-23T12:26:43Z
updated: 2026-03-23T12:26:43Z
---

# Bull vs Bear Debate Engine

## Architecture
- **Module**: `lib/fund_autopilot/debate.py`
- **Integration**: Called in `cycle.py` between `_gather_context()` and `build_autopilot_prompt()`
- **Both paths**: Sync (`run_autopilot_cycle`) and streaming (`build_autopilot_streaming_body`)

## Flow
1. `_gather_context()` collects intel, KPIs, quant signals, holdings, strategies
2. `run_bull_bear_debate(ctx)` runs 🐂 Bull + 🐻 Bear in parallel via `smart_chat_batch`
3. `build_debate_context(bull, bear)` formats into "Part 5" for mega-prompt
4. `build_autopilot_prompt(..., debate_ctx=debate_ctx)` injects debate
5. Super-Analyst must produce `debate_verdict` in structured JSON output

## Key Design Decisions
- **Parallel execution**: Uses `smart_chat_batch` with `max_concurrent=2` for zero added latency
- **Cheaper model**: Uses `capability='text'` (not 'thinking') — debate is argumentation, not synthesis
- **Graceful degradation**: If debate fails, `ctx['debate_ctx'] = None` → prompt renders without it
- **Temperature 0.4**: Slightly creative for diverse argument generation
- **Max tokens 4096**: Enough for 5+ structured arguments each
- **KPI summary**: Compact version in `_build_kpi_summary()` (vs verbose version in reasoning.py)

## JSON Output Schema Addition
```json
"debate_verdict": {
  "bull_bear_ratio": "60:40",
  "bull_best_point": "...",
  "bear_best_point": "...",
  "key_disagreement": "...",
  "your_judgment": "..."
}
```

## Files Modified
- `lib/fund_autopilot/debate.py` — NEW: debate engine
- `lib/fund_autopilot/reasoning.py` — Added `debate_ctx` param, B2 verdict section, `debate_verdict` JSON field
- `lib/fund_autopilot/cycle.py` — Import + call debate in both sync/streaming paths
- `lib/fund_autopilot/__init__.py` — Register debate module in facade

