---
name: paper-report-tool-calling
description: Paper report generation now uses tool-calling loop with web_search/fetch_url, model picker, visible streaming
enabled: true
tags: [paper-reader, report, tool-calling, web-search]
created: 2026-04-17T02:10:23Z
updated: 2026-05-20T05:14:24Z
---

---
name: paper-report-tool-calling
description: Paper report generation: tool-calling loop + streaming deltas + thinking/reasoning passthrough for progress visibility
enabled: true
tags: [paper-reader, report, tool-calling, web-search, streaming, thinking]
---

# Paper Report Generation — Tool-Calling Architecture

## Backend (`routes/paper.py`)
- `_stream_report_with_tools()` (now `_run_report_task`) — main report generator with tool-calling loop
  - Up to `_MAX_REPORT_TOOL_ROUNDS = 14` tool rounds (raised from 8 — see "Quality bar" below)
  - Supports `web_search` (batch) and `fetch_url` (batch) tools
  - `_execute_report_tool()` handles actual tool execution
  - Yields SSE events: `{delta}`, `{thinking}`, `{tool_call}`, `{tool_done}`, `{error}`, `{done}`
- `dispatch_stream` called with BOTH `on_content` and `on_thinking` callbacks so reasoning deltas are forwarded to the UI (needed because reasoning models can take minutes before emitting content — otherwise UI looks frozen).
- `build_body()` called with `max_tokens=128000` (auto-clamped per model — see longform-no-artificial-max-tokens-cap)
- System message instructs LLM to do a research-grade literature scan BEFORE writing (recommended search plan included; predecessors / contemporaries / post-publication follow-ups are all required).
- `force` param skips DB cache for regeneration
- DB cache keyed by `paper_hash + lang`

## Quality bar in the prompts (`_REPORT_PROMPT_EN` / `_REPORT_PROMPT_ZH`)
The prompts explicitly enforce:
1. **Related-work survey must be comprehensive AND current** — must include predecessors, concurrent work, AND post-publication follow-ups (mandatory if paper > 12 months old). At least 3-5 concrete follow-up papers must be named.
2. **Methodology must be reproduction-grade** — explain WHY each design choice was forced by the problem, not just WHAT was done. Use the "problem → constraint → option space → why X wins → cost paid" template.
3. **Mathematical formulation** subsection demands KaTeX equations + symbol glossary + dimensions for every key formula.
4. **Training & Optimization Recipe** lists 10 mandatory items (init, optimizer, LR schedule, batch construction, loss, regularization, gradient clipping, hardware, eval protocol, etc.) with explicit "(not specified)" fallback.

## Frontend (`static/js/paper-reader.js`)
- Model picker: `_populatePaperReportModelDropdown()` reuses `_registeredModels` from main.js
- `_paperReportModel` state var, `_selectPaperReportModel()`, `_togglePaperReportModelDropdown()`
- `_generatePaperReport(force)` — shows tool activity log + thinking panel + streaming content
- Initial DOM layout includes `<details id="reportThinkingBlock">` (hidden until first `thinking` delta), with summary "Thinking…" and body `#reportThinkingBody` (monospace, textContent-only, auto-scroll)
- Thinking block auto-collapses once first content `delta` arrives (still expandable by user)
- Tool status shows spinners for web_search/fetch_url, log entries when done
- `_paperReportAbort` AbortController for cancellation
- Background pre-generation was REMOVED — user sees streaming + thinking live

## CSS (`static/styles.css`)
- `.paper-report-model-picker`, `.paper-report-model-dropdown` — model selector
- `.paper-report-tool-status`, `.paper-report-tool-log` — tool activity indicators
- `.paper-report-thinking` — collapsible details block with pulsing dot, scrollable monospace body (max-height 180px)

## Why the thinking panel exists
User reported "I just see the prompt generating. Nothing else, but it does finish." — for reasoning/thinking models (e.g. o1, Claude with thinking, DeepSeek R1), the model can spend 30s-5min on reasoning before emitting any `content`. Without forwarding `on_thinking`, the UI stays on the "Generating report…" spinner with no progress signal. The thinking stream gives immediate visible activity.

