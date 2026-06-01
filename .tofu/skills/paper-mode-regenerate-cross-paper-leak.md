---
name: paper-mode-regenerate-cross-paper-leak
description: Bug: Regenerate on paper A, switch to paper B mid-await, A's report saves onto B
enabled: true
tags: [paper-mode, race-condition, javascript]
created: 2026-05-20T08:31:13Z
updated: 2026-05-20T08:31:13Z
---

# Paper Mode: Cross-paper report leak via mid-await switch

## Symptom
User clicked Regenerate on paper A in Reading Mode, then quickly switched to
paper B in the sidebar. Paper B's Report tab eventually showed paper A's
report (and persisted it onto B's library entry).

## Root cause (`static/js/paper-reader.js`)
`_generatePaperReport(force=true)` is async. Between the
`await fetch('/api/paper/report/start')` and the line
`_paperReportStream = _makeReportStreamState(_activePaperId, ..., data.task_id)`,
the user can click another paper, flipping `_activePaperId` to B. The new
stream state then binds paper A's `task_id` to paper B's id. Polling writes
the result into the global `_paperReportCache` and calls
`_saveActivePaperState()` — which saves under whatever paper is *currently*
active (B), so B's library entry is overwritten with A's report.

Two more leak sites compounded this:
- `_applyReportEvent` writes `_paperReportCache` / `_paperHash` directly on
  `done` / `enriched` events without checking that the stream still belongs
  to the active paper.
- `_pollReportTask` writes `_paperReportCache = data.report` and calls
  `_saveActivePaperState()` on the terminal `done` branch, again without an
  active-paper guard.

`_loadOrGenerateReport` had the same shape of bug across its three async
fallbacks (lookup → cache → start).

## Fix
1. In `_generatePaperReport` and `_loadOrGenerateReport`, snapshot
   `var startPaperId = _activePaperId;` at the top, and after every `await`
   bail with `if (_activePaperId !== startPaperId) return;`.
2. Build the new `_paperReportStream` with `startPaperId` (NOT the
   possibly-stale-now `_activePaperId`).
3. In `_applyReportEvent` and `_pollReportTask`, only mutate the global
   `_paperReportCache` / `_paperHash` and call `_saveActivePaperState()`
   when `s.paperId === _activePaperId`. The stream's own `s.fullText`
   keeps accumulating regardless, so a background-paper task is still
   visible if the user navigates back to it.

## Rule of thumb
For Paper Reading Mode, ANY async function that mutates global state
keyed off `_activePaperId` must capture it at entry and re-check after
each `await`. Treat `_paperReportCache` / `_paperHash` /
`_saveActivePaperState()` as guarded writes — never call them from a
poll/event handler without confirming the stream's `paperId` still
matches the active paper.

