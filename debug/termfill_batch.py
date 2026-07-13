#!/usr/bin/env python3
"""Batch measurement for the terminology definition-backfill second pass.

The re-audit gate PROVES self-containment (every gap term ends up with a row),
but it CANNOT prove the definitions are TRUE — a plausible-but-wrong definition
passes the gate while misleading the reader. So this harness measures the two
things that actually gate the rollout decision:

  (a) CLOSURE RATE — over gappy reports, the fraction of runs where the backfill
      produces an addendum AND the re-audit gap set drops to empty (fully
      self-contained) or strictly shrinks (partial). This is the mechanism's
      throughput on real reports.

  (b) DEFINITION QUALITY — an LLM-judge rubric (correctness + readability +
      self-containment) scoring each backfilled definition against the report it
      came from. The gate proves self-containment structurally; this catches the
      one failure it can't: a confident wrong definition.

CRITICAL PRECONDITION this harness also surfaces (measure-first): the DETECTOR's
gap count on real reports. If the detector over-flags (well-known field
acronyms, inline-defined terms, cited method names), the backfill is being asked
to "define" 40+ non-gaps per report — that is a DETECTOR precision problem, not
a backfill quality problem, and it must be fixed before the cure is turned on.
The `census` phase quantifies this with NO LLM cost.

Resumable on-disk cache (live gen is slow): phases census → gen → score →
summary. Mirrors debug/insight_batch.py.

Usage (needs a live model + the app venv):
  python3 debug/termfill_batch.py --phase census                 # free, no LLM
  python3 debug/termfill_batch.py --phase gen   --limit 8 --lang en
  python3 debug/termfill_batch.py --phase score --repeats 2
  python3 debug/termfill_batch.py --phase summary
  python3 debug/termfill_batch.py --phase all   --limit 6
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('TRADING_ENABLED', '0')

from lib.log import get_logger  # noqa: E402
from lib.paper.terminology_audit import build_terminology_audit  # noqa: E402
from lib.paper.terminology_backfill import build_backfill_addendum  # noqa: E402

logger = get_logger(__name__)

_SKIP_HASH_PREFIXES = ('probe', 'test', 'dummy')
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.termfill_cache')


def _db():
    from lib.database import get_thread_db
    return get_thread_db()


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
        logger.warning('[termfill_batch] cache read failed %s: %s', p, e)
        return None


def _cache_write(kind, phash, lang, obj):
    try:
        with open(_cache_path(kind, phash, lang), 'w', encoding='utf-8') as f:
            json.dump(obj, f)
    except Exception as e:
        logger.warning('[termfill_batch] cache write failed: %s', e)


def _gappy_reports(langs, limit):
    """Real plain reports with >0 audit gaps, biggest-first, capped at ``limit``."""
    ph = ','.join('?' * len(langs))
    rows = _db().execute(
        f"SELECT paper_hash, lang, report FROM paper_reports "
        f"WHERE lang IN ({ph}) AND length(report) > 3000", tuple(langs)).fetchall()
    out = []
    for r in rows or []:
        if any(r['paper_hash'].startswith(p) for p in _SKIP_HASH_PREFIXES):
            continue
        audit = build_terminology_audit(r['report'])
        if not audit:
            continue
        gaps = audit['counts']['missing'] + audit['counts']['dangling']
        out.append((r['paper_hash'], r['lang'], gaps))
    out.sort(key=lambda x: -x[2])
    return out[:limit]


def _load_report(phash, lang):
    row = _db().execute(
        "SELECT report FROM paper_reports WHERE paper_hash = ? AND lang = ?",
        (phash, lang)).fetchone()
    return (row['report'] if row else '') or ''


def _gaps(audit):
    if not audit:
        return set()
    return ({m['term'].upper() for m in audit['missing']}
            | {d['referencedTerm'].upper() for d in audit['dangling']})


# ── Phase: census (deterministic, no LLM) ───────────────────────────────────

def _phase_census(langs):
    """Quantify the DETECTOR's gap distribution + false-positive composition."""
    ph = ','.join('?' * len(langs))
    rows = _db().execute(
        f"SELECT paper_hash, lang, report FROM paper_reports "
        f"WHERE lang IN ({ph}) AND length(report) > 3000", tuple(langs)).fetchall()
    buckets = {'0': 0, '1-2': 0, '3-5': 0, '6-15': 0, '16+': 0}
    per_report = []
    for r in rows or []:
        if any(r['paper_hash'].startswith(p) for p in _SKIP_HASH_PREFIXES):
            continue
        a = build_terminology_audit(r['report'])
        n = 0 if not a else a['counts']['missing'] + a['counts']['dangling']
        if n == 0:
            buckets['0'] += 1
        elif n <= 2:
            buckets['1-2'] += 1
        elif n <= 5:
            buckets['3-5'] += 1
        elif n <= 15:
            buckets['6-15'] += 1
        else:
            buckets['16+'] += 1
        per_report.append((r['paper_hash'][:12], r['lang'], n))
    print(f'\n=== DETECTOR GAP CENSUS ({len(per_report)} plain reports) ===')
    print('  gaps/report buckets:', buckets)
    if per_report:
        gaps = [n for _h, _l, n in per_report if n]
        if gaps:
            print(f'  gappy reports: {len(gaps)}  |  '
                  f'gaps/report min={min(gaps)} median={sorted(gaps)[len(gaps)//2]} max={max(gaps)}')
    print('\n  A gap distribution dominated by high counts (16+) is a DETECTOR '
          'PRECISION signal, not a backfill-quality one — inspect the flagged '
          'terms (well-known acronyms? inline-defined? cited method names?) '
          'before turning the cure on.')


