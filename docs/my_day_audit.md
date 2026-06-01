# My Day — Audit & V2 Design

_Scope: `routes/daily_report.py` (2339 LOC), `static/js/myday.js` (1288 LOC),
`daily_cost_cache` table. Audit date: 2026-04-30. Closes gap flagged in
`docs/DEVELOPMENT_DIRECTION.md §2.1.4`._

## Findings

### F1 · calendar N+1 parse loop  [perf]
`routes/daily_report.py:1513-1434` (`get_calendar_month`): `os.listdir(_REPORTS_DIR)`
then `_load_report()` (open+json.load) for every `YYYY-MM-*.json` file.
Cost grows linearly with months kept and is not covered by `_calendar_cache`
(only the DB-derived `conv_days`/`cost_days` are cached).
**Fix sketch:** include the parsed report summaries inside `_calendar_cache[key]`
so a warm month serves from memory; long-term move to a `daily_reports` table.

### F2 · duplicate month DB scan  [perf]
`routes/daily_report.py:1556-1595` scans the whole-month conversations range
just to count `conv_days`, while `_get_monthly_costs()` (called right after)
does its own range scan via `_scan_costs_in_range`. Two passes over the same
row set for the current month.
**Fix sketch:** let `_scan_costs_in_range` also return per-day activity counts,
or compute `conv_days` from the scan side-effects inside `_get_monthly_costs`.

### F3 · yesterday report loaded 3× per generation  [perf / code-health]
`_get_yesterday_carryover` (line 750), `_get_today_inherited_todos` (776),
`_get_yesterday_todo_accountability` (810) each call `_load_report(yesterday)`
independently during a single `_analyse_conversations` run (line 951+).
**Fix sketch:** single `_load_day_context(date)` helper; pass the dict through.

### F4 · yesterday report written 2× per generation  [perf / correctness]
`_mark_yesterday_todos_done` (831) and `_close_yesterday_remaining_todos` (894)
each call `_save_report(yesterday, ...)`. Two disk writes + two
`_calendar_cache` invalidations back-to-back.
**Fix sketch:** compute all mutations first, `_save_report` once at the end.

### F5 · fuzzy_todo_match threshold 0.35  [correctness — §10 gated]
`routes/daily_report.py:703` — character-set Jaccard **or** LCS ratio ≥ 0.35
is enough to mark TODO A ≡ B. With short CJK strings (≤ 6 chars), 0.35 is
very lax: e.g. "修复图片" vs "修复bug" share {修, 复} out of union size 5 →
jaccard 0.40, auto-match. Risk: false "done" write-backs.
**Fix sketch:** raise threshold and/or require length-normalised LCS with
min-length gate. **Do not change in this pass** — §10.1 requires user approval.

### F6 · scheduler cadence ignores local midnight  [correctness — §10 gated]
`routes/daily_report.py:2144-2156` sleeps a flat `6*3600` seconds after an
initial 60 s delay. If the server boots at 22:58, the next backfill tick fires
at ~05:00 next day — fine — but at 00:30 it fires at 06:30 (first miss window).
No midnight alignment, no jitter, no DST awareness.
**Fix sketch:** compute seconds-until-next (local midnight + 5 min) and sleep
to that; after first tick, revert to 24 h cadence. **§10 gated.**

### F7 · `_active_jobs` lost on restart  [correctness]
`routes/daily_report.py:41-42` — in-process dict. If the server crashes mid-
generation, the frontend polls `/status/<date>` forever and gets `idle`
(no job, no report). No recovery from a sentinel file.
**Fix sketch:** on generation start, drop a `<date>.lock` file in reports dir;
on startup, sweep orphan locks and set `_active_jobs[date]={status:'error',
error:'interrupted'}` for ~1 poll cycle so the frontend can show a retry.

### F8 · progress messages are hard-coded Chinese  [UX / code-health]
`routes/daily_report.py:1924+` emits `'正在扫描对话…'`, `'LLM 分析 N 个对话…'`,
`'保存报告…'` as progress strings. `static/js/myday.js:325-378` then re-derives
localized messages from `stage` alone, ignoring the server's literal text.
**Fix sketch:** server emits `{stage, n_total, n_current}` only; client renders
via i18n keys `myday.stageExtracting`, etc. No i18n on the server.

