---
name: sse-yield-in-callback-antipattern
description: Critical bug pattern: Python yield inside a callback function creates a sub-generator that is never iterated — SSE progress events silently vanish; fix with thread+queue pattern
enabled: true
tags: [python, flask, sse, streaming, yield, callback, generator, critical-bug, thread-queue]
created: 2026-03-29T04:45:26Z
updated: 2026-03-29T04:45:26Z
---

# SSE Yield-in-Callback Anti-Pattern

## The Bug

When a Flask SSE endpoint passes a callback to a synchronous function,
and that callback uses `yield` to emit SSE events:

```python
def generate():
    def on_progress(phase, done, total, msg=''):
        # ❌ BUG: This makes on_progress a generator function!
        # Calling on_progress() returns a generator object that nobody iterates.
        yield f'event: progress\ndata: {json.dumps(...)}\n\n'

    result = run_full_fetch(on_progress=on_progress)  # calls on_progress()
```

**What happens**: `on_progress()` returns a generator object (because it contains
`yield`). The caller (`run_full_fetch`) ignores the return value. The SSE event
is **silently lost** — the client sees zero progress.

## The Fix

Use a **background thread + queue** pattern:

```python
def generate():
    event_q = queue.Queue()

    def on_progress(phase, done, total, msg=''):
        event_q.put({'phase': phase, 'done': done, 'total': total, 'message': msg})

    result_holder = [None]
    error_holder = [None]

    def _run():
        try:
            result_holder[0] = run_full_fetch(on_progress=on_progress)
        except Exception as e:
            error_holder[0] = e
        finally:
            event_q.put(None)  # Sentinel

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    while True:
        try:
            item = event_q.get(timeout=1.0)
        except queue.Empty:
            yield ': keepalive\n\n'
            continue
        if item is None:
            break
        yield f'data: {json.dumps(item)}\n\n'

    # Final result/error
    if error_holder[0]:
        yield f'data: {json.dumps({"error": str(error_holder[0])})}\n\n'
    elif result_holder[0]:
        yield f'data: {json.dumps(result_holder[0])}\n\n'
```

## Key Points
- `yield` in a nested function makes it a generator, not a continuation of the outer generator
- The thread+queue pattern gives true real-time SSE streaming
- Always add keepalive comments to prevent proxy/browser timeout
- Use a `None` sentinel to signal thread completion