# ── Phase: gen (LLM — build the addendum, measure closure) ──────────────────

def _phase_gen(reports, lang, model):
    for i, (phash, _lg, gaps) in enumerate(reports, 1):
        if _cache_read('gen', phash, lang) is not None:
            print(f'[gen {i}/{len(reports)}] {phash[:12]} — cached, skip')
            continue
        report_md = _load_report(phash, lang)
        audit = build_terminology_audit(report_md)
        if not audit:
            _cache_write('gen', phash, lang, {'ok': False, 'why': 'no gaps'})
            continue
        before = _gaps(audit)
        print(f'[gen {i}/{len(reports)}] {phash[:12]} — {len(before)} gaps; backfilling …')
        addendum = ''
        try:
            addendum = build_backfill_addendum(report_md, audit, lang, model=model)
        except Exception as e:
            logger.warning('[termfill_batch] backfill failed %s: %s', phash, e)
        after = _gaps(build_terminology_audit(
            report_md.rstrip() + '\n\n' + addendum + '\n')) if addendum else before
        closed = before - after
        _cache_write('gen', phash, lang, {
            'ok': bool(addendum),
            'gaps_before': len(before), 'gaps_after': len(after),
            'closed': len(closed), 'fully_closed': len(after) == 0,
            'addendum': addendum,
            'defined_terms': sorted(closed),
        })
        print(f'        {"ok" if addendum else "EMPTY"} — before={len(before)} '
              f'after={len(after)} closed={len(closed)} '
              f'{"[FULLY SELF-CONTAINED]" if len(after) == 0 else ""}')


# ── Phase: score (LLM-judge definition quality) ─────────────────────────────

_QUALITY_SYS = (
    'You are grading auto-generated glossary definitions for a paper explainer. '
    'For each {term: definition}, judge on three axes, 1-5 each:\n'
    '  correctness: is the definition factually right for how this field/paper '
    'uses the term? (5=correct, 1=wrong/misleading)\n'
    '  readability: would a non-expert reader understand it? (5=clear, 1=opaque)\n'
    '  self_contained: does it avoid leaning on OTHER undefined jargon? (5=yes)\n'
    'Return STRICT JSON ONLY: {"TERM": {"correctness":n,"readability":n,'
    '"self_contained":n}, ...} for every term given. No prose.'
)
_Q_AXES = ('correctness', 'readability', 'self_contained')


