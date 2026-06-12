#!/usr/bin/env python3
"""Re-eval OpenCode matplotlib SWE-bench patches through udocker and merge the
corrected verdicts into the run's results.json + details.

WHY: the original OC matplotlib evals hit a freetype-from-source build failure
inside the container (`ImportError: cannot import name '_c_internal_utils'`),
yielding 0/32 false-reds. Re-grading the SAME stored patch against a freshly
built container resolves cleanly. No re-inference (patches already exist).

Usage:
    python scripts/reeval_oc_matplotlib.py --workdir swebench_oc_500 --tool oc-opus [--dry-run]
"""
from __future__ import annotations
import argparse, json, logging, sys, shutil, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from maps_runner.dataset import load_swebench_verified_instances
from maps_runner.eval_udocker import evaluate_patch

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-5s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('reeval')

BUILD_SIGS = ('_c_internal_utils', 'freetype', 'undefined symbol', 'cannot import name')


def eval_out_blob(ev: dict) -> str:
    to = ev.get('test_run_output') or []
    if isinstance(to, list):
        return ' '.join((t.get('stdout', '') + t.get('stderr', '')) for t in to)
    return str(to)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--tool', required=True)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    wd = Path(args.workdir)
    results_path = wd / 'swebench_results.json'
    data = json.load(open(results_path))
    rows = data['results']

    # Target: matplotlib rows, unresolved, with the build-failure signature.
    targets = []
    for it in rows:
        if it.get('tool') != args.tool:
            continue
        if not it['instance_id'].startswith('matplotlib'):
            continue
        if it.get('resolved'):
            continue
        det = wd / 'details' / f"{it['instance_id']}__English__{args.tool}.json"
        blob = ''
        if det.exists():
            d = json.load(open(det))
            blob = eval_out_blob(d.get('eval') or {})
        if any(s in blob for s in BUILD_SIGS):
            targets.append(it['instance_id'])

    log.info('%s: %d matplotlib build-fail instances to re-eval', args.tool, len(targets))
    if not targets:
        return
    if args.dry_run:
        log.info('DRY-RUN ids: %s', ','.join(sorted(targets)))
        return

    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    bak = results_path.with_suffix(f'.bak-reeval-mpl-{args.tool}-{ts}.json')
    shutil.copy2(results_path, bak)
    log.info('backed up results -> %s', bak.name)

    inst_map = {i.instance_id: i for i in
                load_swebench_verified_instances(instance_ids=targets, load_all=False, skip_repos=None)}
    row_idx = {it['instance_id']: it for it in rows if it.get('tool') == args.tool}

    flipped = 0
    for iid in sorted(targets):
        inst = inst_map.get(iid)
        patch_file = wd / 'patches' / f'{iid}__{inst.language}__{args.tool}.diff'
        if not patch_file.exists():
            log.warning('skip %s — no patch', iid); continue
        patch = patch_file.read_text()
        ev = evaluate_patch(inst, patch, args.tool, wd)
        row = row_idx[iid]
        was = row.get('resolved')
        row['resolved'] = bool(ev.resolved)
        row['patch_applies'] = bool(ev.patch_applies)
        f2p = ev.fail_to_pass_results or {}
        p2p = ev.pass_to_pass_results or {}
        row['fail_to_pass_passed'] = sum(1 for v in f2p.values() if v)
        row['fail_to_pass_total'] = len(f2p)
        row['pass_to_pass_passed'] = sum(1 for v in p2p.values() if v)
        row['pass_to_pass_total'] = len(p2p)
        if ev.resolved and not was:
            flipped += 1
        log.info('%s: %s -> %s (f2p %d/%d)', iid, was, ev.resolved,
                 row['fail_to_pass_passed'], row['fail_to_pass_total'])

    json.dump(data, open(results_path, 'w'), indent=2)
    log.info('=== DONE %s: %d/%d flipped to resolved; written %s ===',
             args.tool, flipped, len(targets), results_path.name)


if __name__ == '__main__':
    main()
