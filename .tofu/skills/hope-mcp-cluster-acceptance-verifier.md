---
name: hope-mcp-cluster-acceptance-verifier
description: submit_job verifier + GPU-quota classify/rejected_app_id; retry_on_quota = restart-durable BACKGROUND task that PAUSES on dead session for auto-login (bounded by HOPE_MCP_MAX_PAUSE_SEC=300s, then fails fast); poll/cancel tools; NOT blocking (MCP kills at 120s)
enabled: true
tags: [hope-mcp, submission-pipeline, verification]
created: 2026-05-06T14:16:26Z
updated: 2026-06-22T10:40:49Z
---

# hope-mcp — verifier + .hope rewriter + durable, session-aware quota retry

## v0.4.1 — `_apply_overrides_to_hope_conf`
Backend re-parses `hopeConfContent` server-side; the AFO submitter reads
queue/usergroup/arguments/app_name from THAT text, NOT the run_job HTTP
fields. So `submit_job` surgically rewrites the staged `.hope` file
(line-by-line, section-aware) and ships it as `hopeConfContent`.

## v0.4.0 verifier
`submit_job` polls `POST /jobs/get_yarn_apps?runid=<rid>` (3s/60s):
accepted (PENDING/ACCEPTED/RUNNING/terminal OR app_id) → ok=True;
submission_failed (UNSUBMITTED) → ok=False + submitter_error; transient
keep polling; timeout → ok=False. Env `HOPE_MCP_VERIFY_TIMEOUT`/`_POLL_INTERVAL`.

## GPU-quota: classify + appId 兜底 (tools/submit.py)
P0 codes carry an EXPERIMENT-level GPU quota via the
`tfjob.validate.mlp.sankuai.com` admission webhook (NOT queue capacity).
Markers reordered: `("is restricted to use",100,300)` + `("admission
webhook",0,500)` BEFORE `("error message:",40,400)` so `now using M` tail
survives. `_classify_submitter_failure` → `failure_category=
{category:'quota_exceeded',experiment,limit_gpu,used_gpu}`.
`_extract_rejected_app_id` → `rejected_app_id` (a quota-rejected CR never
becomes a YARN app; do NOT watch_job it).

## Background retry — NOT blocking (tools/submit_retry.py)
chatui MCP client kills any tool call at ~120s (`MCP_CALL_TIMEOUT=120`,
`lib/mcp/types.py`; applied `lib/mcp/client.py:1402`). So `submit_job`
does ONE inline attempt; on quota rejection with `retry_on_quota=True`
calls `spawn_retry()` → background asyncio.Task on the MCP loop, returns
IMMEDIATELY `pending_retry=True`+`retry_task_id`. Shared
`submit._attempt_run_and_verify()` = ONE run_job+verify (no
re-tar/re-upload — init/S3/save run ONCE before). Tools:
`get_submit_retry_status(task_id)` + `cancel_submit_retry` (both in
TOOLS+TOOL_HANDLERS+`_SAFE_RETRY_TOOLS`). Params `retry_deadline_sec=1800`,
`retry_interval_sec=30`.

## RESTART DURABILITY
`_RetryState` carries re-spawn inputs + `to_dict/from_dict`. `_state_dir()`
= `HOPE_MCP_RETRY_STATE_DIR` else `<hope home>/.hope/hope_mcp_retry`.
`_persist()` = atomic tempfile+os.replace, one JSON/task, BEFORE the loop +
every checkpoint. `rehydrate_on_startup()` (in `server.py::run_stdio` after
loop up): running/paused→re-spawn; terminal→reload in memory; corrupt→
log+unlink+skip. `deadline_ts` ABSOLUTE wall-clock → downtime counts; an
expired task finalizes failed on first resumed iter with ZERO run_job.
Best-effort (no writable dir → in-process-only fallback).

## SESSION-AWARE PAUSE + BOUNDED PAUSE CAP
Non-terminal `paused` status + `pause_reason` + `paused_since`. `_LIVE_STATES
= {running,paused}` used by reaper/done-flag/cancel/_on_task_done. **Session
gate at top of every loop iteration**: probe `hope_api.is_session_live()`;
if dead → `status='paused'` (set `paused_since` on entry), call shared
`preflight.try_auto_login()` (idempotent while push pending), persist, sleep
one interval, `continue` — NO attempt increment, NO run_job. Recovery (auto
`approved` OR an out-of-band live-probe flip at the gate top) → back to
`running`, `paused_since=None`.
**Bounded cap:** when continuous pause `>= _MAX_PAUSE_SEC` (env
`HOPE_MCP_MAX_PAUSE_SEC`, default 300=5min) → finalize `failed` with
`session_recovery_failed:True` + clear reason (no_username → "set
HOPE_USERNAME / run hope_login"; else → "approve the push"). Without this a
no_username task would sit paused the whole `retry_deadline_sec`.
`paused_since` resets on recovery so an INTERMITTENT session never
accumulates toward the cap. snapshot surfaces `paused_for_sec` +
`pause_give_up_in_sec`; pause_reason advertises the countdown. Loop order:
deadline check FIRST, then session gate — task never outlives its budget.
Covers mid-loop expiry AND restart-with-dead-session (rehydrate seeds paused
when the shared startup probe says dead). Paused task is cancellable + never
reaped.

## CANCEL RACE (fixed)
`task.cancel()` on a Task NOT yet stepped → straight to cancelled WITHOUT
running the body → `except CancelledError` never fires. Fix:
`task.add_done_callback(_on_task_done)` reconciles cancelled/exception.

## get_run_log result_code=-1 OVERLOADED
rc=-1 + empty result_info = "log not ready"; rc=-1 + non-empty = FATAL.
`_drain_log_for_failure` retries 3× at 2s.

## Tests
`tests/test_submit.py` (46 in-file; full suite 157 pass / 2 skip determ.).
Covers retry+durability+session+cap: bg-spawn-succeeds (run_job 2×/S3+save
1×), gives-up-at-deadline, cancel, persisted-on-spawn, rehydrate-resume→
succeed, rehydrate-deadline-elapsed→0-run_job, rehydrate-keeps-terminal,
pause-on-dead-session→recover (1 run_job), rehydrate-seeds-paused (0 run_job),
paused-is-cancellable, gives-up-at-pause-cap-no-username (cap=0.6s vs
budget=600s proves cap not deadline ends it; asserts session_recovery_failed +
HOPE_USERNAME hint + elapsed<30s), pause-streak-resets-on-recovery (flapping
probe still succeeds). Autouse fixture `_isolate_submit_retry` sets
`HOPE_MCP_RETRY_STATE_DIR` to tmp + resets module globals. Cap tests
`monkeypatch.setattr(sr, "_MAX_PAUSE_SEC", small)`. `_retry_loop` imports
`_attempt_run_and_verify`/`try_auto_login`/`is_session_live` LAZILY → tests
patch the DEFINING modules (submit/preflight/hope_api). pytest-randomly NOT
installed.

