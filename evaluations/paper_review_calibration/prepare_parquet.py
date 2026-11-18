"""Select calibration papers and their body text from the parquet mirrors.

Runs in an isolated environment with pyarrow (evaluation-only dependency):
  uv run --no-project --with pyarrow python prepare_parquet.py \
      PAPERS.parquet OUT.jsonl PER_CLASS PARAGRAPHS.parquet [...]

Standard library + pyarrow only. Selection is deterministic: within each
class, papers sorted by SHA-256 of their OpenReview id. Papers without
paragraph text are skipped in that order, so each class still gets PER_CLASS.
"""
import hashlib
import json
import sys

import pyarrow.compute as pc
import pyarrow.parquet as pq

VENUE = 'ICLR.cc/2025/Conference'
ACCEPT = ('ICLR 2025 Oral', 'ICLR 2025 Spotlight', 'ICLR 2025 Poster')
REJECT = ('Rejected_Submission',)


def key(paper_id):
    return hashlib.sha256(paper_id.encode()).hexdigest()


def main(papers_path, out_path, per_class, *paragraph_paths):
    per_class = int(per_class)
    papers = pq.read_table(papers_path, columns=[
        'venue', 'paper_openreview_id', 'title', 'paper_decision']).to_pylist()
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
    # The paragraph mirror covers only part of the venue: select among the
    # papers that have text, then read those papers' paragraphs.
    with_text = set()
    for path in paragraph_paths:
        table = pq.read_table(path, columns=['venue', 'paper_openreview_id'])
        table = table.filter(pc.equal(table['venue'], VENUE))
        with_text |= set(table['paper_openreview_id'].to_pylist())
    for label in pools:
        pools[label] = [row for row in pools[label]
                        if row['paper_openreview_id'] in with_text]
    wanted = {row['paper_openreview_id'] for label in pools
              for row in pools[label][:per_class * 2]}
    text = {}
    for path in paragraph_paths:
        table = pq.read_table(path, columns=[
            'paper_openreview_id', 'paragraph_idx', 'section', 'content'])
        table = table.filter(pc.is_in(table['paper_openreview_id'],
                                      value_set=__import__('pyarrow').array(sorted(wanted))))
        for row in table.to_pylist():
            text.setdefault(row['paper_openreview_id'], []).append(row)
    with open(out_path, 'w') as sink:
        for label in (1, 0):
            kept = 0
            for row in pools[label]:
                if kept == per_class:
                    break
                rows = sorted(text.get(row['paper_openreview_id']) or [],
                              key=lambda r: int(r['paragraph_idx']))
                body = []
                for paragraph in rows:
                    section = str(paragraph.get('section') or '')
                    if section.strip().casefold() in ('references', 'bibliography'):
                        break
                    body.append(f'[{section}] {paragraph["content"]}')
                if len(body) < 10:
                    continue
                kept += 1
                sink.write(json.dumps({
                    'id': row['paper_openreview_id'], 'title': row['title'],
                    'decision': row['paper_decision'], 'accepted': label,
                    'text': '\n'.join(body)}) + '\n')
            print(f'class {label}: {kept} papers', file=sys.stderr)


if __name__ == '__main__':
    main(*sys.argv[1:])
