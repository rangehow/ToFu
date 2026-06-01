---
name: hope-mcp-v050-watch-stop-verified
description: v0.5.0: watch_job / stop_job_verified / get_task_attempts apply the submit-verify pattern to status + stop
enabled: true
tags: [hope-mcp, verification, status, stop]
created: 2026-05-06T16:01:12Z
updated: 2026-05-06T16:01:12Z
---

# hope-mcp v0.5.0 — Watch / verified-stop / task-attempts

## Motivation

The v0.4 submit_job verifier proved "poll until cluster matches RPC" is
the correct pattern. v0.5 extends the same pattern to the read/stop
side:

* `hope_watch_job` — read-only: poll a job's state until terminal (or
  a custom target state set), return the transition history.
* `hope_stop_job_verified` — read-write: issue stop RPC, then poll
  until the cluster actually enters a terminal state. Surfaces real
  verdicts (killed / already_terminal / stop_unaccepted / timeout)
  instead of just RPC-accepted.
* `hope_get_task_attempts` — read-only: per-pod host / namespace info
  (endpoint was 500 on 2026-05-02, recovered by 2026-05-06).

## State taxonomy (live-verified)

```
_CLUSTER_TERMINAL_STATES = {
    FINISHED, SUCCEEDED, SUCCESS,
    FAILED, KILLED,
    STOPPED,      # hope-layer state for pre-AppId kills (live-verified)
}

_STOP_IN_PROGRESS_STATES = {KILLING, KILL_WAITING, STOPPING, TERMINATING}

_TERMINAL_FAILURE_STATES = {UNSUBMITTED}

_CLUSTER_ACCEPTED_STATES = {PENDING, ACCEPTED, RUNNING} ∪ _CLUSTER_TERMINAL_STATES
```

## stop_job_verified verdict table

| Final state observed | Verdict         | ok    |
|----------------------|-----------------|-------|
| KILLED / STOPPED     | killed          | True  |
| FINISHED / FAILED    | already_terminal| True  |
| (never terminal, RPC ok)     | timeout         | False |
| (never terminal, RPC failed) | stop_unaccepted | False |

## Live test results

```
submit:            run_id=49255075, app_id=psx2dlaxrlud5vr6, state=SUBMITTING → verified
stop_job_verified: verdict=killed, state=STOPPED, polls=1, elapsed=0.06s
```

## Intra-cluster log downloader is still out of scope

`get_task_attempts` is alive, but `hope fetch --log` uses per-node
`http://<hostName>:8416/log/file/download` which is only reachable
from inside the cluster. Moved `hope_fetch_log` into
`_OUT_OF_SCOPE_TOOLS` with this explanation.

## Tool surface (25 tools as of v0.5.0)

| Category | Tools |
|---|---|
| Submission | submit_job, init_job, upload_code, run_job, quickrun, stop_session |
| Stop & query | stop_job, **stop_job_verified**, stop_jobs_batch, get_status, get_status_batch, **watch_job**, list_resource, change_priority |
| Read-only diag | **get_task_attempts**, get_run_log, get_metadata, list_supported_job_types, get_lion_config |
| Data movement | fetch_source_code, dfs_ls |
| Auth / diag | check_login, login, logout, describe_endpoints |

## Tests

`tests/test_watch_stop_verified.py` — 13 tests:
* watch_job: terminal, custom target_states, timeout, arg validation (×2)
* stop_job_verified: killed (KILLED), killed (STOPPED), already_terminal,
  stop_unaccepted, timeout-with-stopping, empty-runid
* get_task_attempts: has-pods, empty-pods, empty-id

The conftest now supports `route_dynamic(url, handler)` for
state-machine fakes — handler is called per-request with the URL +
params + data so it can flip state across polls.

