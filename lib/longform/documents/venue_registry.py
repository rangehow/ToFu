"""Versioned registry of official venue templates: one pinned edition per
venue, year and track, and the only source the document pipeline builds on.

Responsibility: load and validate ``venue_templates.json`` (catalogue format
``tofu.venue-templates/1``, shared with project-local gates), resolve a
paper's venue/year/track/stage to one pinned edition or refuse, fetch that
edition's official archive and verify it byte-for-byte against the pin, and
re-check every pinned edition against the venue's own pages (freshness).

Why pinned, not "newest": a template is per year, and a probe that falls back
from a missing year to the previous one silently builds a paper on the wrong
template, which venues desk-reject. Every edition therefore records the
official archive URL, its retrieval date and SHA-256, the SHA-256 of each
style file, the rules (page limits per paper type and stage, anonymity,
required and exempt sections) each with the official URL and a verbatim
quote, and the layout measured from the official sample. An explicit official
not-yet-published statement is ``not_released``; HTTP 404 or a failed fetch is
only ``unverified``, never proof of non-release. Resolving either refuses,
or, only when the brief names ``template_draft_on``, builds a
labelled draft on the named earlier edition that can never be
submission-ready. A ``superseded`` edition (deadline passed, newer one
pinned) is kept so its style files are recognised as the wrong year.

Entry points: ``load_catalogue``, ``validate_catalogue``, ``resolve``,
``fetch_pinned_kit``, ``check_freshness``, ``known_style_hashes``.
Dependencies: ``lib.http_transport.client`` (shared network authority) for
downloads; the caller's ``download`` seam replaces it in tests.
"""
from __future__ import annotations

import functools
import hashlib
import html
import io
import json
import re
import time
import zipfile
from pathlib import Path

__all__ = ['SCHEMA', 'CATALOGUE_PATH', 'TemplateRefused', 'load_catalogue',
           'validate_catalogue', 'editions', 'edition', 'resolve', 'fetch_pinned_kit',
           'check_freshness', 'known_style_hashes', 'STAGES', 'STATUSES']

SCHEMA = 'tofu.venue-templates/1'
CATALOGUE_PATH = Path(__file__).with_name('venue_templates.json')
STAGES = ('submission', 'rebuttal', 'camera_ready')
STATUSES = ('pinned', 'not_released', 'superseded', 'unpinned', 'unverified')

#: Official template archives are a few MiB at most (AAAI's, with Word files,
#: is ~5.5 MiB); this bounds a hostile or wrong response before the disk.
_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
_FETCH_DEADLINE_S = 120
_SHA_RE = re.compile(r'[0-9a-f]{64}')
_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')


class TemplateRefused(ValueError):
    """No pinned official template for the requested edition; the message
    says why and what to do."""


# ── catalogue ───────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int) -> dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_catalogue(path: str | Path | None = None) -> dict:
    path = Path(path or CATALOGUE_PATH)
    return _load(str(path), path.stat().st_mtime_ns)


def _cited(rule, where: str) -> list[str]:
    source = (rule or {}).get('source') or {}
    errors = []
    if not str(source.get('url') or '').startswith('https://'):
        errors.append(f'{where}: source.url must be an official https URL')
    if not str(source.get('quote') or '').strip():
        errors.append(f'{where}: source.quote (verbatim official text) is missing')
    return errors


