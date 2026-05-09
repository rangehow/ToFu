#!/usr/bin/env python3
"""A/B test: does adding Claude Code's "minimal-edit" coding-style block
to our SWE-bench prompt close the resolve-rate gap?

Two arms per (tool, instance):
  • baseline  — the existing build_agent_prompt() output, unchanged.
  • +ccstyle  — same prompt, plus the coding-style block from
                claude-code/src/constants/prompts.ts:204-218 prepended.

Each arm submits to the isolated SWE-bench server (port 15001) and
fully captures the resulting patch + tool trace.  After both arms
finish, the patch is evaluated using debug/swebench_runner.evaluate_patch
to get a real resolve verdict.

Outputs a per-(tool,instance) row of:
   baseline_resolved, ccstyle_resolved,
   baseline_turns,    ccstyle_turns,
   baseline_patch_size, ccstyle_patch_size,
   baseline_cost,     ccstyle_cost.

Usage:
  python3 debug/swebench_abtest_minimal_edit_prompt.py \\
      --set /tmp/swebench_capture/abtest_set.json \\
      --output /tmp/swebench_capture/abtest_results.json \\
      --workers 2

Important: this script does NOT modify lib/tasks_pkg/system_context.py
to inject the prompt globally.  It only prepends the addendum to the
*user message* on a per-task basis, leaving Tofu's system prompt
unchanged.  This isolates the experiment to the prompt contents and
avoids any restart of the running server.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debug.swebench_runner import (
    MODEL_PRESETS, build_agent_prompt, evaluate_patch,
    load_swebench_instances, setup_workspace,
)


# ────────────────────────────────────────────────────────────────────────
# THE PROMPT ADDENDUM UNDER TEST
# ────────────────────────────────────────────────────────────────────────
# Adapted from claude-code/src/constants/prompts.ts (the
# getSimpleDoingTasksSection block).  Wording trimmed slightly to fit a
# user-message preamble; substantive bullets preserved verbatim.
CC_STYLE_PREFACE = """\
# Code style — minimum-change bias

When fixing a bug or implementing a feature, edit only what's required:

- Don't add features, refactor code, or make "improvements" beyond what
  was asked.  A bug fix doesn't need surrounding code cleaned up.  A
  simple feature doesn't need extra configurability.  Don't add docstrings,
  comments, or type annotations to code you didn't change.
- Don't add error handling, fallbacks, or validation for scenarios that
  can't happen.  Trust internal code and framework guarantees.  Only
  validate at system boundaries (user input, external APIs).
- Don't create helpers, utilities, or abstractions for one-time
  operations.  Don't design for hypothetical future requirements.
  Three similar lines of code is better than a premature abstraction.
- When adding a new branch (e.g. handling a special case), preserve the
  existing default branch intact: write `if NEW: ... else: <original>`,
  not `if NEW: ... ` with the original deleted.
- Before reporting the task complete, verify it actually works: run the
  failing test or re-read the changed file and confirm.

