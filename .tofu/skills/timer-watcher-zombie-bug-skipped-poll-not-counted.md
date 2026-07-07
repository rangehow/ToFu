---
name: timer-watcher-zombie-bug-skipped-poll-not-counted
description: FIXED 2026-06-26: zombie Timer Watchers flooded web_search after restart — leaked TEST timer_watchers rows (conv-parsefail) resurrected by resume_active_timers; 4-layer fix in lib/scheduler/timer.py
enabled: true
tags: [bug, timer, scheduler, zombie-task, fixed, search-storm, test-leak]
created: 2026-04-20T05:30:23Z
updated: 2026-06-26T14:24:34Z
---

## Symptom
After a server restart the web-VS-Code terminal dumped a backlog of `web_search` calls the user NEVER issued — nonsense queries like `query='2026年6月26日 杭州是否已完成电动自行车备案'`, `'2026 FIFA World Cup host country'`. Searcher thread name = `timer-poll-tmr_<id>` (the Timer Watcher poll agent), NOT a user chat.

## Root-cause chain (verified in logs + DB)
40 `status='active'` timers in the DB, ALL `conv_id='conv-parsefail'` / `source_task_id='task-x'` / instruction `'Is the run finished?'` = verbatim `tests/test_timer_parse_failure.py::_make_timer()`. → a TEST leaked active rows into the PROD DB (ambient `TOFU_DB_BACKEND=postgres` defeats conftest SQLite setdefault; conftest purge covered only `conversations`). Amplifiers:
1. `_build_poll_tools` defaulted `searchMode='multi'` even for `tools_config={}` → every poll agent got web_search; ungrounded "is it done?" → cheap model hallucinates a query.
2. `resume_active_timers()` re-spawned EVERY active row each boot, no age/concurrency cap → 40×10s polls at once (search handler hit 60–78s SLOW).
3. Skipped polls (unchanged check_command) didn't increment poll_count → never reach max_polls → immortal.
4. `no such table: timer_watchers` races + `event_id=0` collisions.

## Fix (lib/scheduler/timer.py + tests/conftest.py, all 4 layers)
- **Origin:** `test_timer_parse_failure.py` autouse teardown DELETEs `conv_id='conv-parsefail'`; conftest `_purge_test_timers()` (gated to `conv-parsefail`/`test-conv%`/`source_task_id='task-x'`) wired into session start+end purge.
- **Resume guard:** `resume_active_timers()` pass1 age-sweeps active timers older than `max(24h, poll_interval*max_polls*1.5)` → new `status='expired'` (`_mark_expired`); pass2 caps respawns at `TOFU_TIMER_RESUME_CAP` (def 20). Floor tunable `TOFU_TIMER_MAX_AGE_HOURS`.
- **No-search default:** `_build_poll_tools` — `searchMode` default `''`, `fetchEnabled` default `False`. Bare watcher gets read_files only. BEHAVIOR CHANGE: timers needing web must set searchMode/fetchEnabled in tools_config.
- **Skipped increments:** skip branch in `start_timer_loop` now calls `_increment_poll_count(tid,'skipped',…)`.

## Guardrail
Tests: `tests/test_timer_resume_guardrails.py` (7) + `test_timer_parse_failure.py` (5). Any test calling real `create_timer` MUST clean up its rows — they leak into prod and resurrect on restart. Live cleanup of zombies: loop `cancel_timer` over `list_active_timers()`.

