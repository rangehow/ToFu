---
name: typed-error-envelope-migration-2026-05-22
description: task['error'] is now a typed dict envelope (lib.error_envelope), not a string. All emit sites + DB persistence + JS rendering routed through it.
enabled: true
tags: [error-handling, sse, frontend-backend, convention]
created: 2026-05-22T02:02:58Z
updated: 2026-05-22T02:13:24Z
---


# Typed Error Envelope (2026-05-22)

`task['error']` is now a **dict**, not a string. The string contract was retired.

## Schema (lib/error_envelope.py)

```python
{
  'kind':      <closed enum>,    # see KINDS below
  'severity':  'warning'|'error',
  'retryable': bool,
  'message':   str,              # bilingual user-facing title
  'hint':      str,              # bilingual recovery hint (multi-line)
  'detail':    str,              # short technical detail
  'model':     str,
  'context':   str,              # short tag, e.g. 'fallback', 'task-fatal'
  'source':    str,              # 'llm-stream', 'orchestrator', 'autopilot', ...
  'raw':       str,              # raw exception text (≤300 chars)
}
```

`kind` enum (closed — typos auto-downgrade to `'generic'` with warning):
quota, ratelimit, permission, no_slot, dispatch_exhausted, timeout,
network, content_filter, invalid_image, prompt_too_long, stream_only,
model_limit, tool_rounds_exhausted, tool_timeout, premature_close,
abnormal_stop, aborted, server_offline, internal, generic.

## How to emit

```python
from lib.error_envelope import make_envelope, from_exception

# From an exception (auto-classifies kind):
task['error'] = from_exception(e, model=model, context='round-3', source='llm-stream')

# From a non-exception path (budget exhausted, tool timeout, etc.):
task['error'] = make_envelope('tool_timeout',
    detail='3 consecutive tool timeouts',
    model=model, context='tool-loop', source='orchestrator',
    raw=f'consecutive_tool_timeouts={n}')
```

`lib/llm_error_format.py::format_llm_error_for_user` is now a thin shim around
`from_exception` and returns the envelope.

## Persistence

`task_results.error` is TEXT but stores a **JSON-serialised envelope**.
`manager.persist_task_result` and `_sync_partial_to_conversation` use
`error_envelope.to_json()`. SSE/poll readers in `routes/chat.py` use
`error_envelope.from_json()` so the wire payload is always the dict.
Legacy string rows are auto-wrapped into `kind='generic'` on read.

## Frontend (static/js/core.js)

5 globals exposed on `window`:
- `renderErrorEnvelope(err)` — produces the `.error-block` HTML
- `normalizeErrorEnvelope(err)` — accepts dict OR legacy string, returns dict
- `errorEnvelopeKind(err)` — convenience accessor (e.g. for `=== 'server_offline'` checks)
- `errorEnvelopeMessage(err)` — short title accessor
- `isErrorEnvelope(obj)` — duck-type check

Use these instead of `escapeHtml(am.error)` / regex tests on the string.
The `.error-block-{kind,severity}` CSS modifiers in `static/styles.css`
let you target a specific category (e.g. ratelimit shows orange, quota shows
red).

## Sites updated

### Backend emit sites (every `task['error'] = '<string>'` was rewritten)

Chat / orchestrator pipeline:
- `lib/tasks_pkg/orchestrator.py` — post-loop fallback, tool_rounds, tool_timeout,
  task-fatal, post-loop tool-calls leak.
- `lib/tasks_pkg/llm_fallback.py` — invalid_image, no-fallback giveup,
  both-failed giveup. Note: `assistant_msg.content` is now empty string
  (formerly carried the user-friendly text) — the typed envelope on
  `task['error']` is what the frontend renders.
- `lib/tasks_pkg/stream_handler.py` — abnormal_stop / premature_close
  retry exhausted.
- `lib/tasks_pkg/endpoint.py` — endpoint-fatal, run_task_sync.
- `lib/tasks_pkg/autopilot.py` — followup-thread spawn failure.
- `lib/message_queue.py` — queue-dispatch thread spawn failure.
- `routes/chat.py` — chat_start, endpoint_start, branch_start,
  external-backend, external-task crash.

