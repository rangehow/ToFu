"""Open-source metadata and full-text access for the paper-style corpus.

Responsibility: every network read the corpus build makes — OpenAlex (high
citation selection), DataCite (arXiv identifier and licence by title), arXiv
(PDF), the ACL
Anthology bibliography dump and PDFs, and official proceedings indexes
(CVF open access, PMLR) used only for control *titles*. All requests go
through ``lib.http_transport.client`` (shared network authority) and are
cached on disk under the private corpus root, so a rerun makes no request
for data it already holds.

arXiv asks automated clients for one request every three seconds; that
external limit is ``ARXIV_INTERVAL_S`` (applied to PDF downloads).
"""
from __future__ import annotations

import difflib
import gzip
import hashlib
import html
import json
import re
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

from lib.http_transport.client import http_get

ARXIV_INTERVAL_S = 3.0
# arXiv's API answers 503 under load and asks clients to retry later.
TRANSIENT_ATTEMPTS = 4
_arxiv_lock = threading.Lock()
_api_lock = threading.Lock()
_arxiv_last = [0.0]


def norm_title(title: str) -> str:
    text = unicodedata.normalize('NFKD', html.unescape(title or '')).lower()
    text = re.sub(r"[{}$\\'’]", '', text)
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def title_match(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


class Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, kind: str, key: str, suffix: str = '.json') -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        folder = self.root / kind
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (digest + suffix)

    def text(self, kind: str, url: str, *, arxiv: bool = False,
             timeout: float = 120) -> str:
        target = self.path(kind, url, '.txt')
        if target.is_file():
            return target.read_text(encoding='utf-8')
        last = None
        for attempt in range(TRANSIENT_ATTEMPTS):
            try:
                if arxiv:
                    # One API request in flight at a time: concurrent queries
                    # get empty feeds back from the arXiv API.
                    with _api_lock:
                        _arxiv_wait()
                        response = http_get(url, timeout=timeout)
                else:
                    response = http_get(url, timeout=timeout)
            except Exception as exc:  # transport timeout / reset
                last = exc
            else:
                if response.status_code == 200:
                    target.write_text(response.text, encoding='utf-8')
                    return response.text
                last = RuntimeError(f'{url}: HTTP {response.status_code}')
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(10 * (attempt + 1))
        raise RuntimeError(f'{url}: {last}')


def _arxiv_wait():
    with _arxiv_lock:
        delay = _arxiv_last[0] + ARXIV_INTERVAL_S - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _arxiv_last[0] = time.monotonic()


# ── OpenAlex: highly cited works ─────────────────────────────────────────

def openalex_top(cache: Cache, *, subfield: int, years: str, pages: int) -> list[dict]:
    works = []
    for page in range(1, pages + 1):
        url = ('https://api.openalex.org/works?filter=publication_year:'
               f'{years},primary_topic.subfield.id:{subfield},'
               'locations.source.id:S4306400194&sort=cited_by_count:desc'
               f'&per_page=100&page={page}&select=id,title,publication_year,'
               'cited_by_count,locations,best_oa_location,type,primary_topic')
        works.extend(json.loads(cache.text('openalex', url))['results'])
    return works


def arxiv_id_from_locations(locations: list[dict]) -> str:
    for location in locations or []:
        url = location.get('landing_page_url') or ''
        match = re.search(r'arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})', url)
        if match:
            return match.group(1)
    return ''


# ── arXiv (identifiers and licences via DataCite) ──────────────────────
# arXiv registers a DataCite DOI (10.48550/arXiv.<id>) with title and licence
# for every paper. The arXiv API itself rate-limits this host's shared proxy
# (HTTP 429, empty feeds), so resolution uses the DataCite REST API.

DATACITE = 'https://api.datacite.org/dois'


