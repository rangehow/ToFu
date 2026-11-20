"""Source-level checks for the pinned IJCAI/AAAI 2027 content policies.

Entry point: ``content_rule_rows`` returns conformance-compatible rows. It is
read-only and uses the existing paper-layout heading scanner plus the supplied edition.
Policies are edition-specific, not assumptions about future venue editions.
Evidence comes from the edition's cited anonymity/exempt-sections rules.

Problem: a generic appendix boundary can incorrectly exempt AAAI content.
Decision: additional pages require a reference-only tail; ambiguous appendix
placement fails closed unless a separate *all non-reference content* page
measurement proves it in budget. The ordinary first-bibliography/appendix
body-end probe is NOT that measurement. Do not pass it as such.
Alternative rejected: globally forbidding appendices (in-budget appendices
are not prohibited by the cited rule). This is a source gate, not a TeX
interpreter: custom macros/conditional compilation still need rendered review.
A manual bibliography environment does not prove its enclosed prose is references;
over-budget manual bibliographies without independent page evidence are unknown.
"""
from __future__ import annotations

import re

from lib.paper.latex_layout import iter_section_headings

__all__ = ['content_rule_rows']

_REFERENCES = re.compile(
    r'\\bibliography\s*\{[^}]*\}|\\printbibliography(?:\s*\[[^\]]*\])?'
    r'|\\begin\s*\{thebibliography\}')


def _headings(source: str):
    for _, _, raw_title in iter_section_headings(source):
        title = re.sub(r'\\[A-Za-z]+\*?', '', raw_title)
        yield ' '.join(re.sub(r'[^A-Za-z0-9]+', ' ', title).casefold().split())


def _row(name, ok, detail, evidence, *, status=None):
    row = {'check': name, 'ok': bool(ok), 'detail': detail, 'source': evidence}
    if status:
        row['status'] = status
    return row


def _reference_only_tail(source: str) -> tuple[bool, str]:
    """Conventional references; manual contents are not proved by an env name."""
    references = _REFERENCES.search(source)
    if references is None or re.search(r'\\appendix\b|\\begin\{appendices\}', source):
        return False, 'appendix or missing reference boundary needs an independent page proof'
    tail = source[references.end():]
    if references.group().startswith('\\begin'):
        end = re.search(r'\\end\s*\{thebibliography\}', tail)
        if end is None:
            return False, 'manual bibliography has no verifiable end boundary'
        body = tail[:end.start()]
        titles = list(_headings(body))
        if titles:
            return False, f'headings inside claimed reference material: {titles}; page placement unverified'
        # A bibitem label does not establish that arbitrary enclosed prose is
        # bibliographic. Do not silently discard the body or claim a violation.
        return False, 'manual bibliography content needs an independent non-reference page proof'
    # Only structural document termination may follow the bibliography.
    # Arbitrary prose, sections, tables, or additional inputs are not references.
    tail = re.sub(r'\\(?:end\s*\{document\}|bibliographystyle\s*\{[^}]*\}|'
                  r'clearpage\b|newpage\b|par\b|relax\b)', '', tail)
    ok = not tail.strip()
    return ok, ('conventional reference command with no additional source tail' if ok else
                'non-reference or unknown material follows the references')


def content_rule_rows(ed: dict, source: str, *, stage: str = 'submission',
                      track: str = '', pages: int | None = None,
                      page_limit: int | None = None,
                      nonreference_end_page: int | None = None) -> list[dict]:
    """Check expanded TeX against exact pinned content rules.

    Call after compilation with total ``pages`` and effective ``page_limit``.
    ``source`` must be confined, expanded source, without scratch probe macros.
    ``nonreference_end_page`` is optional independent measurement of the LAST
    non-reference material, including appendices and material after references.
    Missing page evidence fails closed for AAAI; no guessed page locations.
    Existing page-limit/total-page checks remain necessary (including 9 total).
    """
    source = re.sub(r'(?<!\\)%[^\n]*', '', source)
    if stage != 'submission':
        return []
    anonymity = ed.get('anonymity') or {}
    if ed.get('id') == 'ijcai-2027' and anonymity.get('double_blind') and track not in (
            anonymity.get('single_blind_tracks') or ()):
        forbidden = any(title.replace(' ', '') == 'contributionstatement'
                        for title in _headings(source))
        return [_row('content.forbidden-sections', not forbidden,
                     'Contribution Statement is forbidden in anonymous IJCAI submissions'
                     if forbidden else 'no forbidden Contribution Statement heading',
                     anonymity.get('source') or {})]
    if ed.get('id') != 'aaai-2027':
        return []
    evidence = (ed.get('exempt_sections') or {}).get('source') or {}
    status = 'pass'
    if pages is None or pages < 1 or page_limit is None or page_limit < 1:
        status = 'unverifiable'
        ok, detail = False, 'cannot verify references-only extra pages without page measurements'
    elif pages <= page_limit:
        ok, detail = True, 'all pages fit the non-reference content limit'
    elif nonreference_end_page is not None:
        ok = 1 <= nonreference_end_page <= page_limit
        status = 'pass' if ok else 'measured_violation'
        detail = (f'last non-reference content ends on page {nonreference_end_page}; '
                  f'content limit {page_limit}; extra pages must contain only references')
    else:
        ok, reason = _reference_only_tail(source)
        status = 'pass' if ok else 'unverifiable'
        detail = ('conventional reference tail; ordinary body/total limits still apply' if ok else
                  'cannot prove extra pages are references-only: ' + reason)
    return [_row('content.references-only-extra-pages', ok, detail, evidence, status=status)]
