#!/usr/bin/env python3
"""Re-evaluate instances whose prior failure was a git-checkout TIMEOUT.

Context
-------
The 2026-05-04 rerun of all three tofu models hit 46/77 (60%) of failures as
*evaluation-harness* failures:

    Workspace setup failed: Command ['git', 'checkout', '--quiet', <sha>]
    timed out after 30 seconds

The old 30s timeout was too tight for 9 parallel evals on FUSE. The timeout
also left orphan ``.git/index.lock`` files behind, so ``evaluate_patch`` scored
these runs as ``patch_applies=False`` despite inference having produced
perfectly valid patches (byte-identical to CC's winning patches in some
cases).

The runner has since been fixed:
  - timeouts raised (checkout 30→300s, clone 300→600s, clean 30→120s)
  - retry-with-stale-lock-cleanup on timeout
  - env-var overridable: SWEBENCH_GIT_{CHECKOUT,CLEAN,CLONE}_TIMEOUT

This script re-runs the eval-only path for every row in the rerun workdir
whose prior error was a checkout timeout, using the already-saved patch file.

Does NOT re-run model inference — inference data is preserved. Only
eval outcomes (patch_applies, resolved, f2p/p2p) are rewritten.

Usage::

    python3 debug/swebench_reeval_checkout_timeouts.py \\
        --workdir swebench_rerun_workdir \\
        --max-workers 4
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from debug.swebench_runner import (
    BenchmarkResult, _save_per_run_detail, evaluate_patch,
    load_swebench_instances, setup_all_conda_envs,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('reeval-checkout-to')


def _collect_rows(details_dir: Path) -> list[dict]:
    """Find every detail whose prior failure was a git checkout timeout."""
    rows = []
    for fp in sorted(details_dir.glob('*.json')):
        try:
            with open(fp) as f:
                d = json.load(f)
        except Exception as e:
            log.warning('Skipping unreadable detail %s: %s', fp.name, e)
            continue
        if d.get('resolved'):
            continue
        ev = d.get('eval') or {}
        err_parts = [
            ev.get('error') or '',
            (d.get('inference') or {}).get('error') or '',
        ]
        err = ' '.join(err_parts)
        if "'git', 'checkout'" in err and 'timed out' in err:
            rows.append({
                'instance_id': d['instance_id'],
                'tool': d['tool'],
                'prior_error': err.strip()[:200],
            })
        elif "'git', 'clean'" in err and 'timed out' in err:
            # Some clean-timeouts ALSO fit — they blocked checkout on retry
            rows.append({
                'instance_id': d['instance_id'],
                'tool': d['tool'],
                'prior_error': err.strip()[:200],
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default='swebench_rerun_workdir')
    ap.add_argument('--max-workers', type=int, default=4,
                    help='Parallel re-evals (keep low — FUSE-bound, 4 is safe)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    workdir   = Path(args.workdir).resolve()
    patch_dir = workdir / 'patches'
    details_dir = workdir / 'details'
    results_json = workdir / 'swebench_results.json'

    rows = _collect_rows(details_dir)
    log.info('Found %d checkout-timeout failures in %s', len(rows), details_dir)
    if not rows:
        log.info('Nothing to re-evaluate.')
        return 0

    if args.dry_run:
        for r in rows:
            print(f"  {r['instance_id']:<40} {r['tool']:<14}  err={r['prior_error'][:80]}")
        return 0

    log.info('Loading SWE-bench Verified dataset...')
    instances = load_swebench_instances(load_all=True)
    inst_map = {inst.instance_id: inst for inst in instances}

    needed_iids = {r['instance_id'] for r in rows}
    needed = [inst_map[i] for i in needed_iids if i in inst_map]
    env_map = setup_all_conda_envs(needed)

    results_data = (json.loads(results_json.read_text())
                    if results_json.exists() else {})
    results_list = results_data.get('results', [])
    key_to_idx = {
        f"{r['instance_id']}__{r['tool']}": i
        for i, r in enumerate(results_list)
    }

    def _do_one(row):
        iid, tool = row['instance_id'], row['tool']
        stem = f'{iid}__{tool}'
        inst = inst_map.get(iid)
        if not inst:
            return stem, None, 'instance not in dataset'
        patch_path = patch_dir / f'{stem}.diff'
        if not patch_path.exists() or patch_path.stat().st_size == 0:
            return stem, None, 'patch missing or empty'
        model_patch = patch_path.read_text()
        t0 = time.time()
        try:
            eval_result = evaluate_patch(inst, model_patch, tool, workdir, env_map or {})
        except Exception as e:
            return stem, None, f'eval crashed: {e}'
        dt = time.time() - t0

        # Preserve inference metadata
        inf = {}
        detail_file = details_dir / f'{stem}.json'
        if detail_file.exists():
            try:
                inf = json.loads(detail_file.read_text()).get('inference', {})
            except Exception as e:
                log.debug('[%s] could not parse detail: %s', stem, e)

        br = BenchmarkResult(
            instance_id=iid,
            repo=inst.repo,
            difficulty=inst.difficulty,
            tool=tool,
            duration_s=inf.get('duration_s', 0),
            cost_usd=inf.get('cost_usd', 0),
            input_tokens=inf.get('input_tokens', 0),
            output_tokens=inf.get('output_tokens', 0),
            cache_read_tokens=inf.get('cache_read_tokens', 0),
            cache_write_tokens=inf.get('cache_write_tokens', 0),
            num_turns=inf.get('num_turns', 0),
            resolved=eval_result.resolved,
            patch_applies=eval_result.patch_applies,
            fail_to_pass_passed=sum(1 for v in eval_result.fail_to_pass_results.values() if v),
            fail_to_pass_total=len(eval_result.fail_to_pass_results),
            pass_to_pass_passed=sum(1 for v in eval_result.pass_to_pass_results.values() if v),
            pass_to_pass_total=len(eval_result.pass_to_pass_results),
            error=eval_result.error or '',
        )

        dummy_inf = type('obj', (object,), {
            'model_patch': model_patch,
            'duration_s': inf.get('duration_s', 0),
            'cost_usd': inf.get('cost_usd', 0),
            'input_tokens': inf.get('input_tokens', 0),
            'output_tokens': inf.get('output_tokens', 0),
            'cache_read_tokens': inf.get('cache_read_tokens', 0),
            'cache_write_tokens': inf.get('cache_write_tokens', 0),
            'num_turns': inf.get('num_turns', 0),
            'error': inf.get('error', ''),
            'raw_output': inf.get('raw_output', ''),
        })()
        _save_per_run_detail(workdir, inst, tool, dummy_inf, eval_result, br)
        return stem, br, f'{dt:.1f}s'

    updated = failed = flipped_to_resolved = flipped_to_failed = 0
    t0 = time.time()
    prev_resolved_map = {
        f"{r['instance_id']}__{r['tool']}": results_list[key_to_idx.get(
            f"{r['instance_id']}__{r['tool']}", -1)]['resolved']
        if f"{r['instance_id']}__{r['tool']}" in key_to_idx else False
        for r in rows
    }

    with ThreadPoolExecutor(max_workers=args.max_workers,
                            thread_name_prefix='reeval') as ex:
        futs = {ex.submit(_do_one, r): r for r in rows}
        for fut in as_completed(futs):
            row = futs[fut]
            stem = f"{row['instance_id']}__{row['tool']}"
            try:
                _, br, info = fut.result()
            except Exception as e:
                log.error('[%s] crashed: %s', stem, e, exc_info=True)
                failed += 1
                continue
            if br is None:
                log.warning('[%s] skipped: %s', stem, info)
                failed += 1
                continue
            prev = prev_resolved_map.get(stem, False)
            if br.resolved and not prev:
                flipped_to_resolved += 1
                flag = ' 🔄→✅'
            elif not br.resolved and prev:
                flipped_to_failed += 1
                flag = ' 🔄→❌'
            else:
                flag = ''
            status = '✅' if br.resolved else '❌'
            log.info('[%s] %s%s F2P=%d/%d P2P=%d/%d patch_applies=%s (%s)',
                     stem, status, flag,
                     br.fail_to_pass_passed, br.fail_to_pass_total,
                     br.pass_to_pass_passed, br.pass_to_pass_total,
                     br.patch_applies, info)
            key = f'{br.instance_id}__{br.tool}'
            rec = asdict(br)
            if key in key_to_idx:
                results_list[key_to_idx[key]] = rec
            else:
                results_list.append(rec)
                key_to_idx[key] = len(results_list) - 1
            updated += 1

    results_data['results'] = results_list
    results_data.setdefault('metadata', {})['reeval_checkout_timeouts'] = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'rows': len(rows),
        'updated': updated,
        'failed': failed,
        'flipped_to_resolved': flipped_to_resolved,
        'flipped_to_failed': flipped_to_failed,
    }
    backup = results_json.with_suffix('.before_reeval_checkout.json')
    if not backup.exists() and results_json.exists():
        backup.write_bytes(results_json.read_bytes())
    results_json.write_text(json.dumps(results_data, indent=2, default=str))
    log.info('Done in %.1fs: updated=%d failed=%d flipped→✅=%d flipped→❌=%d',
             time.time() - t0, updated, failed,
             flipped_to_resolved, flipped_to_failed)
    return 0


if __name__ == '__main__':
    sys.exit(main())
