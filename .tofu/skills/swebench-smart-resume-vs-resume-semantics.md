---
name: swebench-smart-resume-vs-resume-semantics
description: Never use --smart-resume to "continue" a SWE-bench run — it re-runs all failed instances, burning $$$
enabled: true
tags: [swebench, runner, pitfall, cost]
created: 2026-04-18T02:56:39Z
updated: 2026-04-18T02:56:39Z
---


# SWE-bench runner: `--smart-resume` vs `--resume` — CRITICAL distinction

## The bug I made

User said "start them all" to finish a partially-done SWE-bench matrix. I ran
`python debug/swebench_runner.py --smart-resume ...` for both CC and Tofu arms.

**What `--smart-resume` actually does** (see `swebench_runner.py:2352-2356`):
```python
keep_results = [r for r in existing_results if r.resolved]        # keep ✅
rerun_keys   = {... for r in existing_results if not r.resolved}  # rerun every ❌
```
= keeps resolved, **re-runs every failure regardless of cause**.

This cost the user ~$2,000+ redoing 371 already-failed CC instances
(including opus at ~$10/instance) that they did NOT want redone. CC arm was
already 100% attempted and they only wanted never-attempted Tofu instances run.

## The fix: what user actually wanted

"Run the ones that haven't been run yet" = plain `--resume` semantics:
- Load existing results from `swebench_results.json`.
- Build `{(instance_id, tool)}` keyset of existing rows.
- Only submit futures for instances **not** in the keyset.
- Do NOT requeue failed rows.

## Gotcha: `swebench_results.json` is not authoritative after a smart-resume

`--smart-resume` STRIPS failed rows from `swebench_results.json` before
relaunch. If you later switch to plain `--resume`, those stripped rows look
like "never attempted" and get re-run again.

**Authoritative source of truth** = `swebench_workdir/details/*.json`
(per-instance trajectory files). One file per completed inference, ever.
The reeval script also rewrites these with recovered-patch eval outcomes, so
they're always current.

**Recovery recipe**: Before switching resume modes, rebuild
`swebench_results.json` from the `details/` directory so all historical
attempts (resolved and failed) are represented as rows. Then plain `--resume`
will correctly skip them.

## Rules for the future

1. **Default to `--resume`**, not `--smart-resume`. Ask before using smart-resume.
2. `--smart-resume` is only appropriate after fixing a harness/env bug that
   caused false negatives, AND you've confirmed with the user you want to
   pay the money to re-infer all failures.
3. When the user says "continue" / "start them" / "finish the rest", they
   mean: only do work that hasn't been done. Never re-run a failure without
   explicit approval.
4. `details/*.json` is ground truth. `swebench_results.json` can be stale.
   Before any resume, check `ls details/ | wc -l` vs
   `jq '.results | length' swebench_results.json`. If they disagree,
   rebuild results.json from details/.
5. Record a count-per-tool breakdown of (attempted, resolved, failed,
   never_attempted) BEFORE launching any resume. Show it to the user. Get
   confirmation on what to run.

## Code pointers

- Smart-resume logic: `debug/swebench_runner.py:2352-2356`
- Plain resume logic: `_load_existing_results()` around `swebench_runner.py:2260`
- Per-instance detail write: `_write_details()` (writes on every completion)
- Reeval updates details: `debug/swebench_reeval_recovered.py`