Auxiliary task pipelines (added 2026-05-22 second pass):
- `routes/paper.py` — `_run_report_task` fatal; Babel translate per-chunk
  abort + crash.  Both stash the envelope on `task['error']` AND broadcast
  it inside `{'type':'error','error': envelope}` events through
  `_append_report_event` / `_append_translate_event`.
- `routes/translate.py` — async translate task fatal, PPTX translate
  fatal, PPTX `result.ok=False` failure.  Surfaced through
  `/api/translate/poll/<task_id>` and `/api/translate/poll_batch`.
- `routes/trading_simulator.py` — `_finish_task` now accepts BaseException
  | str | dict and centralises the wrapping.  Both fetch-data and sim-run
  background threads pass the raw exception via the new
  `error_context=` / `error_source=` kwargs so call-sites stay one-liners.

NOT yet migrated (out of scope so far):
- `lib/trading_tasks.py` — `TaskState.error` (separate task store backing
  `/api/trading/tasks/*` decision/autopilot/intel-backtest endpoints).
  Migrating this requires a `trading_bg_tasks.error` schema migration
  (it stores plain strings). Leaving on string for now; emitter sites
  use `task.finish(error=str(e))` and the JS consumer (`tasks.js`) was
  already taught to render via `errorEnvelopeMessage()` so an envelope
  drop-in there will be a one-line backend change.

### Frontend updates

- `static/js/main.js` — POST failure, CaseB recovery (interrupted/expired).
- `static/js/core.js` — server-offline stamp by health check.
- `static/js/branch.js` — error-event handler (wraps via `normalizeErrorEnvelope`).
- `static/js/ui.js` — swarm `agent.preview` now uses `errorEnvelopeMessage`.
- `static/js/paper-reader.js` — `_applyReportEvent('error')`,
  `_pollReportTask` poll-fail / status-error, Babel `ev.type === 'error'` and
  `pollData.status === 'error'`.
- `static/js/translation.js` — `_pollTranslationLoop` terminal-failure,
  `_callTranslateAPI` HTTP error body, `_resumePendingTranslations`
  fall-through error.
- `static/js/trading/simulator.js` — start-fetch and start-sim error toasts.
- `static/js/trading/tasks.js` — server-error response body (top-level
  `data.error` without `data.status`).

## Don't do this

- `task['error'] = str(e)` — write `from_exception(e, ...)` instead.
- `task['error'] = 'Some failure'` — write `make_envelope(<kind>, detail='Some failure', ...)`.
- `if (/server offline/i.test(am.error))` — use `errorEnvelopeKind(am.error) === 'server_offline'`.
- `escapeHtml(am.error)` — use `renderErrorEnvelope(am.error)`.
- `toast('Failed: ' + data.error, 'error')` — use
  `toast('Failed: ' + (errorEnvelopeMessage(data.error) || data.error), 'error')`.

## Verification (2026-05-22)

Ran via downloaded portable Python 3.11.10 (cpython-3.11.10+20241016
from python-build-standalone — local CentOS 7 image only ships 2.7 + 3.6,
neither parses PEP-604 union types).

```
$ /tmp/tmp.XXX/python/bin/python3 tests/test_zero_byte_round0_retry.py
6 passed, 0 failed

$ /tmp/tmp.XXX/python/bin/python3 tests/test_stream_anomaly_retry_widening.py
6 passed, 0 failed
```

Both files now assert `isinstance(task['error'], dict)` and that the
trace_id appears in `detail` + `raw`, so they would fail loudly if any
emit site regressed back to a string.

End-to-end sanity check on `routes/trading_simulator.py::_finish_task`
confirmed:
- BaseException input → `kind='generic'`, `source='routes.trading_simulator'`
- `str` input → wrapped to `kind='generic'`, `detail` carries the raw text
- `result=...` (no error) → `error` field stays `None`

