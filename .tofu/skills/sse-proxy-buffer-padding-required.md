---
name: sse-proxy-buffer-padding-required
description: Flask SSE endpoints need ~4KB padding + no-transform/identity headers to defeat VSCode port-forward / nginx proxy buffering — otherwise small events appear stuck
enabled: true
tags: [python, flask, sse, streaming, proxy, buffering, vscode, padding]
created: 2026-04-17T10:06:53Z
updated: 2026-04-17T10:06:53Z
---

# SSE Proxy Buffer Padding — Required Pattern

## Symptom
Flask SSE endpoints (e.g. arXiv fetch progress, report generation, trading brain)
appear "stuck" — user sees only the initial placeholder ("Parsing…", "Generating…"),
then events arrive in a big batch near the end. The Python side is yielding
correctly, but the proxy in between (VSCode port-forwarding, nginx, gateway)
buffers small chunks until a threshold is reached.

## Root cause
Even with `Cache-Control: no-cache` and `X-Accel-Buffering: no`, a proxy that
terminates/decodes HTTP/1.1 chunked transfer still applies its own write buffer
(often 4-8KB) before flushing. First bytes trigger a larger flush, then
subsequent small events trickle through — but the user-perceived "first signal"
is delayed by many seconds.

## Fix — 3 parts
1. **Yield ~4KB padding as SSE comments** at the very top of the generator,
   BEFORE any real events:
   ```python
   def generate():
       yield ':' + (' ' * 2048) + '\n\n'
       yield ':' + (' ' * 2048) + '\n\n'
       # ... real events
   ```
   Lines starting with `:` are SSE comments (ignored by EventSource/reader).

2. **Add `no-transform`** to Cache-Control so proxies don't re-chunk:
   ```
   'Cache-Control': 'no-cache, no-transform'
   ```

3. **Set `Content-Encoding: identity`** (belt-and-suspenders — flask-compress
   already skips `text/event-stream` in server.py, but explicit is safer):
   ```
   'Content-Encoding': 'identity'
   ```

## Reference implementations
- `routes/trading_brain.py` `/api/trading/brain/stream` — canonical pattern (4×2048 padding)
- `routes/paper.py` `/api/paper/fetch-arxiv-stream` — fixed 2026-04-17
- `routes/paper.py` `/api/paper/report` — fixed 2026-04-17

## When NOT needed
Endpoints that immediately stream large content (e.g. `/api/chat/stream` whose
first events include sizable init payloads) can work without padding — but it's
cheap and harmless insurance. Add it whenever early events are small (<500B each).

## How to diagnose
Compare timing of events server-side (logs) vs client-side (Network tab "timing"
or `console.log` in the SSE reader). If server logs show events every 100ms but
client batches them every 2-5 seconds, it's proxy buffering → needs padding.

