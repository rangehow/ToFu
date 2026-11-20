"""Select calibration papers and their body text from the parquet mirrors.

Runs in an isolated environment with pyarrow (evaluation-only dependency):
  uv run --no-project --with pyarrow python prepare_parquet.py \
      PAPERS.parquet OUT.jsonl PER_CLASS PARAGRAPHS.parquet [...]

Standard library + pyarrow only. Selection is deterministic: within each
class, papers are sorted by SHA-256 of their OpenReview id. Papers with
fewer than ten nonblank body paragraphs are skipped until each class
reaches PER_CLASS or exhausts its candidates.
"""
import hashlib
import json
import sys
from itertools import islice

VENUE = 'ICLR.cc/2025/Conference'
ACCEPT = ('ICLR 2025 Oral', 'ICLR 2025 Spotlight', 'ICLR 2025 Poster')
REJECT = ('Rejected_Submission',)
_PARAGRAPH_COLUMNS = ['paper_openreview_id', 'paragraph_idx', 'section']
_MIN_BODY_PARAGRAPHS = 10


def key(paper_id):
    return hashlib.sha256(paper_id.encode()).hexdigest()


def _read_rows(path, columns, *, filters=None):
    import pyarrow.parquet as pq

    return pq.read_table(path, columns=columns, filters=filters).to_pylist()


def _paragraphs(paths, wanted, *, with_content=False):
    grouped = {}
    if wanted:
        columns = _PARAGRAPH_COLUMNS + (['content'] if with_content else [])
        for path in paths:
            rows = _read_rows(path, columns, filters=[
                ('venue', '=', VENUE), ('paper_openreview_id', 'in', wanted)])
            for row in rows:
                grouped.setdefault(row.pop('paper_openreview_id'), []).append(row)
    return grouped


def _body_paragraphs(rows):
    for row in sorted(rows, key=lambda row: int(row['paragraph_idx'])):
        section = str(row.get('section') or '')
        if section.strip().casefold() in ('references', 'bibliography'):
            break
        yield section, row


def _candidates(pool, metadata):
    for row in pool:
        body = _body_paragraphs(metadata.get(row['paper_openreview_id'], ()))
        if sum(1 for _ in body) >= _MIN_BODY_PARAGRAPHS:
            yield row


def _usable_body(rows):
    body = []
    for section, paragraph in _body_paragraphs(rows):
        content = paragraph.get('content')
        if isinstance(content, str) and content.strip():
            body.append(f'[{section}] {content}')
    return body


def _select(pools, paragraph_paths, per_class):
    wanted = sorted({row['paper_openreview_id'] for pool in pools.values() for row in pool})
    metadata = _paragraphs(paragraph_paths, wanted)
    candidates = {label: _candidates(pool, metadata) for label, pool in pools.items()}
    selected = {label: [] for label in pools}
    # Metadata can rule papers out but cannot establish that their text is usable.
    # Read ahead only as far as the remaining quota, then backfill any shortfall.
    while batch := [(label, row) for label, pool in candidates.items()
                    for row in islice(pool, per_class - len(selected[label]))]:
        text = _paragraphs(paragraph_paths, sorted({row['paper_openreview_id'] for _, row in batch}),
                           with_content=True)
        for label, row in batch:
            body = _usable_body(text.get(row['paper_openreview_id'], ()))
            if len(body) >= _MIN_BODY_PARAGRAPHS:
                selected[label].append((row, body))
    for label, papers in selected.items():
        print(f'class {label}: {len(papers)} papers', file=sys.stderr)
        for row, body in papers:
            yield label, row, body


def main(papers_path, out_path, per_class, *paragraph_paths):
    per_class = int(per_class)
    papers = _read_rows(papers_path, ['venue', 'paper_openreview_id', 'title', 'paper_decision'])
    pools = {1: [], 0: []}
    for row in papers:
        if row['venue'] != VENUE:
            continue
        if row['paper_decision'] in ACCEPT:
            pools[1].append(row)
        elif row['paper_decision'] in REJECT:
            pools[0].append(row)
    for label in pools:
        pools[label].sort(key=lambda row: key(row['paper_openreview_id']))
    selected = list(_select(pools, paragraph_paths, per_class))
    with open(out_path, 'w') as sink:
        for label, row, body in selected:
            sink.write(json.dumps({
                'id': row['paper_openreview_id'], 'title': row['title'],
                'decision': row['paper_decision'], 'accepted': label,
                'text': '\n'.join(body)}) + '\n')


if __name__ == '__main__':
    main(*sys.argv[1:])
