---
name: fetch-pool-closed-crash-root-cause
description: Two distinct fetch-pool crashes: (1) brotli C-decompressor heap corruption (FIXED via gzip-only); (2) lxml/libxml2 text_content thread-unsafety (FIXED 2026-05-23 via _TRAFILATURA_LOCK)
enabled: true
tags: [fetch, crash, threading, urllib3, brotli, trafilatura, lxml]
created: 2026-05-21T15:44:23Z
updated: 2026-05-23T15:10:47Z
---

# Fetch Pipeline Crashes: Two Distinct Native Failure Modes

## Mode 1 — `Pool is closed` + `free()/munmap_chunk(): invalid pointer`
**Root cause**: brotli C extension (`_brotli.Decompressor`) heap corruption.
When a streaming response is interrupted mid-`iter_content()` (pool eviction,
timeout, server reset), the brotli Decompressor is left in an inconsistent
state. Python's GC later runs `__dealloc__` → double-free → SIGABRT.
The pool eviction (`psf/requests#1871`) is the trigger, brotli is the crash vector.

**Fix applied (final, verified)**:
1. Remove brotli from Accept-Encoding — `'gzip, deflate'` only in `lib/fetch/utils.py:93`
   and `lib/fetch/http.py:119`. gzip/deflate use Python's `zlib` (GIL-safe).
2. `pool_connections=100, pool_maxsize=100` — eliminates LRU pool eviction.
3. `except Exception: resp.close(); raise` in `do_request()` — always close response
   on mid-stream errors so any decompressor state is cleaned up before GC.
4. Skip circuit-breaker for "Pool is closed" — client-side, not server fault.

## Mode 2 — Segfault in `lxml.html.text_content`  (FIXED 2026-05-23)
**Symptom**: faulthandler dump shows `Fatal Python error: Segmentation fault`
with current thread + 2+ other threads all inside `lxml/html/__init__.py:400 in text_content`,
called via `trafilatura.delete_by_link_density()` → `prune_unwanted_sections()` → `extract()`.

**Root cause**: lxml 6.1.1 / libxml2 2.14.6 are NOT thread-safe in `text_content()`.
The previous claim "lxml 6.1.0 has internal Cython locks, stress-tested 160 concurrent calls"
was **wrong** for 6.1.1. Under real load (`search/orchestrator.py` runs `fetch_pool` with
`max_workers=16`, and `streaming_tool_executor` runs up to 5 concurrent `web_search`
queries → ~80 concurrent extractions), libxml2 corrupts native state and crashes.

**Fix applied** in `lib/fetch/html_extract.py`:
- Module-level `_TRAFILATURA_LOCK = threading.Lock()`.
- Wrap the `trafilatura.extract(html, ...)` call inside `with _TRAFILATURA_LOCK:`.
- BS4 fallback NOT locked — uses Python's `html.parser` (pure-Python, GIL-safe).
- Throughput impact negligible — extract() is ~tens of ms; the fetch pool is
  network-I/O dominated.

## Diagnostic Signature for Mode 2
- Last business log line is normal fetch/search activity.
- `logs/faulthandler.log` shows `Fatal Python error: Segmentation fault` and the
  `Current thread` traceback ends in `trafilatura/core.py … extract` →
  `lib/fetch/html_extract.py … extract_html_text`.
- Other thread tracebacks include `lxml/html/__init__.py … text_content` and
  `trafilatura/htmlprocessing.py … link_density_test` / `delete_by_link_density`.

## Environment
Python 3.12.13, lxml 6.1.1, libxml2 2.14.6, trafilatura 2.0.0, urllib3 1.26.7, brotli 1.2.0

