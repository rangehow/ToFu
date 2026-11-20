"""Candidate control pools from the OpenReview parquet mirror.

Runs in an isolated environment with pyarrow (evaluation-only dependency):
  uv run --no-project --with pyarrow python openreview_pools.py PAPERS.parquet OUT.jsonl

Reads the parquet that ``evaluations/paper_review_calibration/run.py``
already downloads (``ulab-ai/ResearchArcade-openreview-papers``). For each
venue-year from 2020 on, it writes every ordinary accepted paper (poster /
plain accept; orals, spotlights and notable-top-x% are excluded so the pool
is "accepted but ordinary") and every rejected paper, in SHA-256 order of
the OpenReview id. Callers take prefixes of each pool, so selection is
deterministic. Standard library + pyarrow only.
"""
import hashlib
import json
import re
import sys

import pyarrow.parquet as pq

ORDINARY = re.compile(r'poster|^accept$|ICLR 2023 poster|EMNLP 2023 Main', re.I)
REJECTED = re.compile(r'reject', re.I)


def main(papers_path, out_path):
    rows = pq.read_table(papers_path, columns=[
        'venue', 'paper_openreview_id', 'title', 'abstract',
        'paper_decision']).to_pylist()
    out = []
    for row in rows:
        match = re.match(r'([A-Za-z]+)(?:\.cc)?/(\d{4})/', row['venue'] or '')
        if not match or int(match.group(2)) < 2020:
            continue
        decision = row['paper_decision'] or ''
        if 'desk' in decision.lower():
            continue
        if REJECTED.search(decision):
            group = 'control_rejected'
        elif ORDINARY.search(decision):
            group = 'control_accepted'
        else:
            continue
        out.append({'venue': match.group(1).replace('NeurIPS', 'NeurIPS'),
                    'year': int(match.group(2)), 'group': group,
                    'openreview_id': row['paper_openreview_id'],
                    'title': row['title'], 'decision': decision,
                    'abstract_or': row['abstract'],
                    'key': hashlib.sha256(row['paper_openreview_id'].encode()).hexdigest()})
    out.sort(key=lambda r: (r['venue'], r['year'], r['group'], r['key']))
    with open(out_path, 'w') as sink:
        for row in out:
            sink.write(json.dumps(row) + '\n')
    print(f'{len(out)} pool rows', file=sys.stderr)


if __name__ == '__main__':
    main(*sys.argv[1:])
