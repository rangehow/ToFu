"""Venue kits: the per-year template registry's entry points and the frame a
freshly written paper uses in each official kit family.

Responsibility: ``venue_terms`` resolves a brief's venue, year, paper type,
track and stage to one pinned edition of ``venue_registry`` (or refuses:
an unpinned or unreleased year is never replaced by another year's template),
``fetch_official_kit`` downloads that edition's official archive and verifies
it against its pinned sha256, and ``frame_main_tex`` writes a fresh paper's
``main.tex`` in the kit family's official preamble (the kit's own sample
paper's preamble, with the review/camera-ready option the stage needs).

Rules (page limits, anonymity, required and exempt sections, forbidden
packages, tracks) live per edition in ``venue_templates.json``, each with the
official URL and quote it comes from; code here holds only what is the same
for every year of a kit family (how its preamble is written).

Entry points: ``venue_terms``, ``fetch_official_kit``, ``frame_main_tex``,
``FRAMES``.
"""
from __future__ import annotations

import re

from . import venue_registry as registry

__all__ = ['FRAMES', 'STAGES', 'venue_terms', 'fetch_official_kit', 'frame_main_tex']

STAGES = registry.STAGES


def venue_terms(venue_name: str, *, year=None, paper_type: str | None = None,
                track: str | None = None, stage: str = 'submission',
                draft_on: str | None = None) -> dict:
    """The pinned edition's terms for one paper; ``registry.TemplateRefused``
    (a ValueError) names the missing edition or unpublished choice."""
    resolved = registry.resolve(venue_name, year, track=track, paper_type=paper_type,
                                stage=stage, draft_on=draft_on)
    ed = resolved['edition']
    return {'paper_type': resolved['paper_type'], 'track': resolved['track'],
            'stage': resolved['stage'], 'page_limit': resolved['page_limit'],
            'edition': ed['id'], 'target_edition': resolved['target']['id'],
            'submission_ready': resolved['submission_ready'],
            'draft_label': resolved['draft_label'],
            'track_option': ((ed.get('tracks') or {}).get(resolved['track']) or {}).get(
                'option', '')}


def fetch_official_kit(edition_id: str, destination, *, download=registry.http_download) -> dict:
    """Fetch and verify one pinned edition's official kit; return provenance."""
    ed = registry.edition(edition_id)
    if ed['status'] not in ('pinned',):
        raise registry.TemplateRefused(f'{ed["conference"]} is {ed["status"]}; it has no '
                                       'buildable pinned template')
    return registry.fetch_pinned_kit(ed, destination, download=download)


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


def _final(kit: dict) -> bool:
    return kit.get('stage') == 'camera_ready'


def _frame_acl(kit, title, authors, abstract, body, bibfile):
    option = 'final' if _final(kit) else 'review'
    preamble = (f'\\documentclass[11pt]{{article}}\n\\usepackage[{option}]{{acl}}\n'
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
                + _COMMON + ('\\iclrfinalcopy\n' if _final(kit) else ''))
    return preamble + _plain_body(title, authors, abstract, body, style, bibfile)


def _frame_colm(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(colm\d{4}_conference)\.sty')
    option = 'final' if _final(kit) else 'submission'
    preamble = (f'\\documentclass{{article}}\n\\usepackage[{option}]{{{style}}}\n'
                '\\usepackage{microtype}\n\\usepackage{hyperref}\n\\usepackage{url}\n'
                '\\usepackage{lineno}\n' + _COMMON)
    return preamble + _plain_body(title, authors, abstract, body, style, bibfile)


def _frame_neurips(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(neurips_\d{4})\.sty')
    # neurips_2026.tex: "[eandd]" for the Evaluations & Datasets track, and
    # "final" behind the track for the camera-ready version.
    options = kit.get('track_option') or 'main'
    if _final(kit):
        options += ',final'
    preamble = (f'\\documentclass{{article}}\n\\usepackage[{options}]{{{style}}}\n'
                '\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n'
                '\\usepackage{hyperref}\n\\usepackage{url}\n\\usepackage{microtype}\n'
                + _COMMON)
    return preamble + _plain_body(title, authors, abstract, body, 'plainnat', bibfile)


def _frame_icml(kit, title, authors, abstract, body, bibfile):
    style = _style(kit, r'(icml\d{4})\.sty')
    option = '[accepted]' if _final(kit) else ''
    return (f'\\documentclass{{article}}\n\\usepackage{{microtype}}\n\\usepackage{{hyperref}}\n'
            f'\\usepackage{option}{{{style}}}\n' + _COMMON
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
    option = '' if _final(kit) else '[submission]'
    # The kit's "DO NOT CHANGE THIS" preamble, verbatim in substance.
    return (f'\\documentclass[letterpaper]{{article}}\n\\usepackage{option}{{{style}}}\n'
            '\\usepackage[hyphens]{url}\n\\usepackage{graphicx}\n\\urlstyle{rm}\n'
            '\\def\\UrlFont{\\rm}\n\\usepackage{natbib}\n\\usepackage{caption}\n'
            '\\frenchspacing\n\\usepackage{booktabs}\n'
            '\\usepackage{amsmath}\n\\usepackage{amssymb}\n'
            f'\\title{{{title}}}\n\\author{{{authors}}}\n\\affiliations{{}}\n'
            '\\begin{document}\n\\maketitle\n'
            f'\\begin{{abstract}}\n{abstract}\n\\end{{abstract}}\n\n{body}\n\n'
            f'\\bibliography{{{bibfile}}}\n\\end{{document}}\n')


def _frame_cvpr(kit, title, authors, abstract, body, bibfile):
    year = kit.get('year') or ''
    option = '' if _final(kit) else '[review]'
    return ('\\documentclass[10pt,twocolumn,letterpaper]{article}\n'
            f'\\usepackage{option}{{cvpr}}\n\\input{{preamble}}\n'
            '\\definecolor{cvprblue}{rgb}{0.21,0.49,0.74}\n'
            '\\usepackage[pagebackref,breaklinks,colorlinks,allcolors=cvprblue]{hyperref}\n'
            f'\\def\\confName{{CVPR}}\n\\def\\confYear{{{year}}}\n'
            f'\\title{{{title}}}\n\\author{{{authors}}}\n\\begin{{document}}\n\\maketitle\n'
            f'\\begin{{abstract}}\n{abstract}\n\\end{{abstract}}\n\n{body}\n\n'
            '{\\small\n\\bibliographystyle{ieeenat_fullname}\n'
            f'\\bibliography{{{bibfile}}}\n}}\n\\end{{document}}\n')


#: Kit family -> fresh-paper frame. A family without a frame (IJCAI, ECCV)
#: still serves existing drafts and the conformance gate.
FRAMES = {'acl': _frame_acl, 'iclr': _frame_iclr, 'colm': _frame_colm,
          'neurips': _frame_neurips, 'icml': _frame_icml, 'aaai': _frame_aaai,
          'cvpr': _frame_cvpr}


def frame_main_tex(family: str, kit: dict, *, title: str, authors: str,
                   abstract: str, body: str, bibfile: str) -> str:
    """A fresh paper's ``main.tex`` in the kit family's official frame."""
    frame = FRAMES.get(family)
    if frame is None:
        raise registry.TemplateRefused(
            f'no fresh-paper frame for the {family} kit; revise an existing draft written '
            'on the official kit instead (brief draft.main)')
    return frame(kit, title, authors, abstract, body, bibfile)
