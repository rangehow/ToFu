"""Quantified style analysis: award/high-citation papers vs matched controls.

Usage:
  PYTHONPATH=$PWD .venv/bin/python3 -m evaluations.paper_style_corpus.analyze \
      [--root DATA_DIR] [--extract] [--annotation-model gpt-5.6-terra]

Steps:
  1. ``--extract``: every downloaded PDF → ``<root>/records/<id>.json``
     (sections, captions) and ``<root>/features.jsonl`` (``extract.features``).
  2. Annotation features from ``annotate.py`` output are joined per paper.
  3. Contrasts, each paired within a matched pair (same venue-year):
       * ``top_vs_accepted``: positive (award main/D&B track or high-citation)
         minus its accepted-ordinary control;
       * ``accepted_vs_rejected``: accepted control minus the rejected control
         of the same pair (the review-decision gradient).
     For each feature: n pairs, group means, mean paired difference, the
     standardized paired effect d_z, a seeded 2000-resample bootstrap 95% CI
     of the mean difference, a sign-flip permutation p and Benjamini-Hochberg
     q over all features of the contrast. Computed on the train split; the
     held-out split reports the same statistic for confirmation.
  4. A finding is *supported* when its train CI excludes 0, q < 0.10 and the
     held-out mean difference has the same sign. Only supported findings may
     justify rules (``style_rules.json``).

Output: ``findings.json`` (committed; statistics and short excerpts only).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluations.paper_style_corpus.build_corpus import DEFAULT_ROOT, HERE  # noqa: E402

BOOTSTRAP = 2000
PERMUTATIONS = 4000
SEED = 20261118
Q_MAX = 0.10
MAIN_STRATA = ('main', 'datasets_benchmarks')
CANONICAL = ('context', 'problem', 'gap', 'method', 'result', 'implication')


def _extract_one(args):
    pdf, record_path, title = args
    from evaluations.paper_style_corpus.extract import extract, features
    try:
        record = extract(str(pdf))
        record_path.write_text(json.dumps(record))
        return {'id': pdf.stem, **features(record, title)}
    except Exception as exc:
        return {'id': pdf.stem, 'error': f'{type(exc).__name__}: {exc}'[:300]}


def run_extract(root: Path, corpus: list[dict]) -> None:
    records = root / 'records'
    records.mkdir(exist_ok=True)
    jobs = [(root / 'pdf' / f"{row['id']}.pdf", records / f"{row['id']}.json", row['title'])
            for row in corpus if (root / 'pdf' / f"{row['id']}.pdf").is_file()]
    with ProcessPoolExecutor() as pool, (root / 'features.jsonl').open('w') as sink:
        for result in pool.map(_extract_one, jobs, chunksize=4):
            sink.write(json.dumps(result) + '\n')


def annotation_features(a: dict) -> dict:
    moves = [m for m in a.get('abstract_moves') or [] if m in CANONICAL + ('resource',)]
    order = [CANONICAL.index(m) for m in moves if m in CANONICAL]
    monotone = (sum(1 for x, y in zip(order, order[1:]) if y >= x) / (len(order) - 1)
                if len(order) > 1 else float('nan'))
    roles = a.get('intro_roles') or []
    strength = a.get('claim_strength')
    return {
        'ann_abs_has_gap': int('gap' in moves),
        'ann_abs_has_problem': int('problem' in moves),
        'ann_abs_has_result': int('result' in moves),
        'ann_abs_has_implication': int('implication' in moves),
        'ann_abs_starts_context': int(bool(moves) and moves[0] == 'context'),
        'ann_abs_starts_problem_or_gap': int(bool(moves) and moves[0] in ('problem', 'gap')),
        'ann_abs_monotone_order': monotone,
        'ann_abs_result_share': moves.count('result') / len(moves) if moves else float('nan'),
        'ann_abs_method_share': moves.count('method') / len(moves) if moves else float('nan'),
        'ann_intro_paragraphs': len(roles),
        'ann_intro_has_insight_par': int('insight' in roles),
        'ann_intro_has_results_par': int('results' in roles),
        'ann_intro_has_roadmap': int('roadmap' in roles),
        'ann_intro_has_example': int('example' in roles),
        'ann_intro_related_par': int('related' in roles),
        'ann_intro_first_broad': int(a.get('intro_first_sentence') == 'broad_context'),
        'ann_intro_first_specific': int(a.get('intro_first_sentence') in ('specific_problem', 'claim_or_finding', 'question', 'example')),
        'ann_insight_stated': int(a.get('intro_states_insight') is True),
        'ann_intro_quantified': int(a.get('intro_quantified_result') is True),
        'ann_contrib_bulleted': int(a.get('contribution_form') == 'bulleted_list'),
        'ann_contrib_any_list': int(a.get('contribution_form') in ('bulleted_list', 'numbered_inline')),
        'ann_contrib_count': a.get('contribution_count') if isinstance(a.get('contribution_count'), int) else float('nan'),
        'ann_claim_strength': strength if isinstance(strength, int) else float('nan'),
        'ann_claims_scoped': int(a.get('claims_scoped') is True),
        'ann_rw_contrast': int(a.get('related_work_style') == 'thematic_with_contrast'),
        'ann_rw_list': int(a.get('related_work_style') == 'list_of_summaries'),
        'ann_rw_states_difference': int(a.get('related_work_states_difference') is True),
        'ann_lim_dedicated_specific': int(a.get('limitations_framing') == 'dedicated_specific'),
        'ann_lim_none': int(a.get('limitations_framing') == 'none'),
        'ann_lim_any_dedicated': int(a.get('limitations_framing') in ('dedicated_specific', 'dedicated_generic')),
        'ann_problem_understanding': int(a.get('problem_framing') == 'understanding_gap'),
        'ann_problem_capability': int(a.get('problem_framing') == 'capability_gap'),
    }


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))


def paired_stat(diffs: list[float], rng: random.Random) -> dict:
    n = len(diffs)
    mean = statistics.fmean(diffs)
    sd = statistics.pstdev(diffs) if n > 1 else 0.0
    boots = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(BOOTSTRAP))
    lo, hi = boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]
    observed = abs(mean)
    extreme = sum(1 for _ in range(PERMUTATIONS)
                  if abs(sum(d if rng.random() < 0.5 else -d for d in diffs) / n) >= observed - 1e-12)
    return {'n': n, 'mean_diff': mean, 'ci95': [lo, hi],
            'dz': mean / sd if sd else 0.0, 'p': (extreme + 1) / (PERMUTATIONS + 1)}


def bh(pvalues: list[float]) -> list[float]:
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def pairs_for(corpus_by_id: dict, contrast: str, split: str) -> list[tuple[str, str]]:
    pairs = {}
    for row in corpus_by_id.values():
        if row.get('status') or row['split'] != split:
            continue
        pairs.setdefault(row['pair'], {}).setdefault(row['group'], row['id'])
    out = []
    for pair, members in pairs.items():
        positive_id = members.get('award') or members.get('highcite')
        if positive_id is None or corpus_by_id[positive_id]['stratum'] not in MAIN_STRATA:
            continue
        if contrast == 'top_vs_accepted' and 'control_accepted' in members:
            out.append((positive_id, members['control_accepted']))
        elif contrast == 'award_vs_accepted' and members.get('award') and 'control_accepted' in members:
            out.append((members['award'], members['control_accepted']))
        elif contrast == 'highcite_vs_accepted' and members.get('highcite') and 'control_accepted' in members:
            out.append((members['highcite'], members['control_accepted']))
        elif contrast == 'accepted_vs_rejected' and {'control_accepted', 'control_rejected'} <= set(members):
            out.append((members['control_accepted'], members['control_rejected']))
    return sorted(out)


CONTRASTS = ('top_vs_accepted', 'award_vs_accepted', 'highcite_vs_accepted',
             'accepted_vs_rejected')


def analyze(corpus: list[dict], feats: dict[str, dict]) -> dict:
    by_id = {row['id']: row for row in corpus if row['id'] in feats}
    names = sorted({k for f in feats.values() for k, v in f.items()
                    if k != 'id' and _finite(v)})
    result = {}
    for contrast in CONTRASTS:
        rows = []
        for name in names:
            row = {'feature': name}
            for split in ('train', 'test'):
                rng = random.Random(f'{SEED}:{contrast}:{name}:{split}')
                pairs = pairs_for(by_id, contrast, split)
                diffs, a_vals, b_vals = [], [], []
                for a, b in pairs:
                    va, vb = feats[a].get(name), feats[b].get(name)
                    if _finite(va) and _finite(vb):
                        diffs.append(va - vb)
                        a_vals.append(va)
                        b_vals.append(vb)
                if len(diffs) < 8:
                    row[split] = None
                    continue
                stat = paired_stat(diffs, rng)
                stat['mean_a'] = statistics.fmean(a_vals)
                stat['mean_b'] = statistics.fmean(b_vals)
                row[split] = stat
            rows.append(row)
        tested = [r for r in rows if r.get('train')]
        for r, q in zip(tested, bh([r['train']['p'] for r in tested])):
            r['train']['q'] = q
            test = r.get('test')
            lo, hi = r['train']['ci95']
            r['supported'] = bool(
                (lo > 0 or hi < 0) and q < Q_MAX and test
                and math.copysign(1, test['mean_diff']) == math.copysign(1, r['train']['mean_diff'])
                and test['mean_diff'] != 0)
        result[contrast] = rows
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default=str(DEFAULT_ROOT))
    parser.add_argument('--extract', action='store_true')
    parser.add_argument('--annotation-model', default='gpt-5.6-terra')
    args = parser.parse_args(argv)
    root = Path(args.root)
    corpus = json.loads((root / 'corpus.json').read_text())
    if args.extract or not (root / 'features.jsonl').is_file():
        run_extract(root, corpus)
    feats = {}
    for line in (root / 'features.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row.get('error'):
            continue
        # Extraction-failure guard: a body under 1500 words, no abstract, or
        # an "abstract" over 400 words (it ran into the introduction) means
        # the PDF text layer did not segment; the paper is excluded.
        if (row.get('body_words', 0) < 1500 or row.get('abs_n_words', 0) < 40
                or row.get('abs_n_words', 0) > 400):
            continue
        feats[row['id']] = row
    from evaluations.paper_style_corpus.annotate import PROMPT_DIGEST
    for path in (root / 'annotations').glob(f'*.{PROMPT_DIGEST}.{args.annotation_model}.json'):
        data = json.loads(path.read_text())
        if data['id'] in feats:
            feats[data['id']].update(annotation_features(data))
    analysis = analyze(corpus, feats)
    groups = {}
    for row in corpus:
        if row['id'] in feats:
            groups[row['group']] = groups.get(row['group'], 0) + 1
    out = {'generated': __import__('time').strftime('%Y-%m-%d'),
           'annotation_model': args.annotation_model,
           'n_analyzed_by_group': groups, 'contrasts': analysis}
    (HERE / 'findings.json').write_text(json.dumps(out, indent=1, default=float))
    for contrast, rows in analysis.items():
        print(f'== {contrast}')
        for r in sorted(rows, key=lambda r: (r.get('train') or {}).get('p', 1)):
            t = r.get('train')
            if not t:
                continue
            flag = '*' if r.get('supported') else ' '
            te = r.get('test') or {}
            print(f"{flag} {r['feature']:32s} n={t['n']:3d} a={t['mean_a']:.3f} b={t['mean_b']:.3f} "
                  f"d={t['mean_diff']:+.3f} [{t['ci95'][0]:+.3f},{t['ci95'][1]:+.3f}] dz={t['dz']:+.2f} "
                  f"q={t['q']:.3f} | test d={te.get('mean_diff', float('nan')):+.3f} n={te.get('n', 0)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
