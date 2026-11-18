"""Official venue LaTeX kits, fetched fresh from the venue's own source.

Responsibility: the registry of venues whose official template has a stable
"newest" location, and ``fetch_official_kit``, which downloads that archive in
the current run through the shared HTTP transport, records its provenance
(URL, upstream commit from the GitHub archive comment, SHA-256, time) and
extracts exactly the newest kit files. The pipeline never uses a vendored or
cached copy: the intake stage is not resumable, so every run refetches.

Add a venue by naming its official archive URL, how to select the newest kit
inside it, its preamble, bibliography style and engine. A venue without an
official "newest" location is refused rather than guessed.

Entry points: ``VENUES``, ``fetch_official_kit``, ``main_tex``.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = ['Venue', 'VENUES', 'fetch_official_kit', 'main_tex']

#: Official template archives are well under 1 MiB; this bounds a hostile or
#: wrong response before it reaches the disk.
_ARCHIVE_MAX_BYTES = 32 * 1024 * 1024
_FETCH_DEADLINE_S = 120


def _top_level(prefix_names: list[str]) -> str:
    return ''


def _newest_iclr(names: list[str]) -> str:
    years = sorted({int(m.group(1)) for name in names
                    for m in [re.match(r'[^/]+/iclr(\d{4})/', name)] if m})
    if not years:
        raise ValueError('ICLR archive has no iclrYYYY/ kit directory')
    return f'iclr{years[-1]}/'


@dataclass(frozen=True)
class Venue:
    name: str
    archive_url: str
    select_kit: Callable[[list[str]], str]   # archive names → kit subdir
    kit_suffixes: tuple[str, ...]
    preamble: Callable[[str], str]           # kit subdir → preamble
    bibstyle: Callable[[str], str]
    engine: str = 'pdflatex'


def _acl_preamble(_kit: str) -> str:
    return ('\\documentclass[11pt]{article}\n\\usepackage[review]{acl}\n'
            '\\usepackage{times}\n\\usepackage{latexsym}\n'
            '\\usepackage[T1]{fontenc}\n\\usepackage[utf8]{inputenc}\n'
            '\\usepackage{microtype}\n\\usepackage{inconsolata}\n'
            '\\usepackage{graphicx}\n\\usepackage{booktabs}\n'
            '\\usepackage{amsmath}\n\\usepackage{amssymb}\n')


def _iclr_preamble(kit: str) -> str:
    year = kit.strip('/')
    return (f'\\documentclass{{article}}\n\\usepackage{{{year}_conference,times}}\n'
            '\\input{math_commands.tex}\n\\usepackage{hyperref}\n'
            '\\usepackage{url}\n\\usepackage{graphicx}\n'
            '\\usepackage{booktabs}\n\\usepackage{amsmath}\n\\usepackage{amssymb}\n')


VENUES = {
    'acl': Venue(
        'acl', 'https://github.com/acl-org/acl-style-files/archive/refs/heads/master.zip',
        # acl.sty itself declares \bibliographystyle{acl_natbib}; a second
        # declaration is a BibTeX error that leaves every citation undefined.
        _top_level, ('.sty', '.bst'), _acl_preamble, lambda _kit: ''),
    'iclr': Venue(
        'iclr', 'https://github.com/ICLR/Master-Template/archive/refs/heads/master.zip',
        _newest_iclr, ('.sty', '.bst', 'math_commands.tex'), _iclr_preamble,
        lambda kit: kit.strip('/') + '_conference'),
}


def _download(url: str) -> bytes:
    from lib.http_transport.client import http_get
    with http_get(url, timeout=_FETCH_DEADLINE_S, stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError(f'official template HTTP {response.status_code}')
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if len(body) + len(chunk) > _ARCHIVE_MAX_BYTES:
                raise ValueError('official template archive too large')
            body.extend(chunk)
        return bytes(body)


def fetch_official_kit(venue_name: str, destination: str | Path, *,
                       download=_download) -> dict:
    """Fetch the newest official kit into ``destination``; return provenance."""
    venue = VENUES.get(venue_name)
    if venue is None:
        raise ValueError(f'venue {venue_name!r} has no registered official '
                         f'template source; one of {sorted(VENUES)}')
    data = download(venue.archive_url)
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = archive.namelist()
    root = names[0].split('/', 1)[0] + '/'
    kit = venue.select_kit(names)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in names:
        rel = name[len(root):]
        if not rel.startswith(kit) or rel.endswith('/'):
            continue
        leaf = rel[len(kit):]
        if '/' in leaf or not leaf.endswith(venue.kit_suffixes):
            continue
        content = archive.read(name)
        (destination / leaf).write_bytes(content)
        files[leaf] = hashlib.sha256(content).hexdigest()
    if not files:
        raise ValueError(f'{venue_name} archive held no kit files under {kit!r}')
    commit = archive.comment.decode('ascii', 'replace').strip()
    return {
        'venue': venue_name, 'source_url': venue.archive_url,
        'upstream_commit': commit if re.fullmatch(r'[0-9a-f]{40}', commit) else '',
        'archive_sha256': hashlib.sha256(data).hexdigest(),
        'kit': kit or '(archive root)', 'files': files,
        'fetched_at': time.time(), 'engine': venue.engine,
        'preamble': venue.preamble(kit), 'bibstyle': venue.bibstyle(kit),
    }


def main_tex(*, preamble: str, title: str, authors: str, abstract: str,
             body: str, bibstyle: str, bibfile: str) -> str:
    """Assemble a paper's ``main.tex`` from its kit preamble and sections."""
    return (preamble + f'\n\\title{{{title}}}\n\\author{{{authors}}}\n'
            '\\begin{document}\n\\maketitle\n'
            f'\\begin{{abstract}}\n{abstract}\n\\end{{abstract}}\n\n'
            f'{body}\n\n'
            + (f'\\bibliographystyle{{{bibstyle}}}\n' if bibstyle else '')
            + f'\\bibliography{{{bibfile}}}\n\\end{{document}}\n')