def _parse_json(text):
    if not text:
        return {}
    s = re.sub(r'^```(?:json)?\s*', '', text.strip())
    s = re.sub(r'\s*```$', '', s)
    try:
        o = json.loads(s)
        return o if isinstance(o, dict) else {}
    except Exception:
        m = re.search(r'\{.*\}', s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


def _addendum_rows(addendum):
    """Parse the addendum glossary table → {term: definition}."""
    rows = {}
    for m in re.finditer(r'^\|([^|\n]+)\|([^|\n]+)\|\s*$', addendum, re.MULTILINE):
        t = m.group(1).strip()
        d = m.group(2).strip()
        if t and d and t.lower() not in ('term', '术语') and not re.match(r'^[\s:\-]+$', t):
            rows[t] = d
    return rows


def _phase_score(reports, lang, model, repeats):
    from lib.llm_dispatch.api import dispatch_chat
    for i, (phash, _lg, _g) in enumerate(reports, 1):
        gen = _cache_read('gen', phash, lang)
        if not gen or not gen.get('ok') or not gen.get('addendum'):
            continue
        if _cache_read('score', phash, lang):
            print(f'[score {i}/{len(reports)}] {phash[:12]} — cached, skip')
            continue
        rows = _addendum_rows(gen['addendum'])
        if not rows:
            _cache_write('score', phash, lang, {'scores': {}})
            continue
        report_md = _load_report(phash, lang)
        user = ('Report (context):\n\n' + report_md[:40000] +
                '\n\nDefinitions to grade:\n' + json.dumps(rows, ensure_ascii=False))
        print(f'[score {i}/{len(reports)}] {phash[:12]} — grading {len(rows)} defs ×{repeats} …')
        agg = {}
        for _ in range(repeats):
            try:
                content, _u = dispatch_chat(
                    [{'role': 'system', 'content': _QUALITY_SYS},
                     {'role': 'user', 'content': user}],
                    max_tokens=2000, temperature=0, capability='cheap',
                    prefer_model=model, log_prefix='[termfill_batch:score]')
            except Exception as e:
                logger.warning('[termfill_batch] score dispatch failed: %s', e)
                continue
            got = _parse_json(content)
            for term, ax in got.items():
                if not isinstance(ax, dict):
                    continue
                agg.setdefault(term, {a: [] for a in _Q_AXES})
                for a in _Q_AXES:
                    try:
                        agg[term][a].append(float(ax.get(a)))
                    except (TypeError, ValueError):
                        pass
        scores = {t: {a: (sum(v[a]) / len(v[a]) if v[a] else None) for a in _Q_AXES}
                  for t, v in agg.items()}
        _cache_write('score', phash, lang, {'scores': scores})
        allc = [s['correctness'] for s in scores.values() if s['correctness'] is not None]
        if allc:
            print(f'        mean correctness={sum(allc)/len(allc):.2f} over {len(allc)} defs')


def _phase_summary():
    pairs = []
    for fn in sorted(os.listdir(_CACHE_DIR)) if os.path.isdir(_CACHE_DIR) else []:
        if not fn.endswith('.gen.json'):
            continue
        phash, _, lang = fn[:-len('.gen.json')].rpartition('_')
        gen = _cache_read('gen', phash, lang)
        if gen:
            pairs.append((phash, lang, gen, _cache_read('score', phash, lang)))

    if not pairs:
        print('\nNo cached runs. Run --phase gen first.', file=sys.stderr)
        return

    print(f'\n=== CLOSURE (n={len(pairs)} gappy reports) ===')
    fully = 0
    total_before = total_closed = 0
    for phash, lang, gen, _sc in sorted(pairs, key=lambda p: -(p[2].get('gaps_before') or 0)):
        b, a = gen.get('gaps_before', 0), gen.get('gaps_after', 0)
        c = gen.get('closed', 0)
        total_before += b
        total_closed += c
        if gen.get('fully_closed'):
            fully += 1
        print(f'  {phash[:12]} [{lang:2s}]  before={b:3d} after={a:3d} closed={c:3d}'
              f'{"  [FULLY SELF-CONTAINED]" if gen.get("fully_closed") else ""}')
    print(f'\n  reports fully self-contained after backfill: {fully}/{len(pairs)} '
          f'({100*fully/len(pairs):.0f}%)')
    if total_before:
        print(f'  aggregate gap closure: {total_closed}/{total_before} '
              f'({100*total_closed/total_before:.0f}% of flagged gaps got a definition)')

    # Quality.
    all_scores = {a: [] for a in _Q_AXES}
    n_defs = 0
    for _h, _l, _gen, sc in pairs:
        if not sc:
            continue
        for _t, ax in (sc.get('scores') or {}).items():
            n_defs += 1
            for a in _Q_AXES:
                if ax.get(a) is not None:
                    all_scores[a].append(ax[a])
    print(f'\n=== DEFINITION QUALITY (LLM-judge, {n_defs} definitions scored) ===')
    if n_defs:
        for a in _Q_AXES:
            v = all_scores[a]
            if v:
                lo = sum(1 for x in v if x <= 2)
                print(f'  {a:16s} mean={sum(v)/len(v):.2f}/5   '
                      f'(<=2: {lo}/{len(v)} = {100*lo/len(v):.0f}% poor)')
        corr = all_scores['correctness']
        if corr:
            print(f'\n  VERDICT INPUT: {100*sum(1 for x in corr if x>=4)/len(corr):.0f}% of '
                  f'definitions rated correct (>=4); '
                  f'{100*sum(1 for x in corr if x<=2)/len(corr):.0f}% wrong/misleading (<=2).')
    else:
        print('  (no quality scores cached — run --phase score)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hashes', help='comma-separated paper_hashes (else auto by gap count)')
    ap.add_argument('--lang', default='en', choices=['en', 'zh'])
    ap.add_argument('--langs', default=None, help='comma list for census/auto (default: en,zh)')
    ap.add_argument('--limit', type=int, default=8)
    ap.add_argument('--repeats', type=int, default=2)
    ap.add_argument('--model', default=None)
    ap.add_argument('--phase', default='all',
                    choices=['census', 'gen', 'score', 'summary', 'all'])
    args = ap.parse_args()
    langs = [x.strip() for x in (args.langs or 'en,zh').split(',') if x.strip()]

    if args.phase == 'census':
        _phase_census(langs)
        return
    if args.phase == 'summary':
        _phase_summary()
        return

    if args.hashes:
        reports = [(h.strip(), args.lang, 0) for h in args.hashes.split(',') if h.strip()]
    else:
        reports = _gappy_reports([args.lang], args.limit)
    if not reports:
        print('No gappy reports found.', file=sys.stderr)
        sys.exit(2)
    print(f'\n=== TERMFILL BATCH — {len(reports)} report(s), lang={args.lang}, '
          f'phase={args.phase} ===')

    if args.phase in ('gen', 'all'):
        _phase_gen(reports, args.lang, args.model)
    if args.phase in ('score', 'all'):
        _phase_score(reports, args.lang, args.model, args.repeats)
    if args.phase in ('summary', 'all'):
        _phase_summary()


if __name__ == '__main__':
    main()
