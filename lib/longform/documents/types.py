"""Document types: what varies between a paper, a resume and a report.

Responsibility: declare, per type, the required sections (in order), the
reviewer persona and rubric, and which audit stages apply. The stage graph is
the same for every type (``pipeline.py``); a stage that does not apply to a
type records why and passes. Rules live in ``rules.py``.

Entry points: ``DOCUMENT_TYPES``, ``document_type``.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ['DocumentType', 'DOCUMENT_TYPES', 'document_type']


@dataclass(frozen=True)
class DocumentType:
    name: str
    sections: tuple[tuple[str, str], ...]   # (id, default title), in order
    reviewer: str
    rubric: tuple[tuple[str, str], ...]     # (criterion, what it measures)
    needs_venue: bool
    evidence_audit: bool
    novelty_positioning: bool
    body_end_after: str = ''   # section after which the page-limited body ends


PAPER = DocumentType(
    name='paper',
    sections=(('abstract', 'Abstract'), ('introduction', 'Introduction'),
              ('related_work', 'Related Work'), ('method', 'Method'),
              ('experiments', 'Experiments'), ('conclusion', 'Conclusion'),
              ('limitations', 'Limitations')),
    reviewer=('You are an experienced area chair for a top machine-learning '
              'or NLP venue, reviewing independently and strictly.'),
    rubric=(('novelty', 'how new the idea is against the closest prior work'),
            ('clarity', 'how clearly the problem, method and results read'),
            ('story', 'whether the paper builds one convincing argument'),
            ('evidence', 'whether experiments, figures and tables support '
                         'every claim'),
            ('soundness', 'whether the method and evaluation are correct'),
            ('figures', 'whether figures and tables are publication '
                        'quality and informative')),
    needs_venue=True, evidence_audit=True, novelty_positioning=True,
    body_end_after='conclusion',
)

RESUME = DocumentType(
    name='resume',
    sections=(('header', 'Header'), ('education', 'Education'),
              ('projects', 'Projects'), ('experience', 'Experience'),
              ('publications', 'Publications'), ('skills', 'Skills')),
    reviewer=('You are a senior hiring manager for research and research-'
              'engineering roles screening resumes for a strong shortlist.'),
    rubric=(('strength', 'reads like a strong, professional resume'),
            ('distinctiveness', 'what sets this candidate apart is obvious'),
            ('skimmability', 'each line is one crisp insight with its result'),
            ('evidence', 'claims are concrete and verifiable')),
    needs_venue=False, evidence_audit=False, novelty_positioning=False,
)

LONGFORM = DocumentType(
    name='longform',
    sections=(('summary', 'Summary'), ('body', 'Body'),
              ('conclusion', 'Conclusion')),
    reviewer='You are a demanding senior editor reviewing a long-form report.',
    rubric=(('clarity', 'how clearly it reads'),
            ('story', 'whether it builds one argument'),
            ('evidence', 'whether every claim is sourced')),
    needs_venue=False, evidence_audit=True, novelty_positioning=False,
)

DOCUMENT_TYPES = {item.name: item for item in (PAPER, RESUME, LONGFORM)}


def document_type(name: str) -> DocumentType:
    try:
        return DOCUMENT_TYPES[name]
    except KeyError:
        raise ValueError(f'unknown document type {name!r}; one of '
                         f'{sorted(DOCUMENT_TYPES)}') from None
