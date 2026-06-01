---
name: hope-mcp-cluster-acceptance-verifier
description: v0.4.0 verifier: poll get_yarn_apps after run_job to detect UNSUBMITTED failures the CLI misses
enabled: true
tags: [hope-mcp, submission-pipeline, verification]
created: 2026-05-06T14:16:26Z
updated: 2026-05-06T15:31:30Z
---

# hope-mcp v0.4.1 — Cluster-acceptance verifier + .hope override rewriter

## The REAL bug behind "zw03 routing" mystery (LIVE-FIXED 2026-05-06)

Earlier notes in this memory blamed a "backend skeleton override" for
jobs being routed to a queue the caller did NOT ask for. That was
**wrong — the actual bug was in hope-mcp's own `submit_job`**.

The hope backend re-parses ``hopeConfContent`` server-side and the
AFO cluster-side submitter reads queue / usergroup / arguments /
app_name from THAT text, **not** from the parallel fields we pass in
the run_job HTTP payload. We were sending the literal .hope file
content verbatim while overriding only the HTTP-payload field.
Result: user-requested queue was silently ignored, the submitter
honoured the original queue written in the .hope file, and if that
queue pointed at an inactive sub-cluster (like zw03), the run ended
UNSUBMITTED.

## v0.4.1 fix — `_apply_overrides_to_hope_conf`

Whenever the caller passes `queue`, `usergroup`, `app_name`, or
`arguments` to `submit_job`, we NOW:

1. Copy the workdir to a staging temp dir (symlinks preserved,
   `.hopemeta` excluded).
2. Rewrite the corresponding lines inside the staged `.hope` file
   **surgically** (line-by-line, preserving comments / blank lines /
   section order — no configparser round-trip).
3. Tar the staged directory (so the cluster-side submitter sees the
   overrides when it unzips).
4. Send the SAME rewritten string as `hopeConfContent` in both
   `save_code_version` and `run_job` (so backend bookkeeping agrees
   with what the cluster will actually run).

Section-aware: `arguments` in `[user_args]` is left alone when
overriding `[others]arguments`. Fields not present in the original
file are NOT added (avoids accidentally injecting defaults).

## Live verification

```
workdir: /tmp/hope_mcp_realtest_init  (queue in .hope: zw03_training)
caller: queue=root.zw05_training_cluster.hadoop-aipnlp.llm_second
submit_job →
  ok: True
  verified: True
  run_id: 49254282
  app_id: psx2r18xrlspkti2
  state: RUNNING
  app_url: https://mlp.sankuai.com/ml/#/job/psx2r18xrlspkti2
```

## v0.4.0 verifier (still in place)

`submit_job` polls `POST /jobs/get_yarn_apps?runid=<rid>` every
`verify_poll_interval_sec` (default 3 s) for up to `verify_timeout_sec`
(default 60 s):

* **`accepted`** = state in {PENDING, ACCEPTED, RUNNING, FINISHED,
  SUCCEEDED, FAILED, KILLED} OR `app_id` is non-empty
  → ``ok=True, verified=True, app_id=<id>``
* **`submission_failed`** = state == `UNSUBMITTED`
  → ``ok=False, stage="verify"`` + submitter_error from run log
* **`transient`** = anything else (typically `SUBMITTING`) — keep polling
* **`timeout`** = deadline hit without accept/fail verdict
  → ``ok=False, stage="verify", verify.verdict="timeout"``

## `get_run_log` `result_code=-1` is OVERLOADED

LIVE-VERIFIED 2026-05-06: the backend sends `result_code=-1` with
`result_info` EMPTY for "log not ready yet" AND with `result_info`
containing the entire FATAL stack trace for "run failed before AppId
mint". Treat non-empty `result_info` on `rc=-1` as terminal.

## Log-fetch retry for async FATAL lines

The AFO submitter writes its stack trace a few seconds AFTER flipping
state to UNSUBMITTED. `_drain_log_for_failure` retries up to 3 times
at 2 s apart, short-circuiting on any known error marker.

## Submitter-error extractor markers (priority-ordered, case-insensitive)

```python
("error message:",                       40, 200),   # Image not set
("Configuration file verification failed", 0, 300),
("YarnException",                         0, 600),   # federation
("does not active",                     100, 200),
("Image not set",                        80, 200),
("tensorflow job failed or killed",      80, 200),
("Error running com.meituan",            80, 600),
("Caused by:",                           40, 600),
("FATAL",                                40, 400),
```

## Knobs

* Tool params: `verify_timeout_sec` (default 60, 0 = skip),
  `verify_poll_interval_sec` (default 3, min 0.5).
* Env vars: `HOPE_MCP_VERIFY_TIMEOUT`, `HOPE_MCP_VERIFY_POLL_INTERVAL`.

## Tests

`tests/test_submit.py` (7 dedicated tests, 93 total):

* `test_apply_overrides_to_hope_conf_rewrites_queue` — core rewriter
* `test_apply_overrides_only_rewrites_in_correct_section` — section hint
* `test_submit_job_uploads_rewritten_hope_conf` — end-to-end the
  rewritten body reaches save_code_version + run_job
* `test_submit_job_full_pipeline_verified` — happy path with AppId
* `test_submit_job_legacy_skip_verify` — verify_timeout_sec=0 bypass
* `test_submit_job_flags_unsubmitted_as_failure` — UNSUBMITTED → ok=False
* `test_submit_job_flags_timeout_as_failure` — perpetual SUBMITTING → ok=False

