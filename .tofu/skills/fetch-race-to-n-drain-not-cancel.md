---
name: fetch-race-to-n-drain-not-cancel
description: Race-to-N fetch cancellation correlated with libc heap-corruption aborts; drain instead of cancel.
enabled: true
tags: [fetch, concurrency, crash, faulthandler]
created: 2026-05-22T11:53:26Z
updated: 2026-05-22T11:53:26Z
---

# Fetch race-to-N: drain in-flight, don't cancel

## Symptom
`munmap_chunk(): invalid pointer\nAborted (core dumped)` printed to stderr (NOT
to logs/error.log — Python `logging` only captures `logger.*` calls; libc
aborts go straight to fd 2). Process auto-restarts ~45s later, conversation
appears to hang. The Python traceback right above the abort line is usually a
red herring (e.g. an `SSLError` caught and recovered by `fetch/core.py:122`).

## Root cause
`lib/fetch/core.py::fetch_contents_for_results` previously used a "race-to-N"
pattern that called `Future.cancel()` on every still-pending future once
`ok_count >= target_ok` and broke out of the `as_completed` loop:

```python
for p in pending:
    p.cancel()
break
```

`Future.cancel()` is a no-op for *running* tasks. The `ThreadPoolExecutor`
context-manager exit then `wait=True`s on all running threads anyway —
those threads continue executing inside `requests`/`urllib3` `iter_content`,
holding live HTTP responses. Meanwhile the orchestrator pulls the next
search result, triggers lxml/trafilatura on a *successful* fetch, GC runs,
and a C-extension `__dealloc__` (zlib, urllib3 internal HTTPResponse, lxml)
trips on a half-state buffer → `munmap_chunk(): invalid pointer` → SIGABRT.

This is the same class of bug as the brotli `Pool is closed` crash.
Removing brotli (`Accept-Encoding: gzip, deflate`) fixed *one* trigger.
Mid-stream abandonment of `iter_content()` is the underlying pattern.

## Fix
**Drain, don't cancel.** Keep iterating `as_completed` until completion;
once `ok_count >= target_ok`, just stop *using* further results — but
let each thread finish its own `iter_content` and `resp.close()` in peace.

```python
target_reached_at = None
for fut in as_completed(futs, timeout=90):
    result, content, fetch_elapsed = fut.result()
    if target_reached_at is None:
        # accumulate
    if ok_count >= target_ok and target_reached_at is None:
        target_reached_at = time.time()  # log once, then quietly drain
```

## Diagnostic capture (keep enabled)
`server.py` arms `faulthandler.enable(file='logs/faulthandler.log',
all_threads=True)` before any other import. Any future SIGSEGV/SIGABRT/
heap-corruption abort dumps a per-thread Python traceback to that file
before the process dies. **First place to check** when the server
mysteriously restarts: `tail logs/faulthandler.log`.

## How to recognize this in the wild
- Old server PID is gone, new one started ~45s later (`ps -o lstart -p $PID`).
- Last lines in `logs/app.log` before a multi-second gap include `Race-to-N:
  got N/M pages in Xs, cancelling K slow fetches`.
- `logs/error.log` has a long traceback (often SSL or 403) immediately
  before the gap — but that traceback's exception class is always one
  the fetch code already catches and recovers from.

