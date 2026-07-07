---
name: frontend-unified-api-client
description: Frontend unified API client (static/js/api.js) — 25 domains, BASELINE = {}; URL builders for binary/HTML carve-outs
enabled: true
tags: [frontend, refactor, convention, javascript]
created: 2026-05-28T04:14:49Z
updated: 2026-07-01T00:00:00Z
---

# Frontend Unified API Client (`static/js/api.js`)

**Status (2026-05-29): ✅ MIGRATION COMPLETE.** All 18 JS files migrated.
`BASELINE = {}` in `tests/test_frontend_api_isolation.py`. Any new raw
`fetch('/api/...')` outside `api.js` now fails CI immediately.

See `docs/api_client.md` for the full playbook (kept up to date).

## Hard rule (steady state)

No JS file other than `api.js` may call `fetch('/api/...')` or
`fetch(apiUrl('/api/...'))` directly. Every backend HTTP call goes
through `window.Api.<domain>.<method>(...)`.

## Two guards (both must actually RUN in CI)

1. **Isolation ratchet** — `tests/test_frontend_api_isolation.py`
   (`BASELINE = {}`): only `api.js` may call `/api/` directly. Pure
   file-scan, no node/`server` needed.
2. **Contract test (2026-07-01)** — `tests/test_frontend_backend_contract.py`
   (`pytest.mark.unit`): every `/api/...` literal `api.js` calls MUST resolve
   to a registered route on the LIVE `server.app.url_map`. This replaces the
   old grep-only manual audit, which was **blind to factory-registered
   routes** (`register_task_routes(...)` mints `/run/poll/<id>` + `/run/abort/<id>`
   for orchestrations — a decorator grep flags them as dead; the `url_map`
   check sees them). Checks **184 client paths** vs **315 registered `/api`
   rule templates**.
   - **Normalisation**: strip the query string; collapse every dynamic segment
     — JS `${...}` OR Werkzeug `<conv_id>`/`<int:x>` — to a single `<*>`
     placeholder, so `${id}` ≟ `<int:msg_idx>` compares structurally.
     JS comments are stripped first so `api.js`'s own illustrative
     `fetch('/api/...')` docstring prose isn't scanned as a call.
   - **Scope (honest limit)**: this is **path-level**, not method-level — it
     asserts the PATH resolves, not that the HTTP verb matches. Verb coverage
     is confirmed separately (DELETE-only `del(...)` paths like
     `/api/v1/folders/<id>` and PATCH-only `patch(...)` paths like
     `/api/v1/conversations/<id>/settings` DO appear in the checked set), but a
     path called with the wrong verb would still pass. Add method-matching only
     if a verb-mismatch bug ever surfaces.
   - **Carve-out**: `/api/v1/trading*` — extracted to the external
     `tofu-trading` package, never registers in the vanilla app.
   - **Coverage cross-check** (`test_extractor_sees_every_api_call_site`): makes
     the "184 checked" count PROVABLY complete, not incidentally so. Scans every
     verb-wrapper (`get`/`post`/`put`/`patch`/`del`/`stream`/`request`) call
     site, extracts its FIRST ARG (depth-aware, so a concatenation is seen
     whole), and asserts `refs == inline` — where `refs` = call sites whose
     first arg references `/api` anywhere (literal OR concat) and `inline` = the
     subset that is a parseable inline `/api` literal (the only shape
     `_extract_client_paths` captures). A future concat/variable-built call site
     that embeds a literal (`BASE + '/api/x'`) bumps `refs` not `inline` → fails
     loudly instead of the path silently dropping out of the checked set.
     Baseline `210 == 210`. NC proven: injecting `post(_PREFIX + '/api/v1/ghost')`
     → `refs>inline` → fails. Honest boundary: a fully-dynamic call site with NO
     `/api` substring (`get(BASE + p)`) is undetectable by any static scan and
     not counted — that IS the trading carve-out shape; a future domain using it
     needs its own coverage (enumerate suffixes).
   - Caught a real dead endpoint on introduction: `pdf.vlmPoll` hit
     `/api/pdf/vlm-parse/<id>` but the poll route is registered only under
     `/api/v1/pdf/vlm-parse/<id>` (the START route is non-v1, the POLL route is
     v1 — a per-verb asymmetry). Fixed the client path.

### CI wiring (the trap: a guard that skips is theatre)

Unlike the jsdom `test_frontend_*.py` harnesses (which gate on node and
therefore **SKIP** in the Python-only `test` job — see
`.tofu/skills/frontend-jsdom-test-mechanism-and-ci-gap.md`), the contract test
has **no node/jsdom gate** — it only needs `server.app` to import (route
registration is import-time; the ephemeral-SQLite `no such table` log noise is
non-fatal). So it genuinely RUNS in BOTH CI jobs:
- `test` job (`pytest -ra`, Python matrix, no node) — runs + passes (proven:
  `2 passed`, 0 skipped).
