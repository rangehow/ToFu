"""Paired statistics for the portability report (stdlib only).

The bootstrap resamples TASKS (the unit a scenario's task set fixes), pairing
each task's tofu and baseline scores for one model. B and the seed are the
eval matrix's preregistered values (evaluations/terminal_bench/eval_matrix/PREREG.md),
so every report reproduces bit-for-bit.
"""
from __future__ import annotations

import random
import statistics

BOOTSTRAP = 20_000
SEED = 20261112


def mean(values):
    return statistics.fmean(values) if values else float('nan')


def paired_gain(tofu: dict, baseline: dict) -> dict:
    """Mean (tofu - baseline) over tasks scored in both arms, with a 95% task-bootstrap CI."""
    tasks = sorted(set(tofu) & set(baseline))
    diffs = [tofu[t] - baseline[t] for t in tasks]
    if not diffs:
        return {'n': 0, 'gain': None, 'ci': None, 'sign': 'n/a', 'holds': None}
    rng = random.Random(SEED)
    n = len(diffs)
    draws = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(BOOTSTRAP))
    low, high = draws[int(0.025 * BOOTSTRAP)], draws[int(0.975 * BOOTSTRAP) - 1]
    gain = statistics.fmean(diffs)
    if low > 0:
        sign, holds = '+', True
    elif high < 0:
        sign, holds = '-', False
    else:
        sign, holds = '0', None   # CI covers zero: neither a gain nor a loss is established
    return {'n': n, 'gain': gain, 'ci': [low, high], 'sign': sign, 'holds': holds}


def spread(values: list) -> dict:
    clean = [v for v in values if v == v]
    if not clean:
        return {'min': None, 'max': None, 'range': None, 'sd': None}
    return {'min': min(clean), 'max': max(clean), 'range': max(clean) - min(clean),
            'sd': statistics.pstdev(clean) if len(clean) > 1 else 0.0}
