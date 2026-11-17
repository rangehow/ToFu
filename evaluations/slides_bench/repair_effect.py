"""Paired repair-off / repair-on comparison over benchmark runs.

Usage: python -m evaluations.slides_bench.repair_effect <run> [<run> ...]
       [--seeds slides-bench-v2,slides-bench-v3] [--arms before,after]

Every Tofu run keeps its authored deck from before the layout and visual
repair stages (``tofu-authored/<doc>``, see ``run_tofu.py``), so each
generation is its own control. Run ``evaluate.py <run> --arms
tofu,tofu-authored --seed S`` for each judge seed first; this reads those
cached verdicts and audits and reports, per generation and pooled:

- the blind content mean (judge content dimensions) after minus before
  repair, averaged over judge seeds, with a percentile bootstrap 95% CI
  over generations;
- numbers visible in the authored deck that the repaired deck no longer
  shows (a deterministic content-loss check).

``--arms tofu-prev,tofu`` gives the same paired gap between the new decks
and the previous accepted decks judged in the same pass (one pair per
document and run).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics

from evaluations.slides_bench import score
from evaluations.slides_bench.corpus import data_root, document_ids

_BOOTSTRAP_RESAMPLES = 10000


def _json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def _pptx(base: str) -> str | None:
    import glob
    found = (glob.glob(os.path.join(base, 'deck', '*.pptx'))
             + glob.glob(os.path.join(base, 'forced.pptx')))
    return found[0] if found else None


def _content(verdict: dict) -> float:
    return statistics.mean(verdict['scores'][name]
                           for name in score.CONTENT_DIMENSIONS)


def _visual(verdict: dict) -> float:
    return statistics.mean(verdict['scores'][name]
                           for name in score.DIMENSIONS[6:])


def _ci(values: list, seed: str) -> tuple:
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values)))
                   for _ in range(_BOOTSTRAP_RESAMPLES))
    return (round(means[int(0.025 * len(means))], 2),
            round(means[int(0.975 * len(means)) - 1], 2))


def compare(runs: list[str], seeds: list[str], before_arm: str = 'tofu-authored',
            after_arm: str = 'tofu') -> dict:
    rows = []
    for run in runs:
        run_dir = os.path.join(data_root(), 'runs', run)
        for doc in document_ids():
            after_dir = os.path.join(run_dir, after_arm, doc)
            before_dir = os.path.join(run_dir, before_arm, doc)
            pair = {}
            for seed in seeds:
                after = _json(os.path.join(after_dir, f'judge-{seed}.json'))
                before = _json(os.path.join(before_dir, f'judge-{seed}.json'))
                if after and before:
                    pair[seed] = (before, after)
            if not pair:
                continue
            lost = []
            before_pptx, after_pptx = _pptx(before_dir), _pptx(after_dir)
            if before_pptx and after_pptx:
                kept = set().union(*map(score._numbers,
                                        score.slide_texts(after_pptx)))
                lost = sorted(set().union(*map(
                    score._numbers, score.slide_texts(before_pptx))) - kept)
            rows.append({
                'run': run, 'doc': doc,
                'delivered': os.path.isfile(os.path.join(after_dir, 'run.json')),
                'content_before': statistics.mean(
                    _content(b) for b, _ in pair.values()),
                'content_after': statistics.mean(
                    _content(a) for _, a in pair.values()),
                'visual_before': statistics.mean(
                    _visual(b) for b, _ in pair.values()),
                'visual_after': statistics.mean(
                    _visual(a) for _, a in pair.values()),
                'numbers_lost': lost})
    deltas = [r['content_after'] - r['content_before'] for r in rows]
    visual = [r['visual_after'] - r['visual_before'] for r in rows]
    return {
        'runs': runs, 'seeds': seeds, 'arms': [before_arm, after_arm],
        'generations': len(rows),
        'content_delta_mean': round(statistics.mean(deltas), 2) if deltas else None,
        'content_delta_ci95': _ci(deltas, 'content') if deltas else None,
        'visual_delta_mean': round(statistics.mean(visual), 2) if visual else None,
        'visual_delta_ci95': _ci(visual, 'visual') if visual else None,
        'numbers_lost_per_generation': round(statistics.mean(
            len(r['numbers_lost']) for r in rows), 2) if rows else None,
        'rows': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('runs', nargs='+')
    parser.add_argument('--seeds', default='slides-bench-v2')
    parser.add_argument('--arms', default='tofu-authored,tofu')
    args = parser.parse_args()
    before, after = args.arms.split(',')
    print(json.dumps(compare(args.runs, args.seeds.split(','), before, after),
                     ensure_ascii=False, indent=1))