### F9 · filesystem-only report storage  [perf / missing-feature]
`routes/daily_report.py:75-103` — one JSON file per day under
`data/config/daily_reports/`. No index; cross-day search is impossible
without scanning every file. No concurrent-safe writes (last-writer-wins).
**Fix sketch:** add `daily_reports(user_id, date, body_json, updated_at)` with
`(user_id,date)` PK; keep JSON files as migration source / export artifact.

### F10 · no dedicated tests  [code-health]
`routes/daily_report.py` — 2339 LOC, zero matching file under `tests/` or
`debug/test_daily_report*`. Regressions in fuzzy matching, carry-over, or
the cost cache get discovered manually.
**Fix sketch:** add `tests/test_daily_report.py` covering (a) fuzzy-match
boundary cases, (b) carry-over round-trip, (c) `_scan_costs_in_range` date
filter, (d) calendar endpoint response shape (PG + SQLite).

### F11 · frontend polls at fixed 1.5 s  [UX — §10 gated]
`static/js/myday.js:383` `INTERVAL = 1500`. During the `starting`/`extracting`
phase the payload is ~200 B of JSON, ×1 per 1.5 s — fine. During idle (no
job running) the same interval still fires until the tab is closed.
**Fix sketch:** switch to SSE `/api/daily-report/stream/<date>`; the server
pushes stage updates and a terminal `done` event. Fall back to polling only
on SSE error. **§10 gated (interval).**

### F12 · no multi-day rollup / search / export  [missing-feature]
Endpoints only return single-day payloads. Users can't get a week digest,
search across days, or export a Markdown summary. `routes/daily_report.py`
exposes `/api/daily-report/<date>`, `/calendar/<y>/<m>`, `/task`, etc. — no
`/digest/week/<start>` or `/search?q=...` or `/export/<date>.md`.
**Fix sketch:** new read-only endpoints; digest reuses `_analyse_conversations`
with week-level prompt; search is a `SELECT ... FROM daily_reports WHERE ...`
after F9 lands; export is a deterministic Markdown templater.

### F13 · smart reminder is time-based only  [UX]
`static/js/myday.js:1236-1286` — reminder fires 3 h after page load if hour
≥ 14 and ≥3 conversations today. It does not know if the user already
viewed today's report, and it stores only the last-shown date.
**Fix sketch:** consider (a) skip if a report for today exists and streams ≥ 1,
(b) adaptive hour based on user's most-active window (derivable from
`daily_cost_cache`), (c) open modal on click rather than just toast.

### F14 · conv_id claim-back may lose unclaimed groups of 1  [correctness]
`routes/daily_report.py:1088-1102`: if `len(unclaimed) < 2`, unclaimed
conversations are appended to `final_streams[-1]` without regard to topic.
If `final_streams` is empty (rare), the single unclaimed conv is silently
dropped. Edge case but surfaces when the LLM returns zero valid streams.
**Fix sketch:** guard `if final_streams:` (already present) — also emit a
"Ungrouped" stream when the list is empty so the conv is never lost.

---

## V2 Design

Redesign goal: move from filesystem JSON + in-process job dict to a
DB-backed, SSE-streamed, i18n-clean module with rollup + export.

### (a) `daily_reports` DB table
Schema (both backends — CLAUDE.md §10.3 gated):

```
daily_reports(
  user_id    TEXT    NOT NULL,
  date       TEXT    NOT NULL,        -- 'YYYY-MM-DD'
  body       JSONB   NOT NULL,        -- PG; TEXT JSON on SQLite
  stream_cnt INTEGER NOT NULL DEFAULT 0,
  done_cnt   INTEGER NOT NULL DEFAULT 0,
  updated_at BIGINT  NOT NULL,
  PRIMARY KEY (user_id, date)
)
CREATE INDEX daily_reports_date_idx ON daily_reports(user_id, date DESC);
```

