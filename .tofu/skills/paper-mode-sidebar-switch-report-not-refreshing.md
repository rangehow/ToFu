---
name: paper-mode-sidebar-switch-report-not-refreshing
description: Bug fix: clicking a paper in sidebar didn't refresh the Report/QA panels — stale content from previous paper remained
enabled: true
tags: [bug-fix, paper-mode, sidebar, report, frontend]
created: 2026-04-17T09:42:38Z
updated: 2026-04-17T09:42:38Z
---

# Paper Mode: Sidebar Switch Must Refresh Report Panel

## Symptom
In Paper Reading Mode, after viewing paper A's Report tab and then clicking paper B in the
left sidebar, the PDF on the left switched correctly but the Report area on the right kept
showing paper A's rendered markdown.

## Root cause (`static/js/paper-reader.js` → `_openPaperEntry` + `_switchPaperTab`)
`_openPaperEntry()` cleared in-memory state (`_paperReportCache = ''`) but:
1. Never blanked the DOM inside `#paperReportContent` / `#paperQAMessages` — old rendered
   markdown stayed visible until an async server fetch eventually replaced it.
2. Did not abort `_paperReportAbort` / `_paperQAAbort` — an in-flight stream from paper A
   could keep writing into the container AFTER the switch.
3. `_switchPaperTab('report')` had no `else` branch for "no parsed text AND no hash", so
   opening a fresh library entry (where parsedText/hash hadn't been populated yet) left
   the prior paper's report on screen indefinitely.

## Fix
In `_openPaperEntry`:
- Abort both `_paperReportAbort` and `_paperQAAbort` before swapping state.
- Blank `#paperReportContent` (show loading spinner) and `#paperQAMessages` immediately.

In `_switchPaperTab('report')`:
- Add an `else` branch that writes the empty-state placeholder into `#paperReportContent`
  when there's no `_paperReportCache`, `_paperParsedText`, nor `_paperHash`.

## Rule of thumb
Whenever a "mode"/entity switch shares the same DOM container across entities, the switch
handler MUST (a) abort any in-flight streams targeting that container and (b) reset the
container's innerHTML synchronously — don't rely on the async load path to overwrite it.

