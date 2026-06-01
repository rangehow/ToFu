---
name: swebench-runner-poll-404-infinite-loop-fix
description: SWE-bench runner treats 404 poll as transient error → infinite retry loop after server restart
enabled: true
tags: [bug-fix, swebench, polling, 404, server-restart, log-spam]
created: 2026-04-14T02:24:44Z
updated: 2026-04-14T02:24:44Z
---

# SWE-bench Runner 404 Poll Infinite Loop

## Bug
`debug/swebench_runner.py` and `debug/swebench_inference.py` poll `/api/chat/poll/{task_id}` 
for task status. When the server restarts, in-memory tasks are wiped and polls return 404.

The poll error handling catches ALL exceptions (including `HTTPError` from `raise_for_status()` 
on a 404 response) and just `continue`s the loop. With `INFERENCE_SAFETY_TIMEOUT = 14400` (4 hours), 
this means dead tasks get polled every 2-10s for up to 4 hours, generating thousands of 404 warnings.

## Fix
Check `poll_resp.status_code == 404` BEFORE `raise_for_status()`. On 404, break the loop 
with `result.error = 'Task lost (server restarted during inference)'`.

Also separate `requests.HTTPError` from generic `Exception` in the except block.

## Also Fixed
`static/js/branch.js` `_branchStreamPoll()` had the same issue — it treated 404 same as 
any non-OK status and retried up to 120 times. Added explicit 404 check to stop immediately.

## Root Cause of Log Spam
Server restart wiped 6 in-progress SWE-bench tasks from memory. The swebench_runner.py 
process (started yesterday, still alive) kept polling all 6 every ~5s for 30+ minutes, 
generating ~1200 WARNING entries in app.log and error.log.

