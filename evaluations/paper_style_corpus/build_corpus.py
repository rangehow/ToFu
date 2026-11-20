"""Build the paper-style corpus: award + high-citation papers and matched controls.

Usage (from the Tofu checkout; network; resumable, every read is cached):
  PYTHONPATH=$PWD .venv/bin/python3 -m evaluations.paper_style_corpus.build_corpus \
      [--root DATA_DIR] [--download]

Inputs:
  * ``awards_sources.json`` (committed): award papers with the official
    announcement URL, compiled from conference award pages.
  * OpenAlex, queried 2020-2024 works with an arXiv location in subfields
    1702 (Artificial Intelligence) and 1707 (Computer Vision), most cited
    first; surveys, reviews and technical/model reports are excluded because
    the pipeline writes research papers.
  * Control pools: ``pools/openreview.jsonl`` (``openreview_pools.py``) for
    ICLR 2020-2025 / NeurIPS 2021-2025 accepted-ordinary and rejected
    papers; ACL Anthology bibliography for ACL/EMNLP/NAACL; CVF open access
    and PMLR indexes for CVPR/ICCV/ICML control *titles*.

Matching: each positive (award or high-citation paper) gets one accepted
ordinary control from the same venue-year (nearest available year when the
pool does not cover it) and, when an OpenReview pool exists for that venue
family, one rejected control from the same year. Candidates are taken in
SHA-256 order and skipped when no open full text is found. Positive and
controls share a ``pair`` id; the train/test split is by pair hash (70/30),
so no pair straddles the split.

Full text comes only from arXiv (``arxiv.org/pdf``) or the ACL Anthology.
PDFs are stored under ``<root>/pdf/`` (gitignored ``data/``); only
``manifest.csv`` (metadata, licence, source URL) is committed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluations.paper_style_corpus import sources  # noqa: E402

HERE = Path(__file__).resolve().parent


def _shared_checkout() -> Path:
    """The shared checkout (task worktrees share its gitignored ``data/``)."""
    common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            cwd=HERE, capture_output=True, text=True, check=True).stdout.strip()
    return Path(common).parent


# Private corpus store: PDFs, extracted records, annotations (never committed).
DEFAULT_ROOT = _shared_checkout() / 'data' / 'paper-style-corpus'
MATCH_MIN = 0.92            # title similarity to accept an arXiv hit
HIGHCITE_N = 100
TEST_SHARE_HEX = 'b'        # pair hash first hex digit >= 'b' -> test (5/16 ~ 31%)
CITATION_DATE = time.strftime('%Y-%m-%d')
_EXCLUDE_HIGHCITE = re.compile(
    r'survey|review|overview|technical report|tech report|\breport\b|herd of '
    r'models|model card|opportunities and risks|open problems|tutorial|primer|'
    r'challenges and|a comprehensive|position paper', re.I)
ACL_FAMILY = ('ACL', 'EMNLP', 'NAACL', 'EACL', 'AACL')
ACL_VOLUMES = {'ACL': ('acl-main', 'acl-long'), 'EMNLP': ('emnlp-main',),
               'NAACL': ('naacl-main', 'naacl-long')}
CVF = ('CVPR', 'ICCV', 'ECCV')
PMLR = {2020: 'v119', 2021: 'v139', 2022: 'v162', 2023: 'v202',
        2024: 'v235', 2025: 'v267'}


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_venue(venue: str) -> str:
    v = venue.upper().replace('NEURIPS', 'NeurIPS')
    for name in ('NeurIPS', 'ICML', 'ICLR', 'ACL', 'EMNLP', 'NAACL', 'CVPR',
                 'ICCV', 'ECCV', 'COLM'):
        if name.upper() in v.upper():
            return name
    return venue


# ── positives ────────────────────────────────────────────────────────────

def load_awards() -> list[dict]:
    rows = json.loads((HERE / 'awards_sources.json').read_text())['papers']
    out, seen = [], set()
    for row in rows:
        venue = canonical_venue(row['venue'])
        key = sources.norm_title(row['title'])
        if key in seen:
            continue
        seen.add(key)
        award = row['award']
        year = int(row.get('paper_year') or row['award_year'])
        stratum = 'test_of_time' if 'test_of_time' in award else (
            'position' if 'position' in award else (
                'datasets_benchmarks' if award.endswith('_db') else 'main'))
        out.append({'group': 'award', 'title': row['title'].replace('&amp;', '&'),
                    'venue': venue, 'year': year,
                    'award': f"{venue} {row['award_year']} {award}",
                    'award_source_url': row['source_url'], 'stratum': stratum,
                    'arxiv_id': (row.get('arxiv_id') or '').replace('acl:', '')
                    if not (row.get('arxiv_id') or '').startswith('acl:') else '',
                    'acl_id': row['arxiv_id'][4:] if (row.get('arxiv_id') or '').startswith('acl:') else ''})
    return out


def load_highcite(cache, awards) -> list[dict]:
    works = (sources.openalex_top(cache, subfield=1702, years='2020-2024', pages=3)
             + sources.openalex_top(cache, subfield=1707, years='2020-2024', pages=1))
    works.sort(key=lambda w: -w['cited_by_count'])
    award_titles = {sources.norm_title(a['title']): a for a in awards}
    out, seen = [], set()
    for work in works:
        title = work.get('title') or ''
        arxiv_id = sources.arxiv_id_from_locations(work.get('locations'))
        key = sources.norm_title(title)
        if not arxiv_id or key in seen or _EXCLUDE_HIGHCITE.search(title):
            continue
        seen.add(key)
        if key in award_titles:   # an award paper that is also highly cited
            award_titles[key]['citations'] = work['cited_by_count']
            award_titles[key]['citation_source'] = f"OpenAlex {work['id']} {CITATION_DATE}"
            award_titles[key]['also_highcite'] = True
            continue
        out.append({'group': 'highcite', 'title': title, 'venue': 'arXiv',
                    'year': int(work['publication_year']), 'award': '',
                    'award_source_url': '', 'stratum': 'main',
                    'citations': work['cited_by_count'],
                    'citation_source': f"OpenAlex {work['id']} {CITATION_DATE}",
                    'arxiv_id': arxiv_id, 'acl_id': '',
                    'license': ''})
        if len(out) == HIGHCITE_N:
            break
    return out


# ── control pools ────────────────────────────────────────────────────────

class Pools:
    def __init__(self, root: Path, cache):
        self.cache = cache
        self.openreview = [json.loads(line) for line in
                           (root / 'pools' / 'openreview.jsonl').read_text().splitlines()]
        self.anthology = None
        self.used: set[str] = set()

    def openreview_pool(self, venue: str, year: int, group: str) -> list[dict]:
        years = sorted({r['year'] for r in self.openreview if r['venue'] == venue
                        and r['group'] == group})
        if not years:
            return []
        year = min(years, key=lambda y: (abs(y - year), -y))
        return [r for r in self.openreview if r['venue'] == venue
                and r['year'] == year and r['group'] == group]

    def acl_pool(self, venue: str, year: int) -> list[dict]:
        if self.anthology is None:
            self.anthology = sources.anthology_index(self.cache)
        for delta in (0, -1, 1, -2):
            prefixes = tuple(f'{year + delta}.{v.lower()}.' for v in
                             ACL_VOLUMES.get(venue, ('acl-long',)))
            pool = [{'title': e['title'], 'acl_id': e['acl_id'],
                     'venue': venue, 'year': year + delta}
                    for e in self.anthology if e['acl_id'].startswith(prefixes)
                    and not e['acl_id'].endswith('.0')]
            if pool:
                return sorted(pool, key=lambda r: h(r['acl_id']))
        return []

    def title_pool(self, venue: str, year: int) -> list[dict]:
        titles: list[str] = []
        if venue in CVF:
            for delta in (0, -1, 1, -2):
                for conf in (f'{venue}{year + delta}', f'CVPR{year + delta}'):
                    try:
                        titles = sources.cvf_titles(self.cache, conf)
                    except RuntimeError:
                        titles = []
                    if titles:
                        year = year + delta
                        break
                if titles:
                    break
        elif venue == 'ICML':
            near = min(PMLR, key=lambda y: (abs(y - year), -y))
            titles, year = sources.pmlr_titles(self.cache, PMLR[near]), near
        return sorted(({'title': t, 'venue': venue, 'year': year} for t in titles),
                      key=lambda r: h(r['title']))


def control_plan(positive: dict) -> list[tuple[str, str, int, str]]:
    """(group, pool kind, year, venue) wanted for one positive."""
    venue, year = positive['venue'], positive['year']
    plans = []
    if venue in ('ICLR', 'NeurIPS'):
        plans.append(('control_accepted', 'openreview', year, venue))
    elif venue in ACL_FAMILY:
        plans.append(('control_accepted', 'acl', year, venue))
    elif venue in CVF or venue == 'ICML':
        plans.append(('control_accepted', 'titles', year, venue))
    else:   # COLM, arXiv (high-citation): same-year ML conference control
        alt = 'ICLR' if year <= 2020 or year % 2 else 'NeurIPS'
        plans.append(('control_accepted', 'openreview', year, alt))
    if venue not in ACL_FAMILY and venue not in CVF:
        alt = venue if venue in ('ICLR', 'NeurIPS') else 'ICLR'
        plans.append(('control_rejected', 'openreview', year, alt))
    return plans


# ── resolution ───────────────────────────────────────────────────────────

def resolve_text_source(cache, row: dict) -> bool:
    """Fill arxiv_id/acl_id and source_url; False when no open full text."""
    if row.get('acl_id'):
        row['source_url'] = f"https://aclanthology.org/{row['acl_id']}.pdf"
        row['license'] = 'CC BY 4.0 (ACL Anthology)'
        return True
    if not row.get('arxiv_id'):
        if row['venue'] in ACL_FAMILY:
            anthology = sources.anthology_index(cache)
            key = sources.norm_title(row['title'])
            hits = [e for e in anthology if sources.norm_title(e['title']) == key
                    and e['acl_id'][:4].isdigit() and int(e['acl_id'][:4]) >= row['year'] - 1]
            if hits:
                row['acl_id'] = hits[0]['acl_id']
                return resolve_text_source(cache, row)
        hit = sources.arxiv_search_title(cache, row['title'])
        if row['group'] == 'control_rejected' and hit and hit['score'] >= MATCH_MIN:
            row['note'] = ('arXiv version of a rejected submission; it may be a '
                           'later revision')
        if not hit or hit['score'] < MATCH_MIN:
            row['resolve_note'] = f'no arXiv match ({hit and hit["score"]})'
            return False
        row['arxiv_id'] = hit['arxiv_id']
        row['arxiv_match'] = hit['score']
        row['license'] = hit['license'] or 'unknown'
    row['source_url'] = f"https://arxiv.org/pdf/{row['arxiv_id']}"
    if not row.get('license'):
        row['license'] = sources.arxiv_license(cache, row['arxiv_id'])
    return True


def pool_for(pools: Pools, plan) -> list[dict]:
    group, kind, year, venue = plan
    if kind == 'openreview':
        return pools.openreview_pool(venue, year, group)
    if kind == 'acl':
        return pools.acl_pool(venue, year)
    return pools.title_pool(venue, year)


def pick_control(cache, pools: Pools, positive: dict, plan, pool: list[dict]) -> dict | None:
    group = plan[0]
    for candidate in pool:
        key = sources.norm_title(candidate['title'])
        if key in pools.used:
            continue
        row = {'group': group, 'title': candidate['title'],
               'venue': candidate['venue'], 'year': candidate['year'],
               'award': '', 'award_source_url': '', 'stratum': positive['stratum'],
               'openreview_id': candidate.get('openreview_id', ''),
               'decision': candidate.get('decision', ''),
               'arxiv_id': '', 'acl_id': candidate.get('acl_id', ''),
               'matched_to': positive['id'], 'pair': positive['pair']}
        if resolve_text_source(cache, row):
            return row
    return None


MANIFEST_FIELDS = ('id', 'group', 'stratum', 'split', 'pair', 'matched_to', 'title',
                   'venue', 'year', 'award', 'award_source_url', 'citations',
                   'citation_source', 'decision', 'openreview_id', 'arxiv_id',
                   'acl_id', 'source_url', 'license', 'pdf_sha256', 'status')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default=str(DEFAULT_ROOT))
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--workers', type=int, default=6)
    args = parser.parse_args(argv)
    root = Path(args.root)
    cache = sources.Cache(root / 'meta')
    corpus_path = root / 'corpus.json'
    if corpus_path.is_file():
        corpus = json.loads(corpus_path.read_text())
    else:
        awards = load_awards()
        highcite = load_highcite(cache, awards)
        positives = awards + highcite
        for index, row in enumerate(positives):
            prefix = 'A' if row['group'] == 'award' else 'H'
            row['id'] = f'{prefix}{h(row["title"])[:8]}'
            row['pair'] = row['id']
        pools = Pools(root, cache)
        for row in positives:
            pools.used.add(sources.norm_title(row['title']))

        def work_positive(row):
            try:
                if not resolve_text_source(cache, row):
                    row['status'] = 'no_open_text'
            except Exception as exc:
                row['status'] = f'error: {exc}'[:300]
            return row
        with ThreadPoolExecutor(args.workers) as pool:
            positives = list(pool.map(work_positive, positives))
        print(f'positives: {len(positives)}, with text: '
              f'{sum(1 for r in positives if not r.get("status"))}', flush=True)
        # Positives drawing from the same pool get disjoint, deterministic
        # slices of it (the i-th of m takes pool[i::m]), so control picking
        # runs in parallel without a shared "used" race.
        jobs, by_pool = [], {}
        for row in positives:
            if row.get('status') or row['stratum'] == 'test_of_time':
                continue
            for plan in control_plan(row):
                pool = pool_for(pools, plan)
                key = (plan[0], id(pool) if not pool else
                       (pool[0].get('openreview_id') or pool[0].get('acl_id') or pool[0]['title']))
                by_pool.setdefault(key, []).append((row, plan, pool))
        for members in by_pool.values():
            m = len(members)
            for i, (row, plan, pool) in enumerate(members):
                jobs.append((row, plan, pool[i::m]))

        def work_control(job):
            row, plan, pool = job
            try:
                return pick_control(cache, pools, row, plan, pool)
            except Exception as exc:
                print('control error', row['id'], plan, exc, flush=True)
                return None
        controls = []
        with ThreadPoolExecutor(args.workers) as pool:
            for control in pool.map(work_control, jobs):
                if control:
                    control['id'] = 'C' + h(control['title'])[:8]
                    controls.append(control)
        print(f'controls: {len(controls)} of {len(jobs)} planned', flush=True)
        corpus = positives + controls
        for row in corpus:
            row['split'] = 'test' if h(row['pair'])[0] >= TEST_SHARE_HEX else 'train'
        corpus_path.write_text(json.dumps(corpus, indent=1))
    if args.download:
        pdf_dir = root / 'pdf'
        pdf_dir.mkdir(exist_ok=True)

        def fetch(row):
            if row.get('status') or not row.get('source_url'):
                return row
            target = pdf_dir / f"{row['id']}.pdf"
            try:
                sources.download(row['source_url'], target,
                                 arxiv='arxiv.org' in row['source_url'])
                row['pdf_sha256'] = h_file(target)
            except Exception as exc:
                row['status'] = f'download failed: {exc}'[:300]
            return row
        with ThreadPoolExecutor(args.workers) as pool:
            corpus = list(pool.map(fetch, corpus))
        corpus_path.write_text(json.dumps(corpus, indent=1))
    with (HERE / 'manifest.csv').open('w', newline='') as sink:
        writer = csv.DictWriter(sink, fieldnames=MANIFEST_FIELDS, extrasaction='ignore')
        writer.writeheader()
        for row in sorted(corpus, key=lambda r: (r['group'], r['id'])):
            writer.writerow({k: row.get(k, '') for k in MANIFEST_FIELDS}
                            | {'status': row.get('status') or 'ok'})
    counts = {}
    for row in corpus:
        if not row.get('status'):
            counts[row['group']] = counts.get(row['group'], 0) + 1
    print(json.dumps(counts), flush=True)
    return 0


def h_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == '__main__':
    sys.exit(main())