────────────────────────────────────────────────────────────────────────
"""


def build_user_prompt(inst, *, with_ccstyle: bool) -> str:
    """Return the user message for one arm.

    Both arms share the same SWE-bench instructions; only the addendum
    differs.  The addendum is prepended (not appended) so it sits at
    the start of the user message — the most attention-attractive
    position absent a system-prompt change.
    """
    base = build_agent_prompt(inst)
    if with_ccstyle:
        return CC_STYLE_PREFACE + base
    return base


# ────────────────────────────────────────────────────────────────────────
# Arm execution — submit + poll a single task
# ────────────────────────────────────────────────────────────────────────
def run_one_arm(inst, tool: str, *, with_ccstyle: bool, server: str,
                workdir: Path, max_poll_s: int = 1500) -> dict:
    """Run one A/B arm. Returns a dict suitable for the result table."""
    import requests, subprocess

    cfg = MODEL_PRESETS[tool]
    arm = 'ccstyle' if with_ccstyle else 'baseline'
    arm_tag = f'{tool}__{arm}'
    log_prefix = f'[{inst.instance_id} {arm_tag}]'

    # Workspace per arm (so the patch we extract is for this arm only)
    ws = setup_workspace(inst, arm_tag, workdir)
    user_prompt = build_user_prompt(inst, with_ccstyle=with_ccstyle)

    # ★ Unique convId per arm — prevents the manager's auto-abort-by-conv
    #   logic from killing a task when another submits at the same instant.
    #   Without this, concurrent submits with blank convId collide and
    #   the earlier one is aborted (observed: status=done, 0 rounds, 0 content).
    _conv_id = f'swebench-abtest-{inst.instance_id}-{tool}-{arm}-{int(time.time())}'
    body = {
        'messages': [{'role': 'user', 'content': user_prompt}],
        'convId': _conv_id,
        'config': {
            'model': cfg.model_id,
            'projectEnabled': True,
            'projectPath': str(ws),
            **(cfg.config_overrides or {}),
        },
    }
    rec = {
        'instance': inst.instance_id, 'tool': tool, 'arm': arm,
        'model': cfg.model_id,
        'rounds': 0, 'patch_size': 0, 'duration_s': 0.0,
        'cost_usd': 0.0, 'input_tokens': 0, 'output_tokens': 0,
        'cache_read_tokens': 0, 'cache_write_tokens': 0,
        'patch': '', 'task_id': '', 'final_status': '', 'error': '',
    }

    t0 = time.time()
    try:
        r = requests.post(f'{server}/api/chat/start', json=body, timeout=60)
        r.raise_for_status()
        rec['task_id'] = r.json().get('taskId', '')
        if not rec['task_id']:
            rec['error'] = 'no task_id'
            return rec
        last_round = -1
        while True:
            elapsed = time.time() - t0
            if elapsed > max_poll_s:
                rec['error'] = f'timeout {max_poll_s}s'
                try:
                    requests.post(f'{server}/api/chat/abort/{rec["task_id"]}',
                                  timeout=5)
                except Exception:
                    pass
                break
            try:
                rr = requests.get(f'{server}/api/chat/poll/{rec["task_id"]}',
                                  timeout=15)
            except Exception:
                time.sleep(2); continue
            if rr.status_code != 200:
                time.sleep(2); continue
            data = rr.json()
            status = data.get('status')
            rounds = data.get('apiRounds') or []
            if len(rounds) > last_round:
                last_round = len(rounds)
                print(f'{log_prefix} [+{int(elapsed):4d}s] '
                      f'status={status} rounds={last_round}', flush=True)
            if status in ('done', 'error'):
                break
            time.sleep(2)
        # Final fetch
        rr = requests.get(f'{server}/api/chat/poll/{rec["task_id"]}', timeout=30)
        final = rr.json()
        rec['final_status'] = final.get('status', '')
        rec['rounds'] = len(final.get('apiRounds') or [])
        rec['duration_s'] = time.time() - t0
        # Extract patch
        try:
            p = subprocess.run(['git', 'diff'], capture_output=True, text=True,
                               cwd=str(ws), timeout=120)
            rec['patch'] = p.stdout
            rec['patch_size'] = len(rec['patch'])
        except Exception as e:
            rec['error'] = f'git diff: {e}'
        # Aggregate usage
        for rd in (final.get('apiRounds') or []):
            u = rd.get('usage') or {}
            _pt = u.get('prompt_tokens', 0) or 0
            _cr_a = u.get('cache_read_tokens', 0) or 0
            _det = u.get('prompt_tokens_details') or {}
            _cr_o = (_det.get('cached_tokens', 0) or 0) if isinstance(_det, dict) else 0
            cr = max(_cr_a, _cr_o)
            uncached = max(_pt - _cr_o, 0) if (_cr_o > 0 and _cr_a == 0) else _pt
            rec['input_tokens']      += uncached
            rec['output_tokens']     += u.get('completion_tokens', 0) or 0
            rec['cache_read_tokens'] += cr
            rec['cache_write_tokens'] += u.get('cache_write_tokens', 0) or 0
        # Cost
        rec['cost_usd'] = round(
            rec['input_tokens']       * cfg.price_input  / 1000 +
            rec['output_tokens']      * cfg.price_output / 1000 +
            rec['cache_read_tokens']  * cfg.price_cache_read  / 1000 +
            rec['cache_write_tokens'] * cfg.price_cache_write / 1000, 6,
        )
    except Exception as e:
        rec['error'] = f'{type(e).__name__}: {e}'
    return rec


# ────────────────────────────────────────────────────────────────────────
# Eval — run the saved patch through evaluate_patch
# ────────────────────────────────────────────────────────────────────────
def eval_arm(rec: dict, inst, env_map: dict, workdir: Path) -> dict:
    """Add resolve verdict to rec by running evaluate_patch on its patch."""
    if not rec.get('patch'):
        rec['resolved'] = False
        rec['eval_error'] = 'empty patch'
        return rec
    try:
        ev = evaluate_patch(inst, rec['patch'], rec['tool'] + '__' + rec['arm'],
                            workdir, env_map)
        rec['resolved'] = ev.resolved
        rec['patch_applies'] = ev.patch_applies
        rec['f2p_passed'] = sum(1 for v in ev.fail_to_pass_results.values() if v)
        rec['f2p_total'] = len(ev.fail_to_pass_results)
        rec['p2p_passed'] = sum(1 for v in ev.pass_to_pass_results.values() if v)
        rec['p2p_total'] = len(ev.pass_to_pass_results)
        rec['eval_error'] = ev.error or ''
    except Exception as e:
        rec['resolved'] = False
        rec['eval_error'] = f'eval crashed: {e}'
    return rec


# ────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--set', required=True,
                    help='JSON file with [{"tool":..., "instance":...}] entries')
    ap.add_argument('--output', required=True)
    ap.add_argument('--server', default=os.environ.get(
        'TOFU_BASE_URL', 'http://127.0.0.1:15001'))
    ap.add_argument('--workdir', default=str(
        Path(__file__).resolve().parent.parent / 'abtest_workdir'))
    ap.add_argument('--workers', type=int, default=2,
                    help='Concurrent arms in flight (per-arm runs are still single)')
    ap.add_argument('--max-poll-s', type=int, default=1500)
    args = ap.parse_args()

    test_set = json.loads(Path(args.set).read_text())
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    # Symlink prebuilt envs+repos to save startup
    base_old = Path(__file__).resolve().parent.parent / 'swebench_workdir'
    for sub in ('conda_envs', 'repos'):
        link = workdir / sub
        if not link.exists():
            link.symlink_to(base_old / sub)
    # Sub-dirs
    for sub in ('details', 'patches', 'workspaces', 'eval'):
        (workdir / sub).mkdir(exist_ok=True)

    print(f'Loading {len(test_set)} instances …')
    instances = load_swebench_instances(load_all=True)
    inst_map = {i.instance_id: i for i in instances}

    # Build job list — one job per (tool, instance, arm)
    jobs = []
    for entry in test_set:
        tool, iid = entry['tool'], entry['instance']
        if iid not in inst_map:
            print(f'  SKIP unknown instance {iid}'); continue
        for with_cc in (False, True):
            jobs.append((tool, inst_map[iid], with_cc))
    print(f'Total arms to run: {len(jobs)}')

    # Set up envs (already built, but populates env_map)
    from debug.swebench_runner import setup_all_conda_envs
    _seen_iids = set()
    needed = []
    for _, inst, _ in jobs:
        if inst.instance_id not in _seen_iids:
            _seen_iids.add(inst.instance_id)
            needed.append(inst)
    env_map = setup_all_conda_envs(needed)

    # Run inference arms in parallel
    results = []
    lock = threading.Lock()

    def _do(job):
        tool, inst, with_cc = job
        return run_one_arm(inst, tool, with_ccstyle=with_cc,
                           server=args.server, workdir=workdir,
                           max_poll_s=args.max_poll_s)

    print(f'\nLaunching inference arms (workers={args.workers}) …')
    with ThreadPoolExecutor(max_workers=args.workers,
                            thread_name_prefix='abtest') as ex:
        futs = {ex.submit(_do, j): j for j in jobs}
        for fut in as_completed(futs):
            tool, inst, with_cc = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                rec = {'instance': inst.instance_id, 'tool': tool,
                       'arm': 'ccstyle' if with_cc else 'baseline',
                       'error': f'arm crashed: {e}', 'patch': ''}
            with lock:
                results.append(rec)
            print(f'  ✓ inference done: {rec["instance"]:<35} '
                  f'{rec["tool"]:<13} {rec.get("arm","?"):<8} '
                  f'patch={rec.get("patch_size",0)} rounds={rec.get("rounds",0)} '
                  f'${rec.get("cost_usd",0):.2f}', flush=True)

    # Sort results for deterministic ordering
    results.sort(key=lambda r: (r['instance'], r['tool'], r['arm']))

    # Snapshot intermediate results before eval (in case eval crashes)
    Path(args.output + '.preeval').write_text(
        json.dumps(results, indent=2, default=str))

    # Sequential eval (FUSE-bound; parallel eval makes things worse)
    print(f'\nEvaluating patches sequentially …')
    for rec in results:
        inst = inst_map[rec['instance']]
        rec = eval_arm(rec, inst, env_map, workdir)
        print(f'  {rec["instance"]:<35} {rec["tool"]:<13} {rec.get("arm"):<8} '
              f'resolved={rec.get("resolved")} '
              f'F2P={rec.get("f2p_passed",0)}/{rec.get("f2p_total",0)} '
              f'P2P={rec.get("p2p_passed",0)}/{rec.get("p2p_total",0)}'
              f'{" (" + rec.get("eval_error","")[:60] + ")" if rec.get("eval_error") else ""}',
              flush=True)

    # Aggregate per (tool, arm)
    print('\n━━━ SUMMARY ━━━')
    agg = {}
    for r in results:
        k = (r['tool'], r['arm'])
        a = agg.setdefault(k, {'n':0, 'resolved':0, 'turns':0, 'patch':0,
                                 'cost':0.0, 'dur':0.0})
        a['n']      += 1
        a['resolved'] += int(bool(r.get('resolved')))
        a['turns']  += r.get('rounds', 0)
        a['patch']  += r.get('patch_size', 0)
        a['cost']   += r.get('cost_usd', 0)
        a['dur']    += r.get('duration_s', 0)
    for k in sorted(agg):
        a = agg[k]
        n = max(a['n'], 1)
        print(f'  {k[0]:<14} {k[1]:<10} resolved {a["resolved"]}/{a["n"]} '
              f'  avg_turns {a["turns"]/n:>5.1f}  avg_patch {a["patch"]/n:>6.0f}c '
              f' avg_cost ${a["cost"]/n:>5.2f}  avg_time {a["dur"]/n:>5.0f}s')

    # Save final
    Path(args.output).write_text(json.dumps(results, indent=2, default=str))
    print(f'\nSaved {args.output}')


if __name__ == '__main__':
    main()
