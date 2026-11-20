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
half; the held-out half reports diagnostic AUC (seeded bootstrap 95% interval),
accuracy and balanced accuracy at that threshold. The same-year hash partition
is not an out-of-time test; ``--write-calibration`` refuses publication until a
licensed time-separated corpus and contamination review exist.

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
import tempfile
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
_NORMAL_FINISH_REASONS = ('stop', 'end_turn')
_COMPLETION_VALIDATION = 'compatible_text_choice_v1'


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


def _file_digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _selected_rows(selection: Path, per_class: int) -> list[dict]:
    labels = {'ICLR 2025 Oral': True, 'ICLR 2025 Spotlight': True,
              'ICLR 2025 Poster': True, 'Rejected_Submission': False}
    rows = [json.loads(line) for line in selection.read_text().splitlines()]
    identities = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Selected corpus rows must be objects')
        for field in ('id', 'title', 'text'):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f'Selected corpus requires nonempty string {field}')
        if row['id'] in identities:
            raise ValueError('Duplicate selected corpus paper ID')
        identities.add(row['id'])
        decision = row.get('decision')
        if not isinstance(decision, str) or decision not in labels:
            raise ValueError(f'Unsupported selected corpus decision: {decision!r}')
        accepted = row.get('accepted')
        if type(accepted) not in (bool, int) or accepted not in (0, 1) or accepted != labels[decision]:
            raise ValueError(f'Contradictory or invalid accepted label for decision {decision!r}')
        row['accepted'] = labels[decision]
    if any(sum(row['accepted'] == label for row in rows) != per_class
           for label in (True, False)):
        raise ValueError(f'Insufficient or invalid selection: require exactly {per_class} '
                         'papers per class')
    return rows


def select_papers(out: Path, per_class: int) -> list[dict]:
    if type(per_class) is not int or per_class <= 0:
        raise ValueError('per_class must be a positive integer')
    cache = out / 'data'
    cache.mkdir(parents=True, exist_ok=True)
    paths = {name: _download(url, cache / name) for name, url in _FILES.items()}
    selector = Path(__file__).with_name('prepare_parquet.py')
    identity = {'per_class': per_class,
                'parquet_sha256': {name: _file_digest(path) for name, path in paths.items()},
                'selector_sha256': _file_digest(selector)}
    namespace = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    selection = out / f'selection-{per_class}-{namespace}.jsonl'
    receipt_path = selection.with_suffix('.receipt.json')
    if selection.is_file() and receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text())
        except json.JSONDecodeError:
            receipt = None
        if receipt == {**identity, 'status': 'completed',
                       'selection_sha256': _file_digest(selection)}:
            return [row | {'selection_receipt': receipt}
                    for row in _selected_rows(selection, per_class)]
    with tempfile.TemporaryDirectory(dir=out) as staging:
        prepared = Path(staging) / selection.name
        subprocess.run(
            ['uv', 'run', '--no-project', '--with', 'pyarrow', 'python', str(selector),
             str(paths['papers.parquet']), str(prepared), str(per_class),
             str(paths['paragraphs-0.parquet']), str(paths['paragraphs-1.parquet'])],
            check=True)
        rows = _selected_rows(prepared, per_class)
        if identity != {'per_class': per_class,
                        'parquet_sha256': {name: _file_digest(path) for name, path in paths.items()},
                        'selector_sha256': _file_digest(selector)}:
            raise ValueError('Selection inputs changed during preparation')
        receipt = {**identity, 'status': 'completed', 'selection_sha256': _file_digest(prepared)}
        prepared_receipt = Path(staging) / receipt_path.name
        prepared_receipt.write_text(json.dumps(receipt, sort_keys=True))
        prepared.replace(selection)
        prepared_receipt.replace(receipt_path)
    return [row | {'selection_receipt': receipt} for row in rows]