Calendar becomes one query:
```
SELECT date, stream_cnt, done_cnt FROM daily_reports
WHERE user_id=? AND date LIKE 'YYYY-MM-%'
```
Requires: `_schema_pg.py` + `_schema_sqlite.py` + `_sql_translate.py::_PK_MAP`
update. **§10.3 approval required.**

### (b) Single `_load_day_context(date)` helper
```
def _load_day_context(date):
    """Return (yesterday_report_dict_or_None, today_report_dict_or_None).
       Call _load_report at most ONCE per date."""
```
`_get_yesterday_carryover / _get_today_inherited_todos /
_get_yesterday_todo_accountability` all grow an optional `_prev=None` kw:
if provided, skip the disk load. `_mark_yesterday_todos_done` and
`_close_yesterday_remaining_todos` operate on the same in-memory dict and
return it; the caller performs a single `_save_report` at the end.

### (c) SSE endpoint `/api/daily-report/stream/<date>`
Replaces the 1.5 s poll. Events:
- `event: progress` — `{stage, current, total}`
- `event: done` — `{report}`
- `event: error` — `{error}`

Server: fed by a `queue.Queue` per `date_str`; generator threads `put()`
progress; the SSE route drains and forwards. Client: `new EventSource(...)`
with a polling fallback on network error.

### (d) Stage-message keys, not Chinese literals
Server stops emitting Chinese `message` fields. Progress payload becomes
`{stage: 'extracting', current: N, total: T}`; client composes the label
via `t('myday.stageExtractingN', {c, t})`. Add keys:
`myday.stageStarting`, `…Extracting`, `…Analyzing`, `…Saving`,
`…AnalyzingN`, `…ExtractingN`.

### (e) Scheduler: midnight-aligned + restart recovery
- Replace `time.sleep(6*3600)` with a loop that sleeps to local
  `next_midnight + 5 min`, then 24 h cadence thereafter.
- On `start_report_scheduler()`, sweep `daily_reports` dir for
  `<date>.lock` sentinel files: for each, log `interrupted`, mark
  `_active_jobs[date]={status:'error', error:'server restarted during
  generation'}`, remove lock. Frontend surfaces a "retry" button.
- **§10.2 gated (cadence constant).**

### (f) Week/month digest + Markdown export
New endpoints:
- `GET /api/daily-report/digest/week/<start_date>` — collapse 7 days into
  one "this week" summary via the existing LLM analysis prompt
  (max_tokens gated — §10.1 review).
- `GET /api/daily-report/digest/month/<y>/<m>` — same, month window.
- `GET /api/daily-report/export/<date>.md` — deterministic Markdown:
  `# YYYY-MM-DD`, `## Streams`, `## Tomorrow TODOs`, `## Unfinished`,
  `## Stats`. Pure templater, no LLM.
- `GET /api/daily-report/search?q=...&from=&to=` — after F9 lands,
  Postgres `tsvector` over `body->'streams'` titles + summaries.

### Migration plan (JSON → DB)
1. Ship schema (gated approval).
2. Boot migration: on first run with non-empty `data/config/daily_reports/`,
   insert each `YYYY-MM-DD.json` as a `daily_reports` row; move the
   original file to `data/config/daily_reports/_migrated/` (retained as
   backup until next release).
3. `_load_report` / `_save_report` become DB-first with a best-effort
   fallback to the `_migrated/` dir so rollbacks work.
4. `export.py` — no new secrets, but the new table must be listed under
   `ALWAYS_EXCLUDE` for opensource/internal modes (same policy as
   `daily_cost_cache`).

### § Approval-gated items (do not change without explicit user sign-off)
| # | Constant / Change | Location | Gate |
|---|---|---|---|
| 1 | Fuzzy-match threshold 0.35 | `routes/daily_report.py:703` | §10.1 |
| 2 | Poll `INTERVAL=1500` (or its removal) | `static/js/myday.js:383` | §10.1 |
| 3 | `max_tokens=min(4096, 400*conv_count)` | `routes/daily_report.py:2280` | §10.1 |
| 4 | LLM `temperature=0.3` | `routes/daily_report.py:2281` | §10.1 |
| 5 | Scheduler `6*3600` → midnight-align | `routes/daily_report.py:2156` | §10.1/§10.2 |
| 6 | `_CALENDAR_CACHE_TTL = 30` | `routes/daily_report.py:46` | §10.1 |
| 7 | New `daily_reports` table (DDL) | `lib/database/_schema_*.py` | §10.3 |
| 8 | Model-routing override for digest (if any) | `lib/llm_dispatch/config.py` | §10.2 |