def validate_catalogue(catalogue: dict) -> list[str]:
    """Every defect that would let a paper build on an unproven template."""
    errors = []
    if catalogue.get('schema') != SCHEMA:
        return [f'catalogue schema must be {SCHEMA!r}']
    ids = set()
    for ed in catalogue.get('editions') or []:
        eid = str(ed.get('id') or '')
        where = eid or '(edition without id)'
        if not eid or eid in ids:
            errors.append(f'{where}: id missing or duplicated')
        ids.add(eid)
        if ed.get('status') not in STATUSES:
            errors.append(f'{where}: status must be one of {list(STATUSES)}')
        if not ed.get('venue') or not ed.get('conference'):
            errors.append(f'{where}: venue and conference are required')
        if not _DATE_RE.fullmatch(str(ed.get('checked') or '')):
            errors.append(f'{where}: checked (YYYY-MM-DD) is required')
        if ed.get('status') in ('not_released', 'unpinned', 'unverified'):
            tried = ed.get('not_released_evidence') or []
            if not tried or not all(str(row.get('url') or '').startswith('https://')
                                    and row.get('result') for row in tried):
                errors.append(f'{where}: {ed.get("status")} needs the official URLs tried '
                              'and what each returned')
            if ed.get('status') == 'not_released' and not any(
                    str(row.get('result') or '').startswith('HTTP 200')
                    and row.get('quote') for row in tried):
                errors.append(f'{where}: not_released needs a quoted affirmative official '
                              'statement; HTTP failure alone is only unverified')
            if ed.get('status') == 'unpinned' and not ed.get('unpinned_reason'):
                errors.append(f'{where}: unpinned needs unpinned_reason')
            continue
        archive = ed.get('archive') or {}
        if not str(archive.get('url') or '').startswith('https://'):
            errors.append(f'{where}: archive.url must be the official https download')
        if not _SHA_RE.fullmatch(str(archive.get('sha256') or '')):
            errors.append(f'{where}: archive.sha256 must be a sha256 hex digest')
        if not _DATE_RE.fullmatch(str(archive.get('retrieved') or '')):
            errors.append(f'{where}: archive.retrieved (YYYY-MM-DD) is required')
        files = ed.get('files') or {}
        if not files or not all(_SHA_RE.fullmatch(str(v)) for v in files.values()):
            errors.append(f'{where}: files must map every official style file to its sha256')
        if ed.get('status') == 'superseded':
            continue   # kept so its files are recognised as the wrong year
        if ed.get('main_style_file') not in files:
            errors.append(f'{where}: main_style_file must name a pinned file')
        if not set(ed.get('owned_files') or ()) <= set(files):
            errors.append(f'{where}: owned_files must be pinned files')
        errors += _cited(ed.get('archive_source'), f'{where}: archive_source')
        limits = ed.get('page_limits') or {}
        if not limits or not all((stages or {}).get('submission') for stages in limits.values()):
            errors.append(f'{where}: page_limits needs a submission limit per paper type')
        errors += _cited(ed.get('page_limits_rule'), f'{where}: page_limits_rule')
        anonymity = ed.get('anonymity') or {}
        if not anonymity.get('rule'):
            errors.append(f'{where}: anonymity.rule is required')
        errors += _cited(anonymity, f'{where}: anonymity')
        for row in ed.get('required_sections') or []:
            if not row.get('heading'):
                errors.append(f'{where}: a required section has no heading')
            errors += _cited(row, f'{where}: required section {row.get("heading")!r}')
        exempt = ed.get('exempt_sections') or {}
        if exempt.get('headings'):
            errors += _cited(exempt, f'{where}: exempt_sections')
        if not ed.get('layout'):
            errors.append(f'{where}: layout (measured from the official sample) is required')
        for track, row in (ed.get('tracks') or {}).items():
            if row.get('source'):
                errors += _cited(row, f'{where}: track {track!r}')
    for ed in catalogue.get('editions') or []:
        for key in ('draft_fallback',):
            if ed.get(key) and ed[key] not in ids:
                errors.append(f'{ed.get("id")}: {key} {ed[key]!r} is not an edition')
    return errors


def editions(venue: str | None = None, catalogue: dict | None = None) -> list[dict]:
    rows = (catalogue or load_catalogue()).get('editions') or []
    return [row for row in rows if venue is None or row['venue'] == venue]


def edition(edition_id: str, catalogue: dict | None = None) -> dict:
    for row in editions(catalogue=catalogue):
        if row['id'] == edition_id:
            return row
    raise TemplateRefused(f'no venue template edition {edition_id!r} in the registry')


def known_style_hashes(catalogue: dict | None = None) -> dict[str, list[str]]:
    """sha256 -> ids of the editions whose official kit ships that file."""
    index: dict[str, list[str]] = {}
    for row in editions(catalogue=catalogue):
        for digest in (row.get('files') or {}).values():
            index.setdefault(digest, []).append(row['id'])
    return index


