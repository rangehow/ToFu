---
name: frontend-unified-api-client
description: Frontend unified API client (static/js/api.js) — 25 domains, BASELINE = {}; URL builders for binary/HTML carve-outs
enabled: true
tags: [frontend, refactor, convention, javascript]
created: 2026-05-28T04:14:49Z
updated: 2026-05-29T03:57:54Z
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