---

## Applied in this pass

Landing in this PR (strictly §10-free — no hyperparameter, routing, or
schema changes):

1. **Single yesterday-report load per generation** — `routes/daily_report.py`
   - Added optional `_prev=None` kwarg to `_get_yesterday_carryover`,
     `_get_today_inherited_todos`, `_get_yesterday_todo_accountability`.
   - `_analyse_conversations` now calls `_load_report(yesterday)` **once**
     at entry and threads the dict through all three helpers.
   - Addresses F3 (3× redundant disk reads).

2. **Coalesced yesterday write-back** — `routes/daily_report.py`
   - `_mark_yesterday_todos_done` and `_close_yesterday_remaining_todos`
     grew `_prev`+`_defer_save` kwargs and now return the mutated dict
     plus a change counter.
   - `_analyse_conversations` issues a **single** `_save_report(yesterday,
     ...)` call at the end, logging it as a "coalesced writeback".
   - Addresses F4 (2× disk writes + 2× cache invalidations).

3. **Calendar summary cache** — `routes/daily_report.py::get_calendar_month`
   - Per-day `days` summary (parsed from `YYYY-MM-*.json`) is now stored
     on the existing `_calendar_cache` entry so a warm month skips the
     `os.listdir` + per-file `json.load` loop.
   - Invalidation ride-alongs: `_save_report` already pops the key, so
     the summary refreshes on the next generation.
   - Partially addresses F1 (N+1 parse loop) — the full fix is the
     `daily_reports` DB table in V2 §(a).

Logging discipline: every new `except` block includes a `logger.debug`
or `logger.warning` per CLAUDE.md §2.2.

Files touched: `routes/daily_report.py` (only).
No change to: `static/js/myday.js`, DB schema, `export.py`, routing
tables, or any numeric constant.

## Deferred — awaits user approval

These items appear in the V2 design but are **not applied** because they
fall under CLAUDE.md §10 (hyperparameters, routing, schema).

| # | Item | CLAUDE.md § | Current value | Proposed direction |
|---|---|---|---|---|
| D1 | `_fuzzy_todo_match` threshold | §10.1 | 0.35 | Raise to ~0.55 + min-length gate (F5) |
| D2 | Frontend poll `INTERVAL` | §10.1 | 1500 ms | Remove in favor of SSE (F11 / V2 §c) |
| D3 | LLM `max_tokens` in `_run_llm_analysis` | §10.1 | `min(4096, 400*conv_count)` | Allow 8 K for week/month digest (V2 §f) |
| D4 | LLM `temperature` | §10.1 | 0.3 | Unchanged; flagged for review on digest path |
| D5 | Scheduler cadence | §10.1 / §10.2 | `6*3600` s | Midnight-aligned + 24 h (F6 / V2 §e) |
| D6 | `_CALENDAR_CACHE_TTL` | §10.1 | 30 s | Unchanged; re-evaluate when DB table lands |
| D7 | New `daily_reports` DB table | §10.3 | n/a | Schema in V2 §a — touches both backends |
| D8 | Progress message wire format | §10.1 (UX contract) | Chinese literals | Stage-key payload (V2 §d) |
| D9 | `/api/daily-report/stream/<date>` SSE | §10.1 (API surface) | n/a | V2 §c — replace polling |
| D10 | Week/month digest + Markdown export endpoints | §10.1 (API surface) | n/a | V2 §f |
| D11 | `_active_jobs` crash-recovery sentinel | §10 (behavior change) | in-memory only | V2 §e — disk lock + startup sweep |

Every item above has a dedicated section above (F5/F6/…/V2 §a–f) with
evidence and a fix sketch — ready to request approval as a follow-up.

