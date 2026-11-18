"""Calibrate the document pipeline's review gate on real ICLR decisions.

Usage:
  python3 evaluations/paper_review_calibration/run.py [--per-class N]
      [--judge-model gpt-5.5] [--endpoint URL] [--out DIR] [--write-calibration]

Data: ICLR 2025 submissions with public decisions, from the Hugging Face
parquet mirror of OpenReview (``ulab-ai/ResearchArcade-openreview-papers``
for decisions, ``ulab-ai/ResearchArcade-openreview-paragraphs`` for full
text). The OpenReview API answers this host with a bot challenge, and the
datasets-server query API failed with transient Hub auth errors, so the
parquet files are downloaded once (shared HTTP transport, cached under
``--out``) and read by ``prepare_parquet.py`` in an isolated ``uv`` env with
pyarrow, an evaluation-only dependency.

Method: N accepted (Oral/Spotlight/Poster) and N rejected papers are picked
deterministically by SHA-256 of their OpenReview id (papers whose mirrored
text has fewer than 10 paragraphs are skipped as extraction failures). Each
paper's body text (up to References) goes through exactly the pipeline's
review prompt (``lib.longform.documents.review.review_messages``, paper
checklist included) to the judge model, through the running server's model
routing. Papers split by id hash into a calibration half and a held-out
half. The threshold on ``overall`` maximizes Youden's J on the calibration
half; the held-out half reports AUC (seeded bootstrap 95% interval), accuracy
and balanced accuracy at that threshold. ``--write-calibration`` commits the
summary the review gate reads.

Caveat: ICLR 2025 papers may be in the judge's training data; the result
measures agreement with real decisions, not unseen-paper generalization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_HF = 'https://huggingface.co/datasets'
_PAPERS = 'ulab-ai/ResearchArcade-openreview-papers'
_PARAGRAPHS = 'ulab-ai/ResearchArcade-openreview-paragraphs'
_FILES = {
    'papers.parquet': f'{_HF}/{_PAPERS}/resolve/main/data/train-00000-of-00001.parquet',
    'paragraphs-0.parquet': f'{_HF}/{_PARAGRAPHS}/resolve/main/data/train-00000-of-00002.parquet',
    'paragraphs-1.parquet': f'{_HF}/{_PARAGRAPHS}/resolve/main/data/train-00001-of-00002.parquet',
}
_BOOTSTRAP = 2000


def _download(url: str, target: Path) -> Path:
    if target.is_file() and target.stat().st_size:
        return target
    from lib.http_transport.client import http_get
    partial = target.with_suffix('.part')
    with http_get(url, timeout=600, stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError(f'{url}: HTTP {response.status_code}')
        with partial.open('wb') as sink:
            for chunk in response.iter_content(chunk_size=1 << 20):
                sink.write(chunk)
    partial.replace(target)
    return target


def select_papers(out: Path, per_class: int) -> list[dict]:
    cache = out / 'data'
    cache.mkdir(parents=True, exist_ok=True)
    paths = {name: _download(url, cache / name) for name, url in _FILES.items()}
    selection = out / f'selection-{per_class}.jsonl'
    if not selection.is_file():
        subprocess.run(
            ['uv', 'run', '--no-project', '--with', 'pyarrow', 'python',
             str(Path(__file__).with_name('prepare_parquet.py')),
             str(paths['papers.parquet']), str(selection), str(per_class),
             str(paths['paragraphs-0.parquet']), str(paths['paragraphs-1.parquet'])],
            check=True)
    return [json.loads(line) for line in selection.read_text().splitlines()]


def _key(paper_id: str) -> str:
    return hashlib.sha256(paper_id.encode()).hexdigest()


def judge(paper: dict, *, model: str) -> dict:
    from lib.llm_dispatch.api import dispatch_chat
    from lib.longform.documents.review import parse_review, review_messages
    from lib.longform.documents.rules import rules_for
    from lib.longform.documents.types import document_type
    doc_type = document_type('paper')
    checklist = tuple(rule for rule in rules_for('paper') if rule.kind == 'checklist')
    messages = review_messages(doc_type, venue='ICLR 2025', text=paper['text'],
                               checklist=checklist)
    content, usage = dispatch_chat(messages, max_tokens=None, temperature=0,
                                   prefer_model=model)
    review, errors = parse_review(content, doc_type, checklist=checklist)
    return {key: paper[key] for key in ('id', 'title', 'decision', 'accepted')} | {
        'chars': len(paper['text']), 'overall': review.get('overall'),
        'scores': review.get('scores'), 'errors': errors,
        'usage': {k: (usage or {}).get(k) for k in ('prompt_tokens', 'completion_tokens')}}


def auc(rows: list[dict]) -> float:
    pos = [r['overall'] for r in rows if r['accepted']]
    neg = [r['overall'] for r in rows if not r['accepted']]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def best_threshold(rows: list[dict]) -> tuple[int, float]:
    best = (None, -1.0)
    for threshold in sorted({r['overall'] for r in rows}):
        tpr = sum(r['overall'] >= threshold for r in rows if r['accepted']) / \
            max(1, sum(r['accepted'] for r in rows))
        fpr = sum(r['overall'] >= threshold for r in rows if not r['accepted']) / \
            max(1, sum(not r['accepted'] for r in rows))
        if tpr - fpr > best[1]:
            best = (threshold, tpr - fpr)
    return best


def at_threshold(rows: list[dict], threshold: int) -> dict:
    predicted = [(r['overall'] >= threshold, bool(r['accepted'])) for r in rows]
    accuracy = sum(p == a for p, a in predicted) / len(predicted)
    tpr = sum(p and a for p, a in predicted) / max(1, sum(a for _p, a in predicted))
    tnr = sum((not p) and (not a) for p, a in predicted) / \
        max(1, sum(not a for _p, a in predicted))
    return {'accuracy': accuracy, 'balanced_accuracy': (tpr + tnr) / 2,
            'true_positive_rate': tpr, 'true_negative_rate': tnr}


def bootstrap_auc(rows: list[dict]) -> list[float]:
    rng = random.Random(20261118)
    pos = [r for r in rows if r['accepted']]
    neg = [r for r in rows if not r['accepted']]
    values = sorted(auc([rng.choice(pos) for _ in pos] + [rng.choice(neg) for _ in neg])
                    for _ in range(_BOOTSTRAP))
    return [values[int(0.025 * _BOOTSTRAP)], values[int(0.975 * _BOOTSTRAP) - 1]]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--per-class', type=int, default=50)
    parser.add_argument('--judge-model', default='gpt-5.5')
    parser.add_argument('--endpoint', default='http://127.0.0.1:15000/v1/chat/completions')
    parser.add_argument('--out', default=str(ROOT / '.eval-runs' / 'paper-review-calibration'))
    parser.add_argument('--in-flight', type=int, default=4)
    parser.add_argument('--write-calibration', action='store_true')
    args = parser.parse_args(argv)
    from evaluations.slides_bench import llm_bridge
    llm_bridge.ENDPOINT = args.endpoint
    llm_bridge.install(args.judge_model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / f'rows-{args.judge_model}.jsonl'
    done = {}
    if rows_path.is_file():
        for line in rows_path.read_text().splitlines():
            row = json.loads(line)
            if not row.get('errors') and isinstance(row.get('overall'), int):
                done[row['id']] = row
    papers = select_papers(out, args.per_class)
    todo = [paper for paper in papers if paper['id'] not in done]
    print(f'{len(papers)} papers selected, {len(todo)} to judge', flush=True)

    def work(paper):
        try:
            return judge(paper, model=args.judge_model)
        except Exception as exc:
            return {key: paper[key] for key in ('id', 'title', 'decision', 'accepted')} | {
                'overall': None, 'errors': [f'{type(exc).__name__}: {exc}'[:500]]}
    with ThreadPoolExecutor(args.in_flight) as pool, rows_path.open('a') as sink:
        for row in pool.map(work, todo):
            sink.write(json.dumps(row) + '\n')
            sink.flush()
            print(row['id'], row['accepted'], row.get('overall'), row.get('errors') or '',
                  flush=True)
            if not row.get('errors') and isinstance(row.get('overall'), int):
                done[row['id']] = row
    rows = [done[p['id']] for p in papers if p['id'] in done]
    calibration = [r for r in rows if int(_key(r['id'])[-1], 16) % 2 == 0]
    heldout = [r for r in rows if int(_key(r['id'])[-1], 16) % 2 == 1]
    threshold, youden = best_threshold(calibration)
    from lib.longform.documents.review import prompt_digest
    from lib.longform.documents.rules import rules_for
    from lib.longform.documents.types import document_type
    checklist = tuple(rule for rule in rules_for('paper') if rule.kind == 'checklist')
    summary = {
        'dataset': f'ICLR 2025 public decisions via HF parquet {_PAPERS} + {_PARAGRAPHS}',
        'judge_model': args.judge_model,
        'prompt_digest': prompt_digest(document_type('paper'), checklist),
        'per_class_requested': args.per_class, 'judged': len(rows),
        'failed': len(papers) - len(rows),
        'n_calibration': len(calibration), 'n_heldout': len(heldout),
        'heldout_accepted': sum(r['accepted'] for r in heldout),
        'threshold': threshold, 'calibration_youden_j': youden,
        'calibration_auc': auc(calibration),
        'heldout_auc': auc(heldout), 'heldout_auc_ci95': bootstrap_auc(heldout),
        **{f'heldout_{k}': v for k, v in at_threshold(heldout, threshold).items()},
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    (out / f'summary-{args.judge_model}.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    if args.write_calibration:
        from lib.longform.documents.review import CALIBRATION_PATH
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_PATH.write_text(json.dumps(summary, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
