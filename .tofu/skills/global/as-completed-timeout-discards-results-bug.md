---
name: as-completed-timeout-discards-results-bug
description: Bug pattern: Python concurrent.futures.as_completed(timeout=N) raises TimeoutError that discards already-collected results from completed futures — must catch TimeoutError outside the for loop to keep partial results
enabled: true
tags: [python, concurrent.futures, as_completed, TimeoutError, bug-pattern, data-loss]
created: 2026-03-31T04:44:16Z
updated: 2026-04-08T08:18:42Z
---

# `as_completed()` TimeoutError discards all results

## The Bug

Python's `concurrent.futures.as_completed(futs, timeout=N)` raises `TimeoutError`
when the deadline expires while *any* futures are still pending. The exception is
raised **inside the `for` loop iteration** — meaning if it propagates uncaught,
all results already collected from completed futures are lost.

```python
# ❌ BUG: TimeoutError propagates, discards results from fast engines
for fut in as_completed(futs, timeout=20):
    results.append(fut.result())
# If 1 of 5 futures is slow, ALL 4 good results are lost
```

## The Fix

Wrap the `for` loop in `try/except TimeoutError`:

```python
# ✅ CORRECT: catch timeout, keep partial results
try:
    for fut in as_completed(futs, timeout=20):
        results.append(fut.result())
except TimeoutError:
    timed_out = [futs[f] for f in futs if not f.done()]
    logger.warning('Timeout: %d futures still pending, keeping %d results',
                   len(timed_out), len(results))
```

## Key Details

- In Python 3.x, `concurrent.futures.TimeoutError` IS the builtin `TimeoutError` — no special import needed.
- The exception message is `"N (of M) futures unfinished"`.
- The `for` loop has already yielded completed futures before the timeout — results appended before the exception are safe.
- Always log which futures timed out for debugging.

## Real Example (chatui)

In `lib/search/orchestrator.py`, 5 search engines run in parallel. When DDG-HTML
hit a slow 202 retry (>20s), `as_completed()` raised `TimeoutError`, which propagated
to `executor.py`'s generic `except Exception` handler, logging as
`"web_search failed: 1 (of 5) futures unfinished"` and returning 0 results —
even though Brave/Bing/SearXNG had all returned good results.