# ── resolution ──────────────────────────────────────────────────────────────

def _describe(rows: list[dict]) -> str:
    return ', '.join(f'{row.get("year") or "rolling"} ({row["status"]})' for row in rows)


def _not_released_message(ed: dict) -> str:
    tried = '; '.join(f'{row["url"]} -> {row["result"]}' for row in
                      ed.get('not_released_evidence') or [])
    fallback = ed.get('draft_fallback')
    advice = (f' To draft now, set brief "template_draft_on": "{fallback}"; that build is '
              'labelled a previous-year draft and can never be submission-ready.'
              if fallback else '')
    state = ('unable to verify template availability' if ed['status'] == 'unverified'
             else 'template not yet released')
    return (f'{ed["conference"]}: {state} (checked {ed["checked"]}: '
            f'{tried}). The pipeline never builds on another year\'s template '
            f'silently.{advice} When the venue publishes the kit, pin it '
            '(docs/DOCUMENT_PIPELINE.md, "Updating a venue template").')


def resolve(venue: str, year: int | str | None = None, *, track: str | None = None,
            paper_type: str | None = None, stage: str = 'submission',
            draft_on: str | None = None, catalogue: dict | None = None) -> dict:
    """The pinned edition and terms for one paper, or ``TemplateRefused``.

    Returns ``{'edition', 'target', 'submission_ready', 'draft_label',
    'paper_type', 'track', 'stage', 'page_limit'}``; ``edition`` is the pinned
    edition the build uses, ``target`` the one the paper is for."""
    catalogue = catalogue or load_catalogue()
    rows = editions(venue, catalogue)
    if not rows:
        known = sorted({row['venue'] for row in editions(catalogue=catalogue)})
        raise TemplateRefused(f'venue {venue!r} has no official template in the registry; '
                              f'one of {known}')
    if year in (None, ''):
        rolling = [row for row in rows if row.get('year') is None]
        if not rolling:
            raise TemplateRefused(
                f'{venue} templates are per year: brief field "venue_year" is required '
                f'(registry: {_describe(rows)})')
        target = rolling[0]
    else:
        matches = [row for row in rows if row.get('year') == int(year)]
        if not matches:
            raise TemplateRefused(
                f'no {venue} {year} template is pinned (registry: {_describe(rows)}). '
                'If the venue has published it, pin it (docs/DOCUMENT_PIPELINE.md, '
                '"Updating a venue template"); a previous year\'s template is never used '
                'silently.')
        target = matches[0]
    built, ready, label = target, True, ''
    if target['status'] == 'unpinned':
        tried = '; '.join(f'{row["url"]} -> {row["result"]}' for row in
                          target.get('not_released_evidence') or [])
        raise TemplateRefused(f'{target["conference"]}: the official template is not pinned '
                              f'({target["unpinned_reason"]}; checked {target["checked"]}: '
                              f'{tried}). Pin it before building '
                              '(docs/DOCUMENT_PIPELINE.md, "Updating a venue template").')
    if target['status'] in ('not_released', 'unverified'):
        if not draft_on:
            raise TemplateRefused(_not_released_message(target))
        built = edition(draft_on, catalogue)
        if (built['venue'] != target['venue'] or built['status'] != 'pinned'
                or built.get('year') is None or target.get('year') is None
                or built['year'] >= target['year']):
            raise TemplateRefused(f'template_draft_on {draft_on!r} must be a pinned earlier '
                                  f'{venue} edition')
        ready = False
        label = (f'DRAFT on the {built["conference"]} template: {target["conference"]} has '
                 'no verified pinned template; not submission-ready')
    elif draft_on:
        raise TemplateRefused(f'{target["conference"]} has a pinned template; remove '
                              f'template_draft_on {draft_on!r}')
    elif target['status'] == 'superseded':
        reason = target.get('superseded_reason') or 'its submission cycle has closed'
        raise TemplateRefused(
            f'{target["conference"]} is superseded ({reason}); target a current edition '
            f'(registry: {_describe(rows)})')
    terms = _terms(built, paper_type=paper_type, track=track, stage=stage)
    return {'edition': built, 'target': target, 'submission_ready': ready,
            'draft_label': label, **terms}


