---
name: dispatch_stream_raw_messages_preferred
description: Prefer raw-messages path over pre-built body for dispatch_stream
enabled: true
tags: [llm_dispatch, build_body, convention, bug-pattern]
created: 2026-04-18T03:44:00Z
updated: 2026-04-18T03:44:00Z
---

# dispatch_stream: use raw messages, not a pre-built body

`lib/llm_dispatch/api.py::dispatch_stream` accepts either a pre-built body dict
or a raw `messages` list. **Always prefer the raw-messages path.**

## Why
When you pre-build a body for `model_A` and dispatch retries pick `model_B`
(different key/slot), the body has to be re-adapted via
`_readjust_thinking_params` + `_clamp_max_tokens`. Any provider-specific quirk
not handled there leaks through as HTTP 400. Historical bugs:
- Claude Opus 4.7+ now rejects `temperature`/`top_p`/`top_k` with HTTP 400
  (previously silently ignored). Build_body strips them; pre-built body path
  needed a separate defensive strip in `_readjust_thinking_params`.
- GLM temperature clamp, Qwen `enable_thinking` shape, Doubao `thinking.type`,
  etc — each provider has quirks.

Raw-messages path calls `build_body(slot.model, …)` AFTER the slot is picked,
so the body is always correct for the real target model — no post-hoc fixes.

## How
```python
# ❌ Pre-built body (fragile on model swap)
body = build_body(model, messages, temperature=0, stream=True)
body['tools'] = my_tools
dispatch_stream(body, ...)

# ✅ Raw messages (robust)
dispatch_stream(
    messages,
    tools=my_tools,              # ← new param, added 2026-04
    temperature=0,
    thinking_enabled=False,
    prefer_model=model,
    strict_model=bool(model),
    ...
)
```

`dispatch_stream` now has a `tools=None` kwarg (injected into build_body on
raw-messages path, or into body['tools'] on pre-built path for symmetry).

## Call sites still on pre-built body
- `lib/tasks_pkg/manager.py::stream_llm_response` — chat main flow. Migrating
  would let us delete `_readjust_thinking_params` entirely. Do after
  observing paper/swarm run clean for a while.

## Migrated
- `routes/paper.py::_stream_report_with_tools` (2026-04-18) — triggered by
  Claude Opus 4.7 Bedrock HTTP 400 on `temperature`.