def _datacite_rows(payload: dict) -> list[dict]:
    rows = []
    for item in payload.get('data') or []:
        attrs = item.get('attributes') or {}
        ident = item['id'].lower().split('arxiv.', 1)[-1]
        titles = attrs.get('titles') or [{}]
        rights = [r.get('rightsUri') or r.get('rights') for r in attrs.get('rightsList') or []]
        rows.append({'arxiv_id': ident, 'arxiv_title': titles[0].get('title', ''),
                     'license': next((r for r in rights if r), ''),
                     'year': attrs.get('publicationYear')})
    return rows


def arxiv_search_title(cache: Cache, title: str) -> dict | None:
    phrase = norm_title(title)
    if not phrase:
        return None
    url = (f'{DATACITE}?client-id=arxiv.content&page%5Bsize%5D=10&query='
           + urllib.parse.quote(f'titles.title:"{phrase}"'))
    rows = _datacite_rows(json.loads(cache.text('datacite', url)))
    best = None
    for row in rows:
        score = title_match(title, row['arxiv_title'])
        # Prefer the earliest-registered exact match (later "v2" DOIs do not exist; duplicates are rare).
        if best is None or score > best['score']:
            best = row | {'score': round(score, 3)}
    return best


def arxiv_license(cache: Cache, arxiv_id: str) -> str:
    url = f'{DATACITE}/10.48550/arxiv.{arxiv_id}'
    payload = json.loads(cache.text('datacite', url))
    rows = _datacite_rows({'data': [payload['data']]})
    return rows[0]['license'] or 'unknown'


def download(url: str, target: Path, *, arxiv: bool = False) -> Path:
    if target.is_file() and target.stat().st_size > 10_000:
        return target
    status = None
    for attempt in range(TRANSIENT_ATTEMPTS):
        if arxiv:
            _arxiv_wait()
        try:
            response = http_get(url, timeout=300)
        except Exception as exc:
            status = repr(exc)[:200]
        else:
            body = response.content
            status = response.status_code
            if status == 200 and body.startswith(b'%PDF'):
                break
            if status not in (429, 500, 502, 503, 504):
                raise RuntimeError(f'{url}: HTTP {status}, not a PDF')
        time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f'{url}: {status}')
    partial = target.with_suffix('.part')
    partial.write_bytes(body)
    partial.replace(target)
    return target


# ── ACL Anthology ────────────────────────────────────────────────────────

ANTHOLOGY_BIB = 'https://aclanthology.org/anthology.bib.gz'


def anthology_index(cache: Cache) -> list[dict]:
    target = cache.path('anthology', ANTHOLOGY_BIB, '.json')
    if target.is_file():
        return json.loads(target.read_text())
    raw = cache.root / 'anthology' / 'anthology.bib.gz'
    if not raw.is_file():
        response = http_get(ANTHOLOGY_BIB, timeout=600)
        if response.status_code != 200:
            raise RuntimeError(f'anthology bib: HTTP {response.status_code}')
        raw.write_bytes(response.content)
    text = gzip.decompress(raw.read_bytes()).decode('utf-8', 'replace')
    entries = []
    for block in text.split('\n@')[1:]:
        if not block.startswith('inproceedings'):
            continue
        url = re.search(r'url = "https://aclanthology\.org/([^"]+)/"', block)
        title = re.search(r'title = "(.+?)",\n', block, re.S)
        if url and title:
            entries.append({'acl_id': url.group(1),
                            'title': re.sub(r'[{}]', '', ' '.join(title.group(1).split()))})
    target.write_text(json.dumps(entries))
    return entries


# ── Proceedings indexes (control titles only) ────────────────────────────

def cvf_titles(cache: Cache, conference: str) -> list[str]:
    url = f'https://openaccess.thecvf.com/{conference}?day=all'
    page = cache.text('cvf', url, timeout=300)
    return [html.unescape(t) for t in
            re.findall(r'<dt class="ptitle"><br><a href="[^"]+">([^<]+)</a>', page)]


def pmlr_titles(cache: Cache, volume: str) -> list[str]:
    page = cache.text('pmlr', f'https://proceedings.mlr.press/{volume}/', timeout=300)
    return [html.unescape(' '.join(t.split())) for t in
            re.findall(r'<p class="title">([^<]+)</p>', page)]
