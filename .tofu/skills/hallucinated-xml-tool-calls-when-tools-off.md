---
name: hallucinated-xml-tool-calls-when-tools-off
description: Bug: model hallucinates XML <tool_call> blocks in content when search/tools=off but conversation history contains real tool call results from earlier search=on turns — caused by buildApiMessages injecting tool history and <tools_used_previous_turn> into context without tools param
enabled: true
tags: [javascript, python, debugging, tool-call, hallucination, search-mode, frontend, orchestrator]
created: 2026-03-20T03:07:37Z
updated: 2026-03-20T03:07:37Z
---

# Hallucinated XML Tool Calls When Tools Are Off

## Symptom
When a user switches from a `search=on` preset (e.g., `medium`) to a `search=off` preset (e.g., `opus`) mid-conversation, the model generates hundreds of `<tool_call><tool_name>read_file</tool_name>...</tool_call>` XML blocks in its text content instead of real API tool_use calls. The frontend receives `searchRounds=[]` and no tool UI is rendered — the user sees garbled XML text.

## Root Cause Chain
1. **msg[N]** used `preset=medium` (search=ON), successfully executed 141+ real API tool calls. Tool results are embedded in the assistant content and `searchRounds`.
2. **User switches to `preset=opus`** (search=OFF) for the next message.
3. **`buildApiMessages()`** (frontend, `static/js/main.js`):
   - Sends msg[N]'s full content (containing tool call context) **as-is** to the next request
   - Injects `<tools_used_previous_turn>` summary into the next user message (lines ~499-502)
4. **Backend orchestrator** sets `tools=None` because search=OFF → API request has **no `tools` parameter**
5. **Model sees tool history** in context but can't make real API calls → hallucinates XML tool_call blocks in plain text content
6. **Orchestrator** sees `finish_reason=stop`, no `tool_calls` array → loop exits normally, `searchRounds=[]`
7. **Frontend** gets empty `searchRounds`, no tool UI rendered

## Evidence (chatuiupdate conversation `mmx9xzoirupnyw`)
- Task `d621d2f8`: `tools=no`, `search=off`, model output 57,381 chars with 278 `<tool_call>` XML blocks
- Log: "Loop ending normally: model returned text without tool_calls at round 0"

## Key Code Locations
- `static/js/main.js` → `buildApiMessages()` line ~499: `<tools_used_previous_turn>` injection
- `static/js/main.js` → `buildApiMessages()` line ~539: `messages.push({ role: "assistant", content: msg.content })` sends full tool-laden content
- `lib/tasks_pkg/orchestrator.py` → `_tools_this_round = tool_list if (tool_list and round_num < max_tool_rounds) else None`

## Fix Options
**A. Frontend (`buildApiMessages`)**: Skip `<tools_used_previous_turn>` injection when current request has `search=off` / no tools enabled
**B. Backend (orchestrator)**: Detect `<tool_call>` XML patterns in text content when `tools=None`, and either:
  - Auto-enable tools and retry
  - Strip the XML blocks from content
  - Add a warning to the user
**C. Both**: Most robust approach

