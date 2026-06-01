---
name: timer-tool-args-str-coercion
description: LLM tool-call args arrive as strings; coerce int numerics with _coerce_int_arg() to avoid TypeError in max()/comparisons. Fixed 2026-04-25 in scheduler.
enabled: true
tags: [bug-fix, scheduler, timer, tool-args, llm, defensive-coding, type-coercion]
created: 2026-04-24T21:11:02Z
updated: 2026-04-24T21:11:02Z
---

# LLM Tool-Args: Coerce Numeric Values Defensively

## The bug

On 2026-04-25 04:57:40+ the scheduler started spamming:

```
[Timer] timer_create failed: '>' not supported between instances of 'int' and 'str'
  File ".../lib/scheduler/timer.py", line 73, in create_timer
    poll_interval = max(poll_interval, 10)  # floor at 10s
TypeError: '>' not supported between instances of 'int' and 'str'
```

**Root cause**: the LLM generated `timer_create` tool calls with
`{"poll_interval": "60"}` (string!) even though the tool JSON schema
declares the field as `integer`. The value flowed unchecked through
`_execute_timer_create` → `create_timer(poll_interval=...)` → `max(...)`
→ boom.

## The class of bug

Several LLM providers (especially when tool schemas pass through
multiple normalisation layers or when the model is just misbehaving)
emit numeric tool arguments as JSON strings. **Never trust that a
numeric tool arg is actually an `int`/`float`.**

Other places this pattern has surfaced in this codebase:
- `max_polls` (same call site)
- `timeout`, `poll_interval` in `_execute_await_task`
- Historically: `max_tokens`, `temperature` in swarm sub-agent configs

## The pattern

Add a tiny helper at the top of each executor module that accepts LLM
tool args, and use it to coerce every numeric field **before** passing
it to downstream code:

```python
def _coerce_int_arg(name, raw, default):
    """Coerce an LLM-supplied tool arg to int, warning on fallback."""
    if isinstance(raw, bool):
        # bool is a subclass of int — reject to avoid True→1 / False→0
        logger.warning('[Timer] Boolean %s=%r passed as numeric — '
                       'coerced to default %d', name, raw, default)
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as _e:
        logger.warning('[Timer] Non-integer %s=%r — coerced to default %d '
                       '(reason: %s)', name, raw, default, _e)
        return default
```

Key design choices:
- **Warning level**, not error — the LLM did something sloppy but we
  recovered; no need to flood `error.log`.
- **%-style logging** (per `CLAUDE.md` §2.6) — lazy formatting.
- **Default fallback** rather than raising — `timer_create` must be
  robust against LLM sloppiness; raising would kill the entire tool
  call and the user's task.
- **Explicit bool rejection** — Python's `bool` is `isinstance(int)`,
  so `int(True) == 1`. Not what we want for a numeric-arg field.
- **Defense-in-depth**: coerce at BOTH the executor (where the raw
  `fn_args` dict enters our control) AND the public API (`create_timer`
  in `lib/scheduler/timer.py`). Libraries called from multiple paths
  should never assume the caller sanitised.

## Files touched

- `lib/scheduler/executor.py` — added `_coerce_int_arg()` helper;
  applied to `_execute_timer_create` (`poll_interval`, `max_polls`)
  and `_execute_await_task` (`timeout`, `poll_interval`).
- `lib/scheduler/timer.py::create_timer` — defense-in-depth
  coercion at the top of the function body for `poll_interval`
  and `max_polls`.

## Regression test

`debug/test_pg_auto_heal.py::test_timer_str_args_coerced` — stubs out
the DB layer and invokes `create_timer(poll_interval='60', max_polls='100')`;
used to raise `TypeError`, now returns a valid timer row.

## When to add similar coercion

Whenever you write a new tool handler in `lib/tasks_pkg/handlers/*` or
`lib/scheduler/` that reads numeric / bool / enum values from the raw
`fn_args` dict, add the coercion at the handler entrypoint. Trust only
the keys/existence, not the types.

