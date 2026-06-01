---
name: swebench-safety-timeout-loses-duration-bug
description: Fixed bug: SWE-bench runner safety-timeout path lost duration_s + num_turns + usage → 15 instances recorded as 0/0/0 in prior run
enabled: true
tags: [swebench, bug-fix, timeout, usage-extraction]
created: 2026-05-04T07:56:08Z
updated: 2026-05-04T07:56:08Z
---

# SWE-bench Runner: Safety-Timeout Loses Metrics (Fixed 2026-05-04)

## Bug
When `elapsed > INFERENCE_SAFETY_TIMEOUT` (4h default), `run_tofu_inference`
sets `result.error = 'Safety timeout after Ns'` and `break`s out of the poll
loop **without** setting `duration_s`, without doing a last poll to harvest
`apiRounds` usage, and without computing cost.

Net effect: 15 instances in the prior run recorded as:
```
duration=0s turns=0 in_tok=0 out_tok=0 cost=$0.00
```
even though they had `patch_size > 0` (patch was later extracted from the
workspace). These looked identical to "never started" runs and massively
understated Tofu's actual effort.

## Fix applied
In `debug/swebench_runner.py::run_tofu_inference`, inside the safety-timeout
branch:
1. Set `result.duration_s = elapsed` immediately.
2. Do one LAST poll (`timeout=10`). If it returns 200, parse `apiRounds`
   into num_turns + 4 token fields, then call `_compute_cost`.
3. Log a warning with the partial harvest summary.
4. Then abort the server task as before.

## Validation
Before relaunch: 15 safety-timeout records across 3 tofu models with 0 duration.
After fix: any new safety timeout gets real metrics + cost.

## Related
- `swebench-git-add-timeout-bug` — similar silent-loss bug in `_extract_git_diff`
- `swebench-runner-poll-404-infinite-loop-fix` — 404 poll handling

## Files
- `debug/swebench_runner.py` (safety-timeout branch in `run_tofu_inference`)

