"""Official venue LaTeX kits, fetched fresh from the venue's own source.

Responsibility: the registry of venues and ``fetch_official_kit``, which finds
the newest official kit in the current run, downloads it through the shared
HTTP transport, records its provenance (landing page, archive URL, upstream
commit when the archive is a GitHub snapshot, SHA-256, time) and extracts the
kit files. The pipeline never uses a vendored or cached copy: intake is not
resumable, so every run refetches.

"Newest" is decided by the venue's own publication, never by a pinned year:
  * year-addressed venues (ICML, NeurIPS, CVPR, AAAI) are probed for the
    coming conference year first, then the current one (the calendar, not a
    tuning choice), and the first official page that links a kit wins;
  * repository venues (ACL, ICLR, COLM) download the official repository's
    default branch and pick the newest year-named kit inside it.

Each venue declares its body page limit (from its call for papers, URL
recorded), its anonymity rule, the unnumbered sections that do not count
toward the limit, its forbidden packages, and how a fresh paper is framed
(``frame``). ``sample`` names the official example paper that
``scripts/document_pipeline.py check-venue`` compiles. A venue without an
official kit location is refused rather than guessed.

Entry points: ``VENUES``, ``fetch_official_kit``, ``frame_main_tex``.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

__all__ = ['Venue', 'VENUES', 'fetch_official_kit', 'frame_main_tex']

#: Official template archives are a few MiB at most (AAAI's, with Word files,
#: is ~5.5 MiB); this bounds a hostile or wrong response before the disk.
_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
_FETCH_DEADLINE_S = 120


@dataclass(frozen=True)
class Venue:
    name: str
    page_limit: int                 # body pages, from the call for papers
    page_limit_source: str
    anonymity: str                  # what the review build must do
    # (landing page template with {year}, regex for the kit link) or a fixed URL
    archive: str
    landing: str = ''
    select_kit: Callable[[list[str]], str] = lambda names: ''
    kit_suffixes: tuple[str, ...] = ('.sty', '.bst', '.cls')
    sample: str = ''                # official example paper inside the kit
    unlimited_sections: tuple[str, ...] = ()   # unnumbered sections after the body
    forbidden_packages: tuple[str, ...] = ()
    engine: str = 'pdflatex'
    frame: Callable[..., str] = field(default=None)


def _strip_root(names: list[str]) -> list[str]:
    """Drop a top-level folder shared by every entry (GitHub snapshots, AuthorKitYY/)."""
    tops = {name.split('/', 1)[0] for name in names}
    if len(tops) == 1 and all('/' in name for name in names):
        root = tops.pop() + '/'
        return [name[len(root):] for name in names]
    return list(names)


def _top_level(_names: list[str]) -> str:
    return ''


def _newest_year_dir(prefix: str):
    def select(names: list[str]) -> str:
        years = sorted({int(m.group(1)) for name in _strip_root(names)
                        for m in [re.match(prefix + r'(\d{4})/', name)] if m})
        if not years:
            raise ValueError(f'archive has no {prefix}YYYY/ kit directory')
        return f'{prefix}{years[-1]}/'
    return select


def _style(kit: dict, pattern: str) -> str:
    """Stem of the kit file matching ``pattern`` (e.g. the year-named .sty)."""
    for leaf in sorted(kit['files']):
        match = re.fullmatch(pattern, leaf)
        if match:
            return match.group(1)
    raise ValueError(f'kit has no file matching {pattern!r}')


_COMMON = ('\\usepackage{graphicx}\n\\usepackage{booktabs}\n'
           '\\usepackage{amsmath}\n\\usepackage{amssymb}\n')


def _plain_body(title, authors, abstract, body, bibstyle, bibfile):
    return (f'\\title{{{title}}}\n\\author{{{authors}}}\n\\begin{{document}}\n'
            f'\\maketitle\n\\begin{{abstract}}\n{abstract}\n\\end{{abstract}}\n\n'
            f'{body}\n\n' + (f'\\bibliographystyle{{{bibstyle}}}\n' if bibstyle else '')
            + f'\\bibliography{{{bibfile}}}\n\\end{{document}}\n')


def _frame_acl(kit, title, authors, abstract, body, bibfile):
    preamble = ('\\documentclass[11pt]{article}\n\\usepackage[review]{acl}\n'
                '\\usepackage{times}\n\\usepackage{latexsym}\n'
                '\\usepackage[T1]{fontenc}\n\\usepackage[utf8]{inputenc}\n'
                '\\usepackage{microtype}\n\\usepackage{inconsolata}\n' + _COMMON)
    # acl.sty itself declares \bibliographystyle{acl_natbib}; a second
    # declaration is a BibTeX error that leaves every citation undefined.
    return preamble + _plain_body(title, authors, abstract, body, '', bibfile)


def _frame_iclr(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(iclr\d{4}_conference)\.sty')
    preamble = (f'\\documentclass{{article}}\n\\usepackage{{{style},times}}\n'
                '\\input{math_commands.tex}\n\\usepackage{hyperref}\n\\usepackage{url}\n'
                + _COMMON)
    return preamble + _plain_body(title, authors, abstract, body, style, bibfile)


def _frame_colm(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(colm\d{4}_conference)\.sty')
    preamble = (f'\\documentclass{{article}}\n\\usepackage[submission]{{{style}}}\n'
                '\\usepackage{microtype}\n\\usepackage{hyperref}\n\\usepackage{url}\n'
                '\\usepackage{lineno}\n' + _COMMON)
    return preamble + _plain_body(title, authors, abstract, body, style, bibfile)


def _frame_neurips(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(neurips_\d{4})\.sty')
    preamble = (f'\\documentclass{{article}}\n\\usepackage{{{style}}}\n'
                '\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n'
                '\\usepackage{hyperref}\n\\usepackage{url}\n\\usepackage{microtype}\n'
                + _COMMON)
    return preamble + _plain_body(title, authors, abstract, body, 'plainnat', bibfile)


def _frame_icml(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(icml\d{4})\.sty')
    return (f'\\documentclass{{article}}\n\\usepackage{{microtype}}\n\\usepackage{{hyperref}}\n'
            f'\\usepackage{{{style}}}\n' + _COMMON
            + f'\\icmltitlerunning{{{title}}}\n\\begin{{document}}\n\\twocolumn[\n'
            f'  \\icmltitle{{{title}}}\n  \\begin{{icmlauthorlist}}\n'
            f'    \\icmlauthor{{{authors}}}{{anon}}\n  \\end{{icmlauthorlist}}\n'
            '  \\icmlaffiliation{anon}{Anonymous Institution}\n'
            f'  \\icmlcorrespondingauthor{{{authors}}}{{anonymous@example.com}}\n'
            '  \\vskip 0.3in\n]\n\\printAffiliationsAndNotice{}\n'
            f'\\begin{{abstract}}\n{abstract}\n\\end{{abstract}}\n\n{body}\n\n'
            f'\\bibliography{{{bibfile}}}\n\\bibliographystyle{{{style}}}\n\\end{{document}}\n')


def _frame_aaai(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(aaai\d{4})\.sty')
    # The kit's "DO NOT CHANGE THIS" preamble, verbatim in substance.
    return (f'\\documentclass[letterpaper]{{article}}\n\\usepackage[submission]{{{style}}}\n'
            '\\usepackage[hyphens]{url}\n\\usepackage{graphicx}\n\\urlstyle{rm}\n'
            '\\def\\UrlFont{\\rm}\n\\usepackage{natbib}\n\\usepackage{caption}\n'
            '\\frenchspacing\n\\setlength{\\pdfpagewidth}{8.5in}\n'
            '\\setlength{\\pdfpageheight}{11in}\n\\usepackage{booktabs}\n'
            '\\usepackage{amsmath}\n\\usepackage{amssymb}\n'
            f'\\title{{{title}}}\n\\author{{{authors}}}\n\\affiliations{{}}\n'
            '\\begin{document}\n\\maketitle\n'
            f'\\begin{{abstract}}\n{abstract}\n\\end{{abstract}}\n\n{body}\n\n'
            f'\\bibliography{{{bibfile}}}\n\\end{{document}}\n')


def _frame_cvpr(kit, title, authors, abstract, body, bibfile):
    year = kit.get('year') or ''
    return ('\\documentclass[10pt,twocolumn,letterpaper]{article}\n'
            '\\usepackage[review]{cvpr}\n\\input{preamble}\n'
            '\\definecolor{cvprblue}{rgb}{0.21,0.49,0.74}\n'
            '\\usepackage[pagebackref,breaklinks,colorlinks,allcolors=cvprblue]{hyperref}\n'
            f'\\def\\confName{{CVPR}}\n\\def\\confYear{{{year}}}\n'
            f'\\title{{{title}}}\n\\author{{{authors}}}\n\\begin{{document}}\n\\maketitle\n'
            f'\\begin{{abstract}}\n{abstract}\n\\end{{abstract}}\n\n{body}\n\n'
            '{\\small\n\\bibliographystyle{ieeenat_fullname}\n'
            f'\\bibliography{{{bibfile}}}\n}}\n\\end{{document}}\n')


VENUES = {
    'acl': Venue(
        'acl', 8, 'https://github.com/acl-org/acl-style-files/blob/master/formatting.md '
        '(long papers 8 pages, short 4; set page_limit 4 for a short paper)',
        'review option: anonymous authors, line numbers',
        archive='https://github.com/acl-org/acl-style-files/archive/refs/heads/master.zip',
        select_kit=_top_level, kit_suffixes=('.sty', '.bst'), sample='acl_latex.tex',
        unlimited_sections=('Limitations', 'Ethics Statement', 'Acknowledgments'),
        frame=_frame_acl),
    'iclr': Venue(
        'iclr', 9, 'the newest iclrYYYY_conference.tex in https://github.com/ICLR/Master-Template '
        '("strict upper limit of 9 pages for the main text")',
        'default (non-final) build: anonymous authors',
        archive='https://github.com/ICLR/Master-Template/archive/refs/heads/master.zip',
        select_kit=_newest_year_dir('iclr'), kit_suffixes=('.sty', '.bst', 'math_commands.tex'),
        sample='{kit_stem}.tex', unlimited_sections=('Ethics Statement',
                                                     'Reproducibility Statement'),
        frame=_frame_iclr),
    'colm': Venue(
        'colm', 9, 'https://colm.cc/Conferences/2026/CallForPapers and the kit\'s '
        'colmYYYY_conference.tex ("strict upper limit of 9 pages for the main text")',
        'submission option: anonymous authors, line numbers',
        archive='https://github.com/COLM-org/Template/archive/refs/heads/master.zip',
        select_kit=_top_level, kit_suffixes=('.sty', '.bst', 'math_commands.tex'),
        sample='{kit_stem}.tex', unlimited_sections=('Ethics Statement', 'Acknowledgments'),
        frame=_frame_colm),
    'icml': Venue(
        'icml', 8, 'https://icml.cc/Conferences/{year}/AuthorInstructions ("main body, '
        'which can be up to eight pages long")',
        'default build (no [accepted]): anonymous authors; \\printAffiliationsAndNotice required',
        archive=r'https://media\.icml\.cc/Conferences/ICML\d{4}/styles/icml\d{4}\.zip',
        landing='https://icml.cc/Conferences/{year}/AuthorInstructions',
        sample='example_paper.tex',
        unlimited_sections=('Impact Statement', 'AI Use Statement', 'Accessibility',
                            'Software and Data', 'Acknowledgements', 'Acknowledgments'),
        frame=_frame_icml),
    'neurips': Venue(
        'neurips', 9, 'https://neurips.cc/Conferences/{year}/CallForPapers and the kit\'s '
        'formatting instructions ("up to nine pages long, including figures")',
        'default build (no final/preprint option): anonymous authors',
        archive=r'https://media\.neurips\.cc/Conferences/NeurIPS\d{4}/[^"\s]+\.zip',
        landing='https://neurips.cc/Conferences/{year}/CallForPapers',
        sample='{kit_stem}.tex',
        unlimited_sections=('Acknowledgments', 'Acknowledgements', 'NeurIPS Paper Checklist'),
        frame=_frame_neurips),
    'aaai': Venue(
        'aaai', 7, 'https://aaai.org/conference/aaai/aaai-27/submission-instructions/ '
        '("up to 7 pages of non-references content", pages 8-9 references only)',
        'submission option: anonymous, no acknowledgements, ethics statement inside 7 pages',
        archive='https://aaai.org/authorkit{yy}/',
        select_kit=_top_level, sample='AnonymousSubmission{year}.tex',
        forbidden_packages=('authblk', 'balance', 'CJK', 'float', 'flushend', 'fullpage',
                            'geometry', 'hyperref', 'navigator', 'indentfirst', 'layout',
                            'multicol', 'nameref', 'savetrees', 'setspace', 'stfloats',
                            'tabu', 'titlesec', 'tocbibind', 'ulem', 'wrapfig'),
        frame=_frame_aaai),
    'cvpr': Venue(
        'cvpr', 8, 'https://cvpr.thecvf.com/Conferences/{year}/AuthorGuidelines and the kit '
        '("excluding the references section, must be no longer than eight pages")',
        'review option: anonymous, line numbers',
        archive=r'https://media\.eventhosts\.cc/Conferences/CVPR\d{4}/[^"\s]+latex\.zip',
        landing='https://cvpr.thecvf.com/Conferences/{year}/AuthorGuidelines',
        kit_suffixes=('.sty', '.bst', 'preamble.tex'), sample='main.tex',
        unlimited_sections=('Acknowledgments', 'Acknowledgements'),
        frame=_frame_cvpr),
}


def _download(url: str) -> tuple[bytes, str]:
    """GET ``url``; return (body, final URL after redirects)."""
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


def _candidate_years(now: float | None = None) -> tuple[int, int]:
    year = time.gmtime(now).tm_year
    return (year + 1, year)


def _locate_archive(venue: Venue, download, now=None) -> tuple[bytes, str, str, int | None]:
    """Return (archive bytes, archive URL, landing page, conference year)."""
    if not venue.landing and '{yy}' not in venue.archive:
        data, url = download(venue.archive)
        return data, url, venue.archive, None
    tried = []
    for year in _candidate_years(now):
        try:
            if '{yy}' in venue.archive:
                page = venue.archive.format(yy=f'{year % 100:02d}')
                data, url = download(page)
                if data[:2] == b'PK':
                    return data, url, page, year
                tried.append(f'{page}: not a zip')
                continue
            page = venue.landing.format(year=year)
            html, _ = download(page)
            match = re.search(venue.archive, html.decode('utf-8', 'replace'))
            if not match:
                tried.append(f'{page}: links no kit')
                continue
            data, url = download(match.group(0))
            return data, url, page, year
        except (RuntimeError, ValueError) as exc:
            tried.append(str(exc))
    raise ValueError(f'no official {venue.name} kit found: ' + '; '.join(tried))


def fetch_official_kit(venue_name: str, destination: str | Path, *,
                       download=_download, now: float | None = None) -> dict:
    """Fetch the newest official kit into ``destination``; return provenance."""
    venue = VENUES.get(venue_name)
    if venue is None:
        raise ValueError(f'venue {venue_name!r} has no registered official '
                         f'template source; one of {sorted(VENUES)}')
    data, url, page, year = _locate_archive(venue, download, now)
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = archive.namelist()
    kit = venue.select_kit(names)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    files, sample_files = {}, {}
    for name, rel in zip(names, _strip_root(names)):
        if name.endswith('/') or not rel or not rel.startswith(kit):
            continue
        leaf = rel[len(kit):]
        content = archive.read(name)
        if '/' not in leaf and leaf.endswith(venue.kit_suffixes):
            (destination / leaf).write_bytes(content)
            files[leaf] = hashlib.sha256(content).hexdigest()
        sample_files[leaf] = content
    if not files:
        raise ValueError(f'{venue_name} archive held no kit files under {kit!r}')
    commit = archive.comment.decode('ascii', 'replace').strip()
    if year is None:
        stamped = re.findall(r'(20\d\d)', ' '.join(files))
        year = int(max(stamped)) if stamped else None
    sample_dir = destination / '_official_sample'
    for leaf, content in sample_files.items():
        target = sample_dir / leaf
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    kit_stem = next((Path(leaf).stem for leaf in sorted(files) if leaf.endswith('.sty')
                     and re.search(r'\d{4}', leaf)), '')
    return {
        'venue': venue_name, 'landing_page': page, 'source_url': url,
        'upstream_commit': commit if re.fullmatch(r'[0-9a-f]{40}', commit) else '',
        'archive_sha256': hashlib.sha256(data).hexdigest(),
        'kit': kit or '(archive root)', 'files': files, 'year': year,
        'fetched_at': time.time(), 'engine': venue.engine,
        'page_limit': venue.page_limit,
        'page_limit_source': venue.page_limit_source.format(year=year),
        'anonymity': venue.anonymity,
        'unlimited_sections': list(venue.unlimited_sections),
        'forbidden_packages': list(venue.forbidden_packages),
        'sample': venue.sample.format(kit_stem=kit_stem, year=year),
        'sample_dir': str(sample_dir),
    }


def frame_main_tex(venue_name: str, kit: dict, *, title: str, authors: str,
                   abstract: str, body: str, bibfile: str) -> str:
    """A fresh paper's ``main.tex`` in the venue's official frame."""
    return VENUES[venue_name].frame(kit, title, authors, abstract, body, bibfile)