- `frontend` job (`pytest tests/test_frontend_*.py -ra`, node + `pip install
  -e .[test]`) — the `test_frontend_*` glob also matches it, and it has the
  Python deps to import `server`.
Verify on any change: `pytest tests/test_frontend_backend_contract.py -ra`
must report `passed`, never `skipped`/`no tests ran`.

## Public surface (25 domains)

```js
Api.request / get / post / put / patch / del / stream  // low level
Api.ApiError                                           // {status, code, body, url}

// domains
Api.folders        // list/create/update/remove
Api.memory         // list/create/remove/toggle/files/install/catalog/catalogInstall
Api.timer          // list/trigger/cancel/status
Api.scheduler      // proactiveStatus/triggerTask/pauseTask/resumeTask/pollLog
Api.optimizer      // proposals/approve/reject/revert/runNow
Api.agentBackends  // status
Api.compactions    // list/get
Api.conversations  // get/getResponse/patchSettings/put/getDebugMessages/
                   // deleteBranch/remove/search/patchMessage/deleteMessage/
                   // extractFileChanges
Api.text           // detectLanguage
Api.translate      // run/start/poll/pollBatch/mtTest
Api.chat           // send/regenerate/continue/branchStart/abortTask/abortConv/
                   // queueGet/queueRemove/queueClear/active/activeResponse/
                   // patchToolState/sendTranslateStatus/poll/streamResponse/
                   // stdinResponse/humanResponse
Api.images         // generate/models/upload
Api.pdf            // parse/vlmStart/vlmPoll/vlmTasks
Api.doc            // parse
Api.artifacts      // meta/versions/pin/remove/library/forConv/scan/contentText
                   // + URL builders: rawUrl/viewUrl/exportPdfUrl
Api.health         // check (Response)/info (JSON)
Api.pricing        // get
Api.clientError    // report
Api.serverConfig   // get/update
Api.browser        // status
Api.project        // status/setPaths/setPath/clear/recentList/recentSave/
                   // recentClear/rescan/undo/undoAll/browse/write/writeApproval
Api.daily          // calendar/status/convCount/generate/taskCreate/taskDelete/
                   // taskStatus/todoToggle/inheritedTodoToggle/inheritedTodoDelete
Api.paper          // libraryList/libraryUpsert/libraryDelete/upload/
                   // fetchArxivStream/reparse/chatStream/reportStart/reportPoll/
                   // reportLookup/reportCache/reportAbort/translateStart/
                   // translatePoll/translateAbort/translateCache
Api.features       // set
Api.providers      // templates/probe/probeBulk/balance/discoverModels/updateTemplate
Api.dispatch       // endpointMetrics/keyStats/keyOverride
Api.oauth          // status/loginPost/loginGet/logoutPost/logoutGet/
                   // callbackPost/callbackGet  (POST→GET fallback for proxies)
Api.mcp            // catalogList/catalogInstall/catalogUninstall/connectAll/
                   // connectOne/serverCreate
```

## Verb helpers

`Api.{get,post,put,patch,del}` accept `{onError: 'null'}` to convert
failure to `null` + `console.warn` instead of throwing. The low-level
`Api.request()` adds `parse: 'response'` for callers that need to
inspect HTTP status / parse the body themselves (the SSE chat stream,
status-aware retry loops, etc.).

## URL-builder pattern (carve-outs)

For endpoints that ship typed binary or sandboxed HTML with custom
Content-Disposition / CSP headers (artifact `/raw`, `/view`, `/export`
PDF), the domain method returns an absolute URL string instead of a
fetch promise. Consumers set `iframe.src` / `anchor.href` from these
without bypassing the unified seam:

```js
iframe.src = Api.artifacts.viewUrl(meta.id);
anchor.href = Api.artifacts.exportPdfUrl(_activeId);
```

Use `Api._resolve(path)` to build the absolute URL — same logic the
verb helpers use.

## Streaming carve-outs

- `pushSubscribe(channel, taskId, fn)` from `push.js` for server-push
  events. Don't poll.
- SSE chat stream: `Api.chat.streamResponse(taskId, {signal})` returns
  a Response; caller pipes `.body.getReader()`. Same for
  `Api.paper.fetchArxivStream` / `Api.paper.chatStream`.

## Mapping legacy → /api/v1

When a v1 endpoint is feature-complete, switch the domain method's URL
inside `api.js` — callers stay identical:

```js
// before
list: () => get('/api/foo'),
// after
list: () => get('/api/v1/foo'),
```

## Bundle wiring (already done)

- `lib/js_bundler.py` — `api.js` listed right after `core.js`.
- `index.html` — `<script defer src="static/js/api.js?v=...">`
  right after `core.js`.

## Migration history

- 2026-05-28 PR-1..9: foundation + 24 domains migrated through `Api.*`.
- 2026-05-29 (commit 15-cleanup): added `Api.artifacts` domain with
  URL builders for the binary/HTML carve-outs. `static/js/artifacts.js`
  ditched its 9 raw `apiUrl()` fetches in favour of the unified seam.

