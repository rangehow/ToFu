#!/usr/bin/env python3
"""Batch, section-level A/B for the paper insight second-pass.

Fixes the confound in the first harness: scoring a whole 39k-char document means
a 3k insight section literally cannot move a whole-document mean (and dilutes it
negative). Here we isolate the artifact we actually add:

  arm A = the PLAIN REPORT scored on the 4 INSIGHT axes  (the insight a reader
          gets from the fidelity report today — the incumbent)
  arm B = the INSIGHT SECTION ALONE scored on the SAME 4 axes  (the new artifact)

Both arms score on the identical rubric, which already tells the judge to ignore
factual coverage — so scoring a focused section against a long report is fair on
the INSIGHT dimension, which is the whole question.

Design (matches the 50%-gated bootstrap-win-rate convention):
  * per paper, each arm is scored ``--repeats`` times and averaged (kills the
    single-shot rubric calibration noise);
  * the pair yields a delta = B_overall - A_overall and a win indicator (B > A);
  * we bootstrap over PAPERS (resample with replacement) to get a win-rate point
    estimate and a one-sided 95% CI LOWER BOUND. The feature clears the bar iff
    that lower bound > 0.5.

Also reports the per-axis mean delta so we can see WHICH axis carries the win
(transfer_concreteness is the hypothesised moat).

Usage (needs a live model + the app venv):
  python3 debug/insight_batch.py --lang en --repeats 3
  python3 debug/insight_batch.py --hashes h1,h2,h3 --lang en --repeats 3 --boot 2000
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('TRADING_ENABLED', '0')

from lib.log import get_logger  # noqa: E402
from lib.paper.insight_engine import (  # noqa: E402
    RUBRIC_AXES,
    generate_insight,
    score_report_rubric,
)

logger = get_logger(__name__)

# Synthetic/probe hashes to skip in auto-select.
_SKIP_HASH_PREFIXES = ('probe', 'test', 'dummy')

# Resumable on-disk cache — live insight generation runs ~5-8 min/paper (real
# web_search), so a single run of N papers × repeat-scoring overruns any sane
# timeout. Split into idempotent phases: `gen` caches the rendered section per
# paper, `score` caches per-arm scores, `summary` aggregates. Each phase can be
# run in chunks and re-run freely — already-cached work is skipped.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.insight_cache')


def _cache_path(kind, phash, lang):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f'{phash}_{lang}.{kind}.json')


def _cache_read(kind, phash, lang):
    p = _cache_path(kind, phash, lang)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning('[insight_batch] cache read failed %s: %s', p, e)
        return None


def _cache_write(kind, phash, lang, obj):
    try:
        with open(_cache_path(kind, phash, lang), 'w', encoding='utf-8') as f:
            json.dump(obj, f)
    except Exception as e:
        logger.warning('[insight_batch] cache write failed: %s', e)


def _db():
    from lib.database import get_thread_db
    return get_thread_db()


def _auto_hashes(lang, limit):
    rows = _db().execute(
        "SELECT paper_hash, length(report) AS n FROM paper_reports "
        "WHERE lang = ? AND length(report) > 3000 ORDER BY n DESC",
        (lang,)).fetchall()
    out = []
    for r in rows or []:
        h = r['paper_hash']
        if any(h.startswith(p) for p in _SKIP_HASH_PREFIXES):
            continue
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _load_report(phash, lang):
    row = _db().execute(
        "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
        (phash, lang)).fetchone()
    return (row['report'] if row else '') or ''


def _load_paper_text(phash):
    row = _db().execute(
        "SELECT parsed_text FROM paper_library WHERE paper_hash = ? LIMIT 1",
        (phash,)).fetchone()
    return (row['parsed_text'] if row else '') or ''


def _load_self_identity(phash):
    """(arxiv_id, title) of the paper under analysis, for the self-ref guard."""
    row = _db().execute(
        "SELECT title, arxiv_id FROM paper_library WHERE paper_hash = ? LIMIT 1",
        (phash,)).fetchone()
    if not row:
        return None, None
    return (row['arxiv_id'] or None), (row['title'] or None)


def _score_runs(report_md, model, repeats):
    """Score ``report_md`` ``repeats`` times; return list of {overall, scores} dicts."""
    runs = []
    for _ in range(repeats):
        v = score_report_rubric(report_md, model=model)
        if v:
            runs.append({'overall': v['overall'], 'scores': v['scores']})
    return runs


def _agg(runs):
    """Aggregate a list of score runs → (overall_mean, per_axis_mean, n)."""
    if not runs:
        return None, {}, 0
    overalls = [r['overall'] for r in runs]
    axis_sums = {a: 0.0 for a in RUBRIC_AXES}
    axis_n = {a: 0 for a in RUBRIC_AXES}
    for r in runs:
        for a in RUBRIC_AXES:
            if a in r['scores']:
                axis_sums[a] += r['scores'][a]
                axis_n[a] += 1
    per_axis = {a: axis_sums[a] / axis_n[a] for a in RUBRIC_AXES if axis_n[a]}
    return sum(overalls) / len(overalls), per_axis, len(runs)


def _bootstrap_winrate(deltas, boot, seed=1234):
    """Paired bootstrap over papers. Returns (point_winrate, ci_lower_95_one_sided).

    Win indicator per paper: 1 if delta>0, 0 if delta<0, 0.5 tie. Resample papers
    with replacement ``boot`` times; the CI lower bound is the 5th percentile of
    the resampled mean win-rates (one-sided 95%).
    """
    def _win(d):
        return 1.0 if d > 1e-9 else (0.0 if d < -1e-9 else 0.5)

    wins = [_win(d) for d in deltas]
    point = sum(wins) / len(wins)
    rng = random.Random(seed)
    n = len(wins)
    means = []
    for _ in range(boot):
        sample = [wins[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lower = means[max(0, int(0.05 * boot) - 1)]
    return point, lower


def _phase_gen(hashes, lang, model):
    """Generate + cache the insight section for each paper (idempotent)."""
    for i, phash in enumerate(hashes, 1):
        if _cache_read('gen', phash, lang) is not None:
            print(f'[gen {i}/{len(hashes)}] {phash[:12]} — cached, skip')
            continue
        report_md = _load_report(phash, lang)
        if not report_md:
            _cache_write('gen', phash, lang, {'ok': False, 'why': 'no report'})
            continue
        paper_text = _load_paper_text(phash)
        self_aid, self_title = _load_self_identity(phash)
        print(f'[gen {i}/{len(hashes)}] {phash[:12]} — report {len(report_md)} chars; generating …')
        res = generate_insight(paper_text, report_md, lang, phash=phash, model=model,
                               self_arxiv_id=self_aid, self_title=self_title)
        ok = bool(res['insight']) and not res['llmError']
        _cache_write('gen', phash, lang, {
            'ok': ok, 'why': '' if ok else ('llm error' if res['llmError'] else 'no json'),
            'section': res['markdown'], 'grounded': res['grounded'], 'dropped': res['dropped'],
            'selfref': res.get('selfref', 0),
        })
        print(f'        {"ok" if ok else "FAIL"} — {len(res["markdown"])} chars, '
              f'{res["grounded"]} grounded / {res["dropped"]} dropped / '
              f'{res.get("selfref", 0)} self-ref dropped')


def _phase_score(hashes, lang, model, repeats):
    """Score arm A (report) + arm B (section) per paper; cache the raw runs."""
    for i, phash in enumerate(hashes, 1):
        gen = _cache_read('gen', phash, lang)
        if not gen or not gen.get('ok'):
            continue
        existing = _cache_read('score', phash, lang)
        if existing and existing.get('a') and existing.get('b'):
            print(f'[score {i}/{len(hashes)}] {phash[:12]} — cached, skip')
            continue
        report_md = _load_report(phash, lang)
        section = gen['section']
        print(f'[score {i}/{len(hashes)}] {phash[:12]} — scoring A×{repeats} + B×{repeats} …')
        a_runs = _score_runs(report_md, model, repeats)
        b_runs = _score_runs(section, model, repeats)
        _cache_write('score', phash, lang, {'a': a_runs, 'b': b_runs})
        a_ov, _, _ = _agg(a_runs)
        b_ov, _, _ = _agg(b_runs)
        if a_ov is not None and b_ov is not None:
            print(f'        A={a_ov:.2f}  B={b_ov:.2f}  Δ={b_ov - a_ov:+.2f}')


def _scan_cache_pairs():
    """All (phash, lang) that have a gen cache file — for the cross-lang summary."""
    out = []
    if not os.path.isdir(_CACHE_DIR):
        return out
    for fn in sorted(os.listdir(_CACHE_DIR)):
        if not fn.endswith('.gen.json'):
            continue
        stem = fn[:-len('.gen.json')]
        # stem == '<phash>_<lang>'; lang is the final underscore-separated token.
        phash, _, lang = stem.rpartition('_')
        if phash and lang:
            out.append((phash, lang))
    return out


def _phase_summary(hash_lang_pairs, boot):
    """Aggregate over an explicit list of (phash, lang) pairs → bootstrap verdict."""
    pairs = []
    skipped = []
    for phash, lang in hash_lang_pairs:
        gen = _cache_read('gen', phash, lang)
        if not gen or not gen.get('ok'):
            skipped.append((phash, lang, gen.get('why', 'not generated') if gen else 'not generated'))
            continue
        sc = _cache_read('score', phash, lang)
        if not sc:
            skipped.append((phash, lang, 'not scored'))
            continue
        a_ov, a_ax, a_n = _agg(sc.get('a') or [])
        b_ov, b_ax, b_n = _agg(sc.get('b') or [])
        if a_ov is None or b_ov is None:
            skipped.append((phash, lang, 'scoring empty'))
            continue
        pairs.append((phash, lang, a_ov, b_ov, a_ax, b_ax, gen.get('grounded', 0)))

    if not pairs:
        print('\nNo scorable pairs yet.', file=sys.stderr)
        for h, lg, why in skipped:
            print(f'  {h[:12]} [{lg}]: {why}', file=sys.stderr)
        return

    print('\n=== PER-PAPER (report → insight section, on the 4 INSIGHT axes) ===')
    n_zh = sum(1 for p in pairs if p[1] == 'zh')
    for phash, lang, a_ov, b_ov, _a, _b, gr in pairs:
        flag = 'WIN' if b_ov > a_ov + 1e-9 else ('tie' if abs(b_ov - a_ov) < 1e-9 else 'LOSS')
        print(f'  {phash[:12]} [{lang:2s}]  A={a_ov:.2f}  B={b_ov:.2f}  Δ={b_ov - a_ov:+.2f}  '
              f'grounded={gr}  [{flag}]')

    deltas = [b - a for _h, _lg, a, b, _ax, _bx, _g in pairs]
    point, lower = _bootstrap_winrate(deltas, boot)
    mean_delta = sum(deltas) / len(deltas)

    print(f'\n=== SUMMARY (n={len(pairs)} scored [{n_zh} zh], {len(skipped)} skipped) ===')
    print(f'  mean Δ overall (B - A):       {mean_delta:+.3f}')
    print(f'  section-beats-report rate:    {point:.2f}')
    print(f'  bootstrap 95% CI lower bound:  {lower:.2f}   '
          f'({"CLEARS" if lower > 0.5 else "does NOT clear"} the 0.5 gate)')

    print('\n=== PER-AXIS mean (report → section) — which axis carries it ===')
    for ax in RUBRIC_AXES:
        avals = [p[4].get(ax) for p in pairs if p[4].get(ax) is not None]
        bvals = [p[5].get(ax) for p in pairs if p[5].get(ax) is not None]
        if avals and bvals:
            am, bm = sum(avals) / len(avals), sum(bvals) / len(bvals)
            print(f'  {ax:32s} {am:.2f} → {bm:.2f}  ({bm - am:+.2f})')

    if skipped:
        print('\n  skipped:')
        for h, lg, why in skipped:
            print(f'    {h[:12]} [{lg}]: {why}')


def _phase_gated_summary(hash_lang_pairs, boot):
    """The GATED verdict: partition papers by the a-priori headroom gate on their
    OWN arm-A baseline, then report BOTH:
      (a) fired subset (baseline <= INSIGHT_GATE_THRESHOLD) — win-rate + bootstrap
          CI lower bound. This is what production sees, since the pass only fires
          there. Gate = CI lower bound > 0.5.
      (b) gate precision — confirm the REJECTED (high-baseline) papers would
          indeed have been ties/losses, so the gate isn't discarding wins.
    Uses the FIXED threshold from the engine (not tuned here)."""
    from lib.paper.insight_engine import INSIGHT_GATE_THRESHOLD, insight_gate_fires

    rows = []   # (phash, lang, a_ov, b_ov, delta, fired)
    skipped = []
    for phash, lang in hash_lang_pairs:
        gen = _cache_read('gen', phash, lang)
        if not gen or not gen.get('ok'):
            skipped.append((phash, lang, gen.get('why', 'not generated') if gen else 'not generated'))
            continue
        sc = _cache_read('score', phash, lang)
        if not sc:
            skipped.append((phash, lang, 'not scored'))
            continue
        a_ov, _, _ = _agg(sc.get('a') or [])
        b_ov, _, _ = _agg(sc.get('b') or [])
        if a_ov is None or b_ov is None:
            skipped.append((phash, lang, 'scoring empty'))
            continue
        rows.append((phash, lang, a_ov, b_ov, b_ov - a_ov, insight_gate_fires(a_ov)))

    if not rows:
        print('\nNo scorable pairs.', file=sys.stderr)
        for h, lg, why in skipped:
            print(f'  {h[:12]} [{lg}]: {why}', file=sys.stderr)
        return

    def _flag(d):
        return 'WIN' if d > 1e-9 else ('tie' if abs(d) < 1e-9 else 'LOSS')

    fired = [r for r in rows if r[5]]
    rejected = [r for r in rows if not r[5]]

    print(f'\n=== GATED VERDICT (a-priori threshold: baseline <= {INSIGHT_GATE_THRESHOLD}) ===')
    print(f'\n--- FIRED subset (n={len(fired)}) — production behaviour ---')
    for phash, lang, a, b, d, _f in sorted(fired, key=lambda r: r[2]):
        print(f'  {phash[:12]} [{lang:2s}]  A={a:.2f}  B={b:.2f}  Δ={d:+.2f}  [{_flag(d)}]')
    if fired:
        deltas = [r[4] for r in fired]
        point, lower = _bootstrap_winrate(deltas, boot)
        mean_delta = sum(deltas) / len(deltas)
        n_zh = sum(1 for r in fired if r[1] == 'zh')
        print(f'\n  fired n={len(fired)} ({n_zh} zh)   mean Δ={mean_delta:+.3f}   '
              f'win-rate={point:.2f}')
        print(f'  bootstrap 95% CI lower bound: {lower:.2f}   '
              f'({"CLEARS" if lower > 0.5 else "does NOT clear"} the 0.5 gate)'
              f'{"" if len(fired) >= 7 else "  [WARNING: n<7 — not yet at acceptance sample size]"}')

    print(f'\n--- REJECTED subset (n={len(rejected)}) — gate precision check ---')
    print('  (these were withheld; they SHOULD be ties/losses — a WIN here = gate threw away value)')
    rej_wins = 0
    for phash, lang, a, b, d, _f in sorted(rejected, key=lambda r: r[2]):
        fl = _flag(d)
        if fl == 'WIN':
            rej_wins += 1
        print(f'  {phash[:12]} [{lang:2s}]  A={a:.2f}  B={b:.2f}  Δ={d:+.2f}  [{fl}]')
    if rejected:
        precision = 1.0 - (rej_wins / len(rejected))
        print(f'\n  gate precision (rejected that were correctly NOT wins): '
              f'{precision:.2f} ({len(rejected) - rej_wins}/{len(rejected)})')
        if rej_wins:
            print(f'  WARNING: {rej_wins} rejected paper(s) would have WON — gate discards value.')

    if skipped:
        print('\n  skipped:')
        for h, lg, why in skipped:
            print(f'    {h[:12]} [{lg}]: {why}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hashes', help='comma-separated paper_hashes (else auto-select)')
    ap.add_argument('--lang', default='en', choices=['en', 'zh'])
    ap.add_argument('--limit', type=int, default=8, help='max papers when auto-selecting')
    ap.add_argument('--repeats', type=int, default=3, help='rubric runs per arm (averaged)')
    ap.add_argument('--boot', type=int, default=2000, help='bootstrap resamples')
    ap.add_argument('--model', default=None)
    ap.add_argument('--phase', default='all',
                    choices=['gen', 'score', 'summary', 'summary_all', 'gated', 'all'],
                    help='run one resumable phase (default all). gen/score are slow + cached. '
                         'summary_all aggregates EVERY cached (phash,lang) across both languages. '
                         'gated = the GATED verdict (fired-subset CI + gate precision).')
    args = ap.parse_args()

    if args.phase in ('summary_all', 'gated'):
        # Cross-language verdict over every cached pair on disk.
        cached = _scan_cache_pairs()
        print(f'\n=== SECTION-LEVEL INSIGHT A/B — cross-lang over '
              f'{len(cached)} cached pair(s), boot={args.boot} ===')
        if args.phase == 'gated':
            _phase_gated_summary(cached, args.boot)
        else:
            _phase_summary(cached, args.boot)
        return

    hashes = ([h.strip() for h in args.hashes.split(',') if h.strip()]
              if args.hashes else _auto_hashes(args.lang, args.limit))
    if not hashes:
        print('No candidate reports found.', file=sys.stderr)
        sys.exit(2)

    print(f'\n=== SECTION-LEVEL INSIGHT A/B — {len(hashes)} paper(s), '
          f'lang={args.lang}, repeats={args.repeats}, phase={args.phase} ===')
    print('arm A = plain report on 4 insight axes; arm B = insight SECTION on same axes\n')

    if args.phase in ('gen', 'all'):
        _phase_gen(hashes, args.lang, args.model)
    if args.phase in ('score', 'all'):
        _phase_score(hashes, args.lang, args.model, args.repeats)
    if args.phase in ('summary', 'all'):
        _phase_summary([(h, args.lang) for h in hashes], args.boot)


if __name__ == '__main__':
    main()
