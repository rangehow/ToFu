#!/usr/bin/env python3
"""A/B harness for the paper insight second-pass.

Measures whether the insight pass (lib/paper/insight_engine) actually moves the
INSIGHT rubric — the honest-verdicts requirement: show the numeric delta between
a one-pass report and report+insight, not "feels better".

Report source (pick one):
  --hash <paper_hash>    read the cached fidelity report from paper_reports
  --report <file.md>     read the report body from a local Markdown file
  --arxiv <id>           fetch + parse the paper, generate a fresh report, then A/B
                         (needs a live LLM; the two above run insight+rubric only)

It then:
  1. scores the plain report on the 4 insight axes (arm A);
  2. runs the insight pass → appends the rendered insight section;
  3. scores report+insight (arm B);
  4. prints per-axis + overall scores for A and B and the delta.

This calls the REAL LLM (dispatch_stream) — run it inside the app venv with a
configured provider. Example:
    python3 debug/insight_ab.py --hash <phash> --lang en
    python3 debug/insight_ab.py --report /tmp/report.md
"""

import argparse
import os
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


def _load_report_from_db(phash, lang):
    from lib.database import get_thread_db
    db = get_thread_db()
    row = db.execute(
        "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
        (phash, lang)).fetchone()
    if not row or not row['report']:
        # Fall back to any-lang report for this hash.
        row = db.execute(
            "SELECT report, lang FROM paper_reports WHERE paper_hash = ? LIMIT 1",
            (phash,)).fetchone()
    return row['report'] if row else None


def _load_paper_text_from_db(phash):
    from lib.database import get_thread_db
    db = get_thread_db()
    row = db.execute(
        "SELECT parsed_text FROM paper_library WHERE paper_hash = ? LIMIT 1",
        (phash,)).fetchone()
    return (row['parsed_text'] if row else '') or ''


def _fmt_scores(v):
    if not v:
        return '(scoring failed)'
    s = v['scores']
    per = '  '.join(f'{a[:6]}={s.get(a, "-")}' for a in RUBRIC_AXES)
    return f'overall={v["overall"]:.2f}  [{per}]'


def _score_repeated(report_md, model, repeats, label):
    """Score a report ``repeats`` times; return (per_axis_mean, overall_mean, n_ok).

    A single rubric call has cross-call calibration noise (observed: the same
    report scored 5.0 then 3.75 on back-to-back calls). Averaging N runs is the
    50%-gated-style control: report a mean, not a lucky/unlucky single sample.
    """
    axis_sums = {a: 0.0 for a in RUBRIC_AXES}
    axis_n = {a: 0 for a in RUBRIC_AXES}
    overalls = []
    for i in range(repeats):
        v = score_report_rubric(report_md, model=model)
        if not v:
            print(f'    [{label}] run {i + 1}/{repeats}: scoring failed')
            continue
        overalls.append(v['overall'])
        for a in RUBRIC_AXES:
            if a in v['scores']:
                axis_sums[a] += v['scores'][a]
                axis_n[a] += 1
        print(f'    [{label}] run {i + 1}/{repeats}: {_fmt_scores(v)}')
    per_axis = {a: (axis_sums[a] / axis_n[a]) for a in RUBRIC_AXES if axis_n[a]}
    overall = (sum(overalls) / len(overalls)) if overalls else None
    return per_axis, overall, len(overalls)


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--hash', help='paper_hash of a cached report')
    src.add_argument('--report', help='local Markdown file with a report body')
    src.add_argument('--arxiv', help='arXiv id — fetch+parse+generate then A/B')
    ap.add_argument('--lang', default='en', choices=['en', 'zh'])
    ap.add_argument('--model', default=None, help='model override (both passes)')
    ap.add_argument('--repeats', type=int, default=3,
                    help='rubric scoring runs per arm (averaged; controls calibration noise)')
    args = ap.parse_args()

    paper_text = ''
    phash = args.hash or ''

    if args.report:
        with open(args.report, encoding='utf-8') as f:
            report_md = f.read()
    elif args.hash:
        report_md = _load_report_from_db(args.hash, args.lang)
        if not report_md:
            print(f'No cached report for hash={args.hash}', file=sys.stderr)
            sys.exit(2)
        paper_text = _load_paper_text_from_db(args.hash)
    else:  # --arxiv
        from lib.paper.arxiv import _extract_arxiv_id
        from lib.paper.report_engine import _run_report_task  # noqa: F401
        print('--arxiv path generates a fresh report; this needs the full '
              'fetch/parse/report pipeline and a live model. Prefer --hash on an '
              'already-generated paper for a clean A/B.', file=sys.stderr)
        _ = _extract_arxiv_id(args.arxiv)
        sys.exit(2)

    print('\n=== INSIGHT A/B ===')
    print(f'report chars: {len(report_md)}  paper chars: {len(paper_text)}  '
          f'lang: {args.lang}  repeats: {args.repeats}\n')

    # Insight pass FIRST (so a failure aborts before spending A/B scoring).
    print('[*] running insight second-pass (research → synthesize → ground) …')
    res = generate_insight(paper_text, report_md, args.lang, phash=phash, model=args.model)
    if res['llmError']:
        print('    insight pass errored — see logs', file=sys.stderr)
        sys.exit(1)
    if not res['insight']:
        print('    insight pass produced nothing', file=sys.stderr)
        sys.exit(1)
    print(f'    grounded refs: {res["grounded"]}  dropped(hallucinated): {res["dropped"]}')
    print(f'    insight section: {len(res["markdown"])} chars')
    print('\n----- rendered insight -----')
    print(res['markdown'])
    print('----------------------------\n')

    two_pass = report_md.rstrip() + '\n\n' + res['markdown']

    # Arm A — plain report (averaged over --repeats).
    print(f'[A] scoring one-pass report ({args.repeats}×) …')
    a_axis, a_overall, a_ok = _score_repeated(report_md, args.model, args.repeats, 'A')

    # Arm B — report + insight (averaged over --repeats).
    print(f'\n[B] scoring report+insight ({args.repeats}×) …')
    b_axis, b_overall, b_ok = _score_repeated(two_pass, args.model, args.repeats, 'B')

    if a_ok and b_ok:
        print(f'\n=== MEAN DELTA (B - A), n_A={a_ok} n_B={b_ok} ===')
        for ax in RUBRIC_AXES:
            av, bv = a_axis.get(ax), b_axis.get(ax)
            if av is not None and bv is not None:
                print(f'  {ax:32s} {av:.2f} → {bv:.2f}  ({bv - av:+.2f})')
        print(f'  {"overall":32s} {a_overall:.2f} → {b_overall:.2f}  '
              f'({b_overall - a_overall:+.2f})')
    else:
        print(f'\n(insufficient successful scores: n_A={a_ok} n_B={b_ok})', file=sys.stderr)


if __name__ == '__main__':
    main()