def _completed_selection_receipt(receipt) -> bool:
    if not isinstance(receipt, dict):
        return False
    hashes = receipt.get('parquet_sha256')
    return (receipt.get('status') == 'completed'
            and type(receipt.get('per_class')) is int and receipt['per_class'] > 0
            and isinstance(hashes, dict) and set(hashes) == set(_FILES)
            and all(isinstance(value, str) and len(value) == 64
                    and all(char in '0123456789abcdef' for char in value)
                    for value in [receipt.get('selector_sha256'),
                                  receipt.get('selection_sha256'), *hashes.values()]))


def _key(paper_id: str) -> str:
    return hashlib.sha256(paper_id.encode()).hexdigest()


def separated_split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Hold out complete conference-years; refuse duplicates and unavailable dates."""
    if not rows or any(not row.get('venue') or not isinstance(row.get('year'), int)
                       for row in rows):
        raise ValueError('Conference and year are required for time-separated evaluation')
    identities = [row['id'] for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError('Duplicate paper IDs can leak across evaluation partitions')
    names = [row['title'].casefold().strip() for row in rows]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate paper titles can leak across evaluation partitions')
    years = sorted({row['year'] for row in rows})
    if len(years) < 2:
        raise ValueError('A distinct later conference-year is required for held-out evaluation')
    heldout_year = years[-1]
    calibration = [row for row in rows if row['year'] < heldout_year]
    heldout = [row for row in rows if row['year'] == heldout_year]
    if not all(any(bool(row['accepted']) == label for row in part)
               for part in (calibration, heldout) for label in (True, False)):
        raise ValueError('Both partitions require accepted and rejected decisions')
    return calibration, heldout


def review_request(paper: dict):
    from lib.longform.documents.review import review_messages
    from lib.longform.documents.rules import rules_for
    from lib.longform.documents.types import document_type
    doc_type = document_type('paper')
    checklist = tuple(rule for rule in rules_for('paper') if rule.kind == 'checklist')
    messages = review_messages(doc_type, venue='ICLR 2025', text=paper['text'],
                               checklist=checklist)
    return doc_type, checklist, messages


def request_provenance(paper: dict, model: str, messages: list, *,
                       endpoint: str | None = None, effective_route: str | None = None) -> dict:
    return {
        'selection_receipt': paper.get('selection_receipt'),
        'prompt_digest': current_prompt_digest(),
        'request_prompt_digest': hashlib.sha256(
            json.dumps(messages, sort_keys=True).encode()).hexdigest(),
        'paper_input_digest': hashlib.sha256(
            json.dumps(paper, sort_keys=True).encode()).hexdigest(),
        'requested_model': model,
        'endpoint': endpoint,
        'effective_route': effective_route,
    }


def _judge_completion(payload: dict, row: dict) -> str:
    """Validate the compatible choice before projecting it into a judge row."""
    choices = payload.get('choices')
    # A compatible request with no n parameter asks for exactly one choice.
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError('Expected exactly one judge completion choice')
    choice = choices[0]
    finish_reason = choice.get('finish_reason')
    row['finish_reason'] = finish_reason
    if finish_reason not in _NORMAL_FINISH_REASONS:
        raise ValueError(f'Incomplete judge completion: finish_reason={finish_reason!r}')
    message = choice.get('message')
    if not isinstance(message, dict):
        raise ValueError('Missing judge completion message')
    if message.get('tool_calls') not in (None, []) or message.get('function_call') is not None:
        raise ValueError('Judge completion contains tool or function calls')
    content = message.get('content')
    if not isinstance(content, str):
        raise ValueError('Judge completion is not text')
    usage = payload.get('usage') or {}
    row.update(completion=content, reported_model=payload.get('model'),
               completion_validation=_COMPLETION_VALIDATION,
               usage={key: usage.get(key) for key in ('prompt_tokens', 'completion_tokens')})
    return content


def judge(paper: dict, *, model: str, endpoint: str | None = None) -> dict:
    from evaluations.slides_bench.llm_bridge import request_completion
    from lib.longform.documents.review import parse_review
    from lib.model_info import route_output_limit
    doc_type, checklist, messages = review_request(paper)
    row = {key: paper[key] for key in ('id', 'title', 'decision', 'accepted')} | {
        **request_provenance(paper, model, messages, endpoint=endpoint),
        'reported_model': None, 'chars': len(paper['text']), 'overall': None,
        'scores': None, 'errors': [], 'usage': {},
        'request_messages': messages, 'completion': None, 'finish_reason': None}
    try:
        payload = request_completion(messages, model=model, max_tokens=route_output_limit(model),
                                     temperature=0, endpoint=endpoint)
        content = _judge_completion(payload, row)
        if row['reported_model'] != model:
            raise ValueError(f'Model identity mismatch: requested {model!r}, '
                             f'reported {row["reported_model"]!r}')
        if not _completed_selection_receipt(row['selection_receipt']):
            raise ValueError('A completed selection receipt is required before scoring')
        review, errors = parse_review(content, doc_type, checklist=checklist)
        row.update(overall=review.get('overall'), scores=review.get('scores'), errors=errors)
    except Exception as exc:
        row['errors'] = [f'{type(exc).__name__}: {exc}'[:500]]
    return row


def current_prompt_digest() -> str:
    from lib.longform.documents.review import prompt_digest
    from lib.longform.documents.rules import rules_for
    from lib.longform.documents.types import document_type
    checklist = tuple(rule for rule in rules_for('paper') if rule.kind == 'checklist')
    return prompt_digest(document_type('paper'), checklist)


def _is_scored_completion(row: dict, model: str) -> bool:
    return (row.get('completion_validation') == _COMPLETION_VALIDATION
            and row.get('reported_model') == model and not row.get('errors')
            and row.get('finish_reason') in _NORMAL_FINISH_REASONS
            and type(row.get('overall')) is int and 1 <= row['overall'] <= 10)


def reusable_rows(lines, digest: str, *, papers: list[dict], model: str,
                  endpoint: str | None = None, effective_route: str | None = None) -> dict:
    """Reuse requires a current effective route identity supplied by trusted transport."""
    if (not endpoint or not effective_route
            or any(not _completed_selection_receipt(paper.get('selection_receipt'))
                   for paper in papers)):
        return {}
    selected = {paper['id']: paper for paper in papers}
    expected = {paper['id']: request_provenance(
        paper, model, review_request(paper)[2], endpoint=endpoint, effective_route=effective_route)
        for paper in papers}
    done = {}
    for line in lines:
        row = json.loads(line)
        provenance = expected.get(row.get('id'))
        if (provenance and all(row.get(k) == v for k, v in provenance.items())
                and row.get('prompt_digest') == digest
                and _is_scored_completion(row, model)):
            done[row['id']] = row | {
                key: selected[row['id']][key] for key in ('id', 'title', 'decision', 'accepted')}
    return done


def require_classes(rows: list[dict]) -> None:
    if not all(any(bool(row['accepted']) == label for row in rows) for label in (True, False)):
        raise ValueError('Insufficient evidence: accepted and rejected decisions are required')


def auc(rows: list[dict]) -> float:
    require_classes(rows)
    pos = [r['overall'] for r in rows if r['accepted']]
    neg = [r['overall'] for r in rows if not r['accepted']]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def best_threshold(rows: list[dict]) -> tuple[int, float]:
    require_classes(rows)
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
    require_classes(rows)
    predicted = [(r['overall'] >= threshold, bool(r['accepted'])) for r in rows]
    accuracy = sum(p == a for p, a in predicted) / len(predicted)
    tpr = sum(p and a for p, a in predicted) / max(1, sum(a for _p, a in predicted))
    tnr = sum((not p) and (not a) for p, a in predicted) / \
        max(1, sum(not a for _p, a in predicted))
    return {'accuracy': accuracy, 'balanced_accuracy': (tpr + tnr) / 2,
            'true_positive_rate': tpr, 'true_negative_rate': tnr}


def bootstrap_auc(rows: list[dict]) -> list[float]:
    require_classes(rows)
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
    if args.write_calibration:
        raise ValueError('Historical ICLR 2025 mirror has no time-separated or license-verified '
                         'holdout; cannot publish this calibration as a general prediction')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / f'rows-{args.judge_model}.jsonl'
    digest = current_prompt_digest()
    papers = select_papers(out, args.per_class)
    selection_receipt = papers[0].get('selection_receipt') if papers else None
    if (not _completed_selection_receipt(selection_receipt)
            or any(paper.get('selection_receipt') != selection_receipt for paper in papers)):
        raise ValueError('A single completed selection receipt is required for scoring')
    # The bridge reports the model, but no trusted effective-route revision.
    done = reusable_rows(rows_path.read_text().splitlines() if rows_path.is_file() else [],
                         digest, papers=papers, model=args.judge_model, endpoint=args.endpoint)
    todo = [paper for paper in papers if paper['id'] not in done]
    print(f'{len(papers)} papers selected, {len(todo)} to judge', flush=True)

    def work(paper):
        try:
            return judge(paper, model=args.judge_model, endpoint=args.endpoint)
        except Exception as exc:
            return {key: paper[key] for key in ('id', 'title', 'decision', 'accepted')} | {
                'selection_receipt': paper.get('selection_receipt'),
                'overall': None, 'errors': [f'{type(exc).__name__}: {exc}'[:500]]}
    with ThreadPoolExecutor(args.in_flight) as pool, rows_path.open('a') as sink:
        for row in pool.map(work, todo):
            sink.write(json.dumps(row) + '\n')
            sink.flush()
            print(row['id'], row['accepted'], row.get('overall'), row.get('errors') or '',
                  flush=True)
            if (row.get('selection_receipt') == selection_receipt
                    and _is_scored_completion(row, args.judge_model)):
                done[row['id']] = row
    rows = [done[p['id']] | {key: p[key] for key in ('id', 'title', 'decision', 'accepted')}
            for p in papers if p['id'] in done]
    calibration = [r for r in rows if int(_key(r['id'])[-1], 16) % 2 == 0]
    heldout = [r for r in rows if int(_key(r['id'])[-1], 16) % 2 == 1]
    sufficient = all(any(bool(row['accepted']) == label for row in part)
                     for part in (calibration, heldout) for label in (True, False))
    threshold, youden = best_threshold(calibration) if sufficient else (None, None)
    summary = {
        'dataset': f'ICLR 2025 public decisions via HF parquet {_PAPERS} + {_PARAGRAPHS}',
        'judge_model': args.judge_model,
        'endpoint': args.endpoint, 'effective_route': None, 'cache_reuse_eligible': False,
        'status': 'diagnostic' if sufficient else 'insufficient_evidence',
        'reason': None if sufficient else 'Both calibration and heldout require accepted and rejected decisions',
        'prompt_digest': digest,
        'selection_receipt': selection_receipt,
        'per_class_requested': args.per_class, 'judged': len(rows),
        'decision_counts': {decision: sum(row['decision'] == decision for row in rows)
                            for decision in sorted({row['decision'] for row in rows})},
        'oral_auc': None, 'oral_calibration': 'unavailable: one oral in the historical selected sample',
        'heldout_partition': 'same_conference_year_id_hash',
        'license_verified': False, 'training_contamination': 'unknown',
        'sample_bias': 'balanced text-available accept/reject subset, not venue prevalence',
        'probability_calibration': None,
        'failed': len(papers) - len(rows),
        'n_calibration': len(calibration), 'n_heldout': len(heldout),
        'heldout_accepted': sum(r['accepted'] for r in heldout),
        'threshold': threshold, 'calibration_youden_j': youden,
        'calibration_auc': auc(calibration) if sufficient else None,
        'heldout_auc': auc(heldout) if sufficient else None,
        'heldout_auc_ci95': bootstrap_auc(heldout) if sufficient else None,
        **{f'heldout_{k}': v for k, v in (
            at_threshold(heldout, threshold) if sufficient else
            dict.fromkeys(('accuracy', 'balanced_accuracy', 'true_positive_rate',
                           'true_negative_rate'))).items()},
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    (out / f'summary-{args.judge_model}.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