def _terms(ed: dict, *, paper_type, track, stage) -> dict:
    limits = ed['page_limits']
    types = list(limits)
    paper_type = paper_type or types[0]
    if paper_type not in limits:
        raise TemplateRefused(f'{ed["conference"]} paper_type {paper_type!r} is not one of '
                              f'{types}')
    if stage not in STAGES:
        raise TemplateRefused(f'stage {stage!r} is not one of {list(STAGES)}')
    limit = limits[paper_type].get(stage)
    if not limit:
        raise TemplateRefused(f'{ed["conference"]} publishes no {stage} page limit for '
                              f'{paper_type} papers ({ed["page_limits_rule"]["source"]["url"]})')
    tracks = ed.get('tracks') or {}
    if track and not tracks:
        raise TemplateRefused(f'{ed["conference"]} has no tracks; omit track {track!r}')
    track = track or next(iter(tracks), '')
    if tracks and track not in tracks:
        raise TemplateRefused(f'{ed["conference"]} track {track!r} is not one of '
                              f'{list(tracks)}')
    return {'paper_type': paper_type, 'track': track, 'stage': stage, 'page_limit': limit}


# ── fetch ───────────────────────────────────────────────────────────────────

def http_download(url: str) -> tuple[bytes, str]:
    """GET ``url`` through the shared network authority; (body, final URL)."""
    from lib.http_transport.client import http_get
    with http_get(url, timeout=_FETCH_DEADLINE_S, stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError(f'{url}: HTTP {response.status_code}')
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if len(body) + len(chunk) > _ARCHIVE_MAX_BYTES:
                raise ValueError(f'{url}: response larger than {_ARCHIVE_MAX_BYTES} bytes')
            body.extend(chunk)
        return bytes(body), str(getattr(response, 'url', '') or url)


def strip_root(names: list[str]) -> list[str]:
    """Drop a top-level folder shared by every entry (GitHub snapshots, AuthorKitYY/)."""
    tops = {name.split('/', 1)[0] for name in names}
    if len(tops) == 1 and all('/' in name for name in names):
        root = tops.pop() + '/'
        return [name[len(root):] for name in names]
    return list(names)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_archive_names(archive: zipfile.ZipFile) -> list[str]:
    """Official downloads are still untrusted bytes: never escape extraction."""
    names = archive.namelist()
    for name in names:
        if (name.startswith('/') or chr(92) in name
                or '..' in name.split('/') or re.match(r'^[A-Za-z]:', name)):
            raise TemplateRefused(f'unsafe archive member {name!r}; no kit extracted')
    return names


class TemplateUnverifiable(TemplateRefused):
    """Source identity cannot be established; never a current/released verdict."""


def _verify_destination(ed: dict, requested: str, final: str) -> None:
    pin = ed.get('archive') or {}
    expected = (pin.get('final_url') if requested == pin.get('url') else
                (ed.get('source_redirects') or {}).get(requested)) or requested
    if final.rstrip('/') != expected.rstrip('/'):
        raise TemplateUnverifiable(f'unrecorded redirect from {requested} to {final}; '
                                   f'expected {expected}; verify and pin the destination')


def verify_pinned_archive(ed: dict, data: bytes, final_url: str) -> dict:
    """Single archive/member verifier shared by intake and freshness.

    A matching ZIP comment alone proves nothing: regenerated bytes require
    BOTH the pinned commit marker and every pinned member's hash.
    """
    archive_pin = ed['archive']
    _verify_destination(ed, archive_pin['url'], final_url)
    digest = _sha(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise TemplateRefused(f'{archive_pin["url"]} is not a zip archive ({exc})') from exc
    commit = archive.comment.decode('ascii', 'replace').strip()
    note = ''
    if digest != archive_pin['sha256']:
        # GitHub regenerates snapshot zips (compression changes) for the same
        # commit; the commit and every pinned file prove the content instead.
        if not (archive_pin.get('upstream_commit') and commit == archive_pin['upstream_commit']):
            raise TemplateRefused(
                f'{ed["conference"]}: the official archive changed since it was pinned on '
                f'{archive_pin["retrieved"]} ({archive_pin["url"]} is sha256 {digest}, pinned '
                f'{archive_pin["sha256"]}). Run `scripts/document_pipeline.py venue-freshness '
                f'{ed["id"]}` and re-pin before building.')
        note = (f'archive bytes regenerated by the host; content verified by commit {commit} '
                'and every pinned file hash')
    names = _safe_archive_names(archive)
    kit_dir = str(ed.get('kit_dir') or '')
    members = {}
    for name, rel in zip(names, strip_root(names)):
        if name.endswith('/') or not rel.startswith(kit_dir):
            continue
        leaf = rel[len(kit_dir):]
        if leaf in members:
            raise TemplateRefused(f'duplicate official kit member {leaf!r}')
        members[leaf] = archive.read(name)
    wrong = sorted(leaf for leaf, pin in ed['files'].items()
                   if leaf not in members or _sha(members[leaf]) != pin)
    if wrong:
        raise TemplateRefused(f'{ed["conference"]}: official kit files {wrong} are missing or '
                              'differ from the pinned sha256; the venue changed its kit. '
                              f'Run `scripts/document_pipeline.py venue-freshness {ed["id"]}`.')
    return {'members': members, 'digest': digest, 'note': note}


def fetch_pinned_kit(ed: dict, destination: str | Path, *, download=http_download) -> dict:
    """Verify the whole pin before extracting; uses the freshness verifier too."""
    archive_pin = ed['archive']
    data, url = download(archive_pin['url'])
    verified = verify_pinned_archive(ed, data, url)
    digest, note = verified['digest'], verified['note']
    kit_dir = str(ed.get('kit_dir') or '')
    destination = Path(destination)
    sample_dir = destination / '_official_sample'
    destination.mkdir(parents=True, exist_ok=True)
    for leaf, content in verified['members'].items():
        if leaf in ed['files']:
            target = destination / leaf
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        target = sample_dir / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return {
        'venue': ed['venue'], 'edition': ed['id'], 'conference': ed['conference'],
        'family': ed.get('family') or ed['venue'], 'year': ed.get('year'),
        'landing_page': ed['archive_source']['source']['url'], 'source_url': archive_pin['url'],
        'final_url': url, 'upstream_commit': archive_pin.get('upstream_commit') or '',
        'archive_sha256': digest, 'pinned_archive_sha256': archive_pin['sha256'],
        'pinned_on': archive_pin['retrieved'], 'verification_note': note,
        'kit': kit_dir or '(archive root)', 'files': dict(ed['files']),
        'fetched_at': time.time(), 'engine': ed.get('engine') or 'pdflatex',
        'page_limit_source': f'{ed["page_limits_rule"]["source"]["url"]} '
                             f'("{ed["page_limits_rule"]["source"]["quote"]}")',
        'anonymity': ed['anonymity']['rule'],
        'unlimited_sections': list((ed.get('exempt_sections') or {}).get('headings') or ()),
        'required_sections': [row['heading'] for row in ed.get('required_sections') or []],
        'forbidden_packages': list(ed.get('forbidden_packages') or ()),
        'sample': ed.get('sample') or '', 'sample_dir': str(sample_dir),
    }


def pin_archive(url: str, *, kit_dir: str = '', suffixes: tuple[str, ...] = ('.sty', '.cls',
                                                                         '.bst'),
                sample: str = '', engine: str = 'pdflatex', workdir: str | Path,
                download=http_download) -> dict:
    """Measure a new edition's pin: archive sha256, kit file hashes, and the
    official sample's layout and printed text (for the anonymous author block).
    The rules and their quotes are written by the person pinning, from the
    official pages; this only measures what can be measured."""
    from lib.longform.documents.conformance import measure_layout
    from lib.longform.documents.render import pdf_text
    data, final = download(url)
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = _safe_archive_names(archive)
    commit = archive.comment.decode('ascii', 'replace').strip()
    files, sample_dir = {}, Path(workdir) / 'sample'
    for name, rel in zip(names, strip_root(names)):
        if name.endswith('/') or not rel.startswith(kit_dir):
            continue
        leaf = rel[len(kit_dir):]
        content = archive.read(name)
        target = sample_dir / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if '/' not in leaf and leaf.endswith(suffixes):
            files[leaf] = _sha(content)
    out = {'archive': {'url': url, 'final_url': final, 'sha256': _sha(data),
                       'retrieved': time.strftime('%Y-%m-%d', time.gmtime()),
                       'upstream_commit': commit if re.fullmatch(r'[0-9a-f]{40}', commit)
                       else ''},
           'kit_dir': kit_dir, 'files': files, 'sample': sample}
    if sample:
        out['layout'] = measure_layout(sample_dir, sample, engine)
        pdf = sample_dir / (Path(sample).stem + '.pdf')
        text = pdf_text(pdf) if pdf.is_file() else ''
        out['sample_anonymous_lines'] = [' '.join(line.split()) for line in text.splitlines()
                                         if 'anonymous' in line.casefold()][:6]
    return out


# ── freshness ───────────────────────────────────────────────────────────────

def page_text(raw: bytes) -> str:
    """Visible text of an HTML page or Markdown file, whitespace-normalised."""
    text = raw.decode('utf-8', 'replace')
    text = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return normalise(html.unescape(text))


def normalise(text: str) -> str:
    text = text.replace('\u2019', "'").replace('\u2018', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"').replace('\u00a0', ' ')
    text = re.sub(r'[*_`]+', '', text)      # Markdown emphasis in kit READMEs
    text = re.sub(r'\s+', ' ', text)
    # Removing tags leaves "icml2027.zip )" where the HTML had a link.
    text = re.sub(r'\s+([).,;:!?\]])', r'\1', re.sub(r'([(\[])\s+', r'\1', text))
    return text.strip().casefold()


def rule_quotes(ed: dict) -> list[tuple[str, str, str]]:
    """(rule, official URL, verbatim quote) for every cited rule of an edition."""
    rows = [('page limits', ed.get('page_limits_rule')), ('anonymity', ed.get('anonymity')),
            ('archive link', ed.get('archive_source')), ('exempt sections',
                                                         ed.get('exempt_sections'))]
    rows += [(f'required section {row.get("heading")!r}', row)
             for row in ed.get('required_sections') or []]
    rows += [(f'track {name!r}', row) for name, row in (ed.get('tracks') or {}).items()]
    rows += [(f'rule {row.get("name")!r}', row) for row in ed.get('extra_rules') or []]
    rows.append(('forbidden packages', ed.get('forbidden_packages_rule')))
    out = []
    for name, rule in rows:
        source = (rule or {}).get('source') or {}
        if source.get('url') and source.get('quote'):
            out.append((name, source['url'] + (f'#{source["archive_member"]}'
                                               if source.get('archive_member') else ''),
                        source['quote']))
    return out


def check_freshness(ed: dict, *, download=http_download) -> dict:
    """Re-fetch the edition's official pages and archive; report what changed.

    ``verdict``: ``current`` (archive and every quoted rule unchanged, no
    newer edition answering), ``changed`` (re-pin before building) or
    ``unverifiable`` (a source could not be fetched)."""
    report = {'edition': ed['id'], 'conference': ed['conference'], 'status': ed['status'],
              'checked_at': time.strftime('%Y-%m-%d', time.gmtime()),
              'archive': None, 'rules': [], 'newer': [], 'released': []}
    pages: dict[str, str | Exception] = {}
    finals: dict[str, str] = {}
    responses: dict[str, tuple[bytes, str]] = {}

    def get(url):
        if url not in responses:
            body, final = download(url)
            _verify_destination(ed, url, final)
            responses[url] = body, final
        return responses[url]

    def text_of(url):
        # ``archive#member`` is a file inside an official archive (rules a
        # venue states only in its kit, e.g. IJCAI's length rule).
        if url not in pages:
            base, _, member = url.partition('#')
            try:
                body, finals[url] = get(base)
                if member:
                    archive = zipfile.ZipFile(io.BytesIO(body))
                    names = dict(zip(strip_root(archive.namelist()), archive.namelist()))
                    body = archive.read(names[member])
                pages[url] = page_text(body)
            except (RuntimeError, ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
                pages[url] = exc
        return pages[url]

    if ed['status'] in ('not_released', 'unpinned', 'unverified'):
        # A page that failed when checked and now answers, or a page whose
        # recorded text ("will be posted here soon") is gone, may announce
        # the release: re-check by hand and pin.
        for row in ed.get('not_released_evidence') or []:
            text = text_of(row['url'])
            if isinstance(text, Exception):
                report['released'].append({'url': row['url'], 'now': str(text),
                                           'error': str(text), 'changed': False})
                continue
            was_ok = str(row['result']).startswith('HTTP 200')
            gone = bool(row.get('quote')) and normalise(row['quote']) not in text
            report['released'].append({'url': row['url'], 'now': 'HTTP 200',
                                       'changed': (not was_ok) or gone})
    else:
        try:
            data, final = get(ed['archive']['url'])
            verified = verify_pinned_archive(ed, data, final)
            report['archive'] = {'url': ed['archive']['url'], 'pinned': ed['archive']['sha256'],
                                 'now': verified['digest'], 'changed': False,
                                 'note': verified['note']}
        except TemplateUnverifiable as exc:
            report['archive'] = {'url': ed['archive']['url'], 'error': str(exc)}
        except TemplateRefused as exc:
            report['archive'] = {'url': ed['archive']['url'], 'changed': True,
                                 'error': str(exc)}
        except (RuntimeError, ValueError, OSError, zipfile.BadZipFile) as exc:
            report['archive'] = {'url': ed['archive']['url'], 'error': str(exc)}
        for name, url, quote in rule_quotes(ed):
            text = text_of(url)
            if isinstance(text, Exception):
                report['rules'].append({'rule': name, 'url': url, 'error': str(text)})
            else:
                report['rules'].append({'rule': name, 'url': url, 'quote': quote,
                                        'present': normalise(quote) in text})
    head = ed.get('upstream_head')
    if head:
        # A kit published as a moving branch (ACL): the branch head must still
        # be the pinned commit.
        try:
            body = get(head['url'])[0].decode('utf-8', 'replace')
            match = re.search(head.get('pattern') or r'"sha":\s*"([0-9a-f]{40})"', body)
            now = match.group(1) if match else ''
            report['upstream_head'] = {'url': head['url'], 'pinned': head['commit'],
                                       'now': now, 'changed': now != head['commit']}
        except (RuntimeError, ValueError, OSError) as exc:
            report['upstream_head'] = {'url': head['url'], 'error': str(exc)}
    for url in ed.get('newer_edition_probes') or []:
        text = text_of(url)
        # aaai.org redirects an unknown edition's page to an old one.
        moved = finals.get(url, url).rstrip('/') != url.rstrip('/')
        report['newer'].append({
            'url': url, 'answers': not isinstance(text, Exception) and not moved,
            'availability': ('unverified' if isinstance(text, Exception) or moved
                             else 'page_answers_requires_review'),
            'unverifiable': moved or (isinstance(text, Exception)
            and not re.search(r'HTTP (404|410)\b', str(text))),
            'result': str(text) if isinstance(text, Exception)
            else f'redirected to {finals[url]}' if moved else 'HTTP 200'})
    changed = (bool(report['archive'] and report['archive'].get('changed'))
               or any(row.get('present') is False for row in report['rules'])
               or any(row['answers'] for row in report['newer'])
               or any(row['changed'] for row in report['released'])
               or bool((report.get('upstream_head') or {}).get('changed')))
    unverifiable = (bool(report['archive'] and report['archive'].get('error'))
                    or bool((report.get('upstream_head') or {}).get('error'))
                    or any(row.get('error') for row in report['rules'])
                    or any(row.get('error') for row in report['released']))
    unverifiable = (unverifiable or ed['status'] in ('unverified', 'unpinned')
                    or any(row.get('unverifiable') for row in report['newer']))
    report['scope'] = 'Pinned edition archive and recorded rules only; future-edition '
    report['scope'] += 'HTTP 404/410 is unknown availability, never proof of non-release.'
    report['verdict'] = 'changed' if changed else 'unverifiable' if unverifiable else 'current'
    return report
