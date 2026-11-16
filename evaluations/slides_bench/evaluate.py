"""Score one benchmark run: blind vision judge + text content audit per deck.

Usage: python -m evaluations.slides_bench.evaluate <run-name> [--arms a,b,c]
       [--docs d1,d2] [--seed S]

Reads ``$SLIDES_BENCH_DATA/runs/<run>/<arm>/<doc>/render/slide-*.png`` (the
shared LibreOffice renders) and the arm's PPTX. A Tofu deck whose pipeline
refused publication has no PPTX: it is recorded as undelivered, and its
Chromium previews (``render_preview/``) are judged separately so the report
can still diagnose it; undelivered decks never enter the arm averages.
Writes ``scores.json`` and ``scores.md`` under the run directory.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics

from evaluations.slides_bench import score
from evaluations.slides_bench.corpus import data_root, document_ids, load_document


def _deck(run_dir: str, arm: str, doc_id: str) -> dict | None:
    base = os.path.join(run_dir, arm, doc_id)
    pptx = (glob.glob(os.path.join(base, 'deck.pptx'))
            + glob.glob(os.path.join(base, 'deck', '*.pptx')))
    pngs = sorted(glob.glob(os.path.join(base, 'render', 'slide-*.png')))
    if pptx and pngs:
        return {'arm': arm, 'pptx': pptx[0], 'pngs': pngs, 'delivered': True}
    forced = sorted(glob.glob(os.path.join(base, 'render_forced', 'slide-*.png')))
    if forced and os.path.isfile(os.path.join(base, 'forced.pptx')):
        return {'arm': arm, 'pptx': os.path.join(base, 'forced.pptx'),
                'pngs': forced, 'delivered': False}
    return None


def _cached(path: str, compute):
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    value = compute()
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(value, fh, ensure_ascii=False, indent=1)
    return value


def evaluate(run: str, arms: list[str], docs: list[str], seed: str) -> dict:
    run_dir = os.path.join(data_root(), 'runs', run)
    rows = []
    for doc_id in docs:
        doc = load_document(doc_id)
        for label, arm in score.blind_order(doc_id, arms, seed):
            deck = _deck(run_dir, arm, doc_id)
            if deck is None:
                rows.append({'doc': doc_id, 'arm': arm, 'delivered': False,
                             'missing': True})
                continue
            # Verdicts belong to this run: a deck linked from another run
            # (re-judged in a new pass) must not read or write that run's cache.
            base = os.path.join(run_dir, arm, doc_id)
            judged = _cached(
                os.path.join(base, f'judge-{seed}.json'),
                lambda: score.judge_deck(doc['text'], deck['pngs'],
                                         label=label, lang=doc['lang']))
            audit = None
            if deck['pptx']:
                audit = _cached(
                    os.path.join(base, f'audit-{seed}.json'),
                    lambda: score.audit_deck(doc['text'],
                                             score.slide_texts(deck['pptx'])))
            rows.append({'doc': doc_id, 'arm': arm, 'label': label,
                         'delivered': deck['delivered'],
                         'slides': len(deck['pngs']), 'judge': judged,
                         'audit': audit})
            print(f'{doc_id} {arm} delivered={deck["delivered"]} '
                  f'{judged["scores"]}', flush=True)
    summary = _summary(rows, arms)
    out = {'run': run, 'seed': seed, 'judge_model': score.JUDGE_MODEL,
           'rubric': score.RUBRIC, 'rows': rows, 'summary': summary}
    with open(os.path.join(run_dir, 'scores.json'), 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(run_dir, 'scores.md'), 'w', encoding='utf-8') as fh:
        fh.write(markdown(out))
    return out


def _mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.mean(values), 2) if values else None


def _summary(rows: list[dict], arms: list[str]) -> dict:
    out = {}
    for arm in arms:
        mine = [r for r in rows if r['arm'] == arm]
        delivered = [r for r in mine if r.get('delivered')]
        judged = [r for r in mine if r.get('judge')]
        entry = {'decks': len(mine), 'delivered': len(delivered),
                 'overall_incl_undelivered': _mean([
                     statistics.mean(r['judge']['scores'].values())
                     for r in judged]),
                 'content_incl_undelivered': _mean([
                     statistics.mean(r['judge']['scores'][n]
                                     for n in score.CONTENT_DIMENSIONS)
                     for r in judged])}
        for name in score.DIMENSIONS:
            entry[name] = _mean([r['judge']['scores'][name] for r in delivered])
        entry['all_judged'] = {
            name: _mean([r['judge']['scores'][name] for r in judged])
            for name in score.DIMENSIONS}
        entry['content_mean'] = _mean([
            statistics.mean(r['judge']['scores'][n]
                            for n in score.CONTENT_DIMENSIONS)
            for r in delivered])
        entry['overall_mean'] = _mean([
            statistics.mean(r['judge']['scores'].values()) for r in delivered])
        for key in ('specific_per_slide', 'specific_share',
                    'unsupported_per_slide', 'filler_per_slide',
                    'assertion_title_share', 'numbers_not_in_source_count'):
            entry[key] = _mean([r['audit'][key] for r in delivered
                                if r.get('audit')])
        out[arm] = entry
    return out


def markdown(result: dict) -> str:
    summary = result['summary']
    arms = list(summary)
    columns = (['delivered', 'content_incl_undelivered',
                'overall_incl_undelivered'] + list(score.DIMENSIONS)
               + ['content_mean', 'overall_mean', 'specific_per_slide',
                  'specific_share', 'unsupported_per_slide',
                  'filler_per_slide', 'assertion_title_share',
                  'numbers_not_in_source_count'])
    lines = [f'# Slides benchmark run `{result["run"]}`', '',
             f'Judge model {result["judge_model"]} via Tofu routing; blind '
             f'labels, seeded order `{result["seed"]}`. Means over delivered '
             'decks; scores 1-10; audit counts per slide.', '',
             '| metric | ' + ' | '.join(arms) + ' |',
             '|---|' + '---|' * len(arms)]
    for column in columns:
        cells = []
        for arm in arms:
            value = summary[arm].get(column)
            if column == 'delivered':
                value = f'{summary[arm]["delivered"]}/{summary[arm]["decks"]}'
            cells.append('' if value is None else str(value))
        lines.append(f'| {column} | ' + ' | '.join(cells) + ' |')
    lines += ['', '## Means over every judged deck (incl. undelivered)', '',
              '| dimension | ' + ' | '.join(arms) + ' |',
              '|---|' + '---|' * len(arms)]
    for name in score.DIMENSIONS:
        cells = [str(summary[arm]['all_judged'].get(name) or '')
                 for arm in arms]
        lines.append(f'| {name} | ' + ' | '.join(cells) + ' |')
    lines += ['', '## Per deck', '',
              '| doc | arm | delivered | content | visual | overall | '
              'specific/slide | unsupported/slide | judge summary |',
              '|---|---|---|---|---|---|---|---|---|']
    for row in result['rows']:
        if row.get('missing'):
            lines.append(f'| {row["doc"]} | {row["arm"]} | no deck | | | | | | |')
            continue
        scores = row['judge']['scores']
        content = round(sum(scores[n] for n in score.CONTENT_DIMENSIONS) / 5, 1)
        visual = round((scores['visual_design']
                        + scores['layout_readability']) / 2, 1)
        overall = round(sum(scores.values()) / len(scores), 1)
        audit = row.get('audit') or {}
        summary_text = str(row['judge'].get('summary') or '').replace('|', '/')
        lines.append(
            f'| {row["doc"]} | {row["arm"]} | {row["delivered"]} | {content} | '
            f'{visual} | {overall} | {audit.get("specific_per_slide", "")} | '
            f'{audit.get("unsupported_per_slide", "")} | {summary_text} |')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run')
    parser.add_argument('--arms', default='tofu,slidedeckai,pptagent')
    parser.add_argument('--docs', default='')
    parser.add_argument('--seed', default='slides-bench-v1')
    args = parser.parse_args()
    evaluate(args.run, args.arms.split(','),
             args.docs.split(',') if args.docs else document_ids(), args.seed)
