---
name: claude-opus-4.7-breaking-changes
description: Claude Opus 4.7 breaking changes: hidden thinking (display=summarized), xhigh effort, sampling params REJECTED (HTTP 400) — unconditional strip in build_body
enabled: true
tags: [claude, opus, thinking, llm-client, breaking-change, api]
created: 2026-04-17T10:09:33Z
updated: 2026-04-18T03:35:35Z
---

# Claude Opus 4.7 Breaking Changes

## Summary
Claude Opus 4.7 (and later — 4.8, 5.0, etc.) introduced breaking API changes vs Opus 4.6.

## The 4 Breaking Changes

### 1. Hidden thinking by default
- 4.6 and earlier: `thinking.type="adaptive"` → reasoning streamed as `reasoning_content` deltas automatically.
- 4.7+: reasoning trace is **hidden** unless you send `thinking.display="summarized"`.
- Without `display`, the stream contains only `content` deltas — user sees empty "thinking..." panel.

### 2. Sampling params ignored (will be rejected later)
- `temperature`, `top_p`, `top_k` are silently ignored on 4.7 today.
- Anthropic docs warn they may return HTTP 400 in a future revision.
- Policy: **do not send** sampling params on 4.7+.

### 3. `thinking.budget_tokens` removed
- 4.6 accepted `{type: "enabled", budget_tokens: N}`.
- 4.7 only supports `{type: "adaptive"}`.
- Bedrock gateways (Meituan `aws.*`) still accept `adaptive` — do NOT switch to `enabled+budget_tokens`.

### 4. New `xhigh` effort level
- Effort ladder is now: `low`, `medium`, `high`, `xhigh`, `max`.
- `xhigh` is Opus 4.7-only — sending it to 4.6 returns HTTP 400.
- Auto-downgrade `xhigh → high` on non-4.7 models in `build_body()` and `_readjust_thinking_params()`.

## Implementation

### Detector: `lib/model_info.py::is_claude_opus_47(model)`
Uses regex `opus[-_.]?(\d+)[-_.](\d+)` and returns True for (major, minor) >= (4, 7).
Matches: `claude-opus-4-7`, `aws.claude-opus-4.7`, `us.anthropic.claude-opus-4-7-v1:0`, `claude-opus-5-0`, etc.

### build_body() — `lib/llm_client.py::build_body()` Claude branch
```python
elif not _tf and is_claude(model) and thinking_enabled:
    body['thinking'] = {'type': 'adaptive'}
    if is_claude_opus_47(model):
        body['thinking']['display'] = 'summarized'
        # Do NOT send temperature on 4.7+
    else:
        body['temperature'] = 1.0
    if _effort and _effort != 'medium':
        if _effort == 'xhigh' and not is_claude_opus_47(model):
            _effort = 'high'  # downgrade
        body['effort'] = _effort
```

### dispatch_stream's `_readjust_thinking_params()` — MUST MIRROR BUILD_BODY
File: `lib/llm_dispatch/api.py::_readjust_thinking_params()`
This is called on **every** dispatched request (not just model-swap) at line ~541 of api.py.
It strips all thinking keys and re-applies based on new_model. Its Claude branch must stay
IN SYNC with build_body's Claude branch — otherwise it silently drops `display: summarized`
on every request and user sees no thinking.

**CRITICAL GOTCHA (discovered 2026-04-18):** An earlier version of `_readjust_thinking_params`
only set `{'type': 'adaptive'}` + `temperature=1.0` for Claude, with no 4.7 awareness.
Result: build_body correctly added `display: summarized`, but dispatch then stripped it.
Conv `mo3s3y3is3mxwy` exhibited this — depth=xhigh, correct body at build_body time, but
raw SSE dump showed `"thinking":{"type":"adaptive"}` (no display) going over the wire, and
0 thinking chars came back. Fix: add `is_claude_opus_47` handling in the readjust function's
Claude branch too.

## UI: `static/js/main.js::_DEPTH_LABELS`
```js
const _DEPTH_LABELS = { off: 'Off', low: 'Low', medium: 'Med', high: 'Hi', xhigh: 'xHigh', max: 'Max' };
```
User preferred lowercase `xhigh` label (not `X-Hi` / `X-Hi`) — applies to desktop bar,
mobile bar, model badge, and Settings default-depth dropdown.

## Diagnostic checklist — ordered most to least likely

When user reports "thinking is empty on Opus 4.7":
1. **Raw body check (FIRST)**: enable `LLM_DEBUG_RAW_SSE=opus-4` and verify the outgoing body
   contains `"thinking":{"type":"adaptive","display":"summarized"}`. If it lacks `display`,
   `_readjust_thinking_params` has regressed — fix the dispatch code, not build_body.
2. **Prompt doesn't need reasoning**: trivial prompts trigger adaptive-skip → 0 thinking.
3. **Model satisfies "show reasoning" in visible content**: when prompt says "show your
   reasoning step by step", the model may put reasoning in `content` and skip the summary
   channel entirely. This is correct 4.7 behavior, not a bug.
4. **Rate limit / 429 failover**: dispatch may switch to a different slot/key mid-stream;
   verify which slot actually served the request via log_prefix in manager.py.

## Raw SSE debugging
- Toggle: `LLM_DEBUG_RAW_SSE=opus-4` env var (substring match on model id).
- Output: `logs/raw_sse.log` — each request gets a `body=...` header with the sanitized
  outgoing body snapshot, followed by every SSE line verbatim.
- Use `grep "^body=" logs/raw_sse.log | tail -N` to inspect recent outgoing thinking params.

## When to update export.py
No sensitive data added by this feature — no export.py changes needed.

