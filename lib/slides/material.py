"""Caller-supplied source material as citable evidence units.

Responsibility: turn ``produce_slides`` ``material`` (a report, paper text,
résumé or notes the deck must be grounded in) into an ordered list of evidence
cards ``M1..Mn``. Each card is one verbatim unit of the material: a paragraph,
a list item, or a table row carried with its header row, labelled with its
nearest heading. Splitting is lossless and deterministic (zero LLM): joined
in order, the units contain every non-blank line of the material.

The cards ride the same channel as web research cards (``[S#]``): the outline
cites ``[M#]`` per page, ``recipe._outline_candidate`` attaches the cited
cards to the page brief, and the page author receives them verbatim, so the
exact numbers, conditions and caveats of the source reach every page instead
of only the planner's paraphrase.

Entry points: ``material_cards(text)``, ``format_material_cards(cards)``,
``evidence_numbers(texts)``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

__all__ = ['material_cards', 'format_material_cards', 'evidence_numbers',
           'NUMBER_RE']

_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$')
_LIST_RE = re.compile(r'^\s*(?:[-*+•]|\d+[.)、])\s+')
_TABLE_RE = re.compile(r'^\s*\|.*\|\s*$')
_TABLE_RULE_RE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$')
#: A figure or table caption line ("Figure 5: ...", "Table 1. ...", "图 3：...").
#: Text extracted from a PDF has no blank lines, so without this a caption
#: and the paragraphs around it merge into one unit and the outline cannot
#: cite a figure on its own.
_CAPTION_RE = re.compile(
    r'^\s*(?:Figure|Fig\.|Table|图|表)\s*\d+[a-z]?\s*[:：.]', re.IGNORECASE)
#: Numbers as written in sources: 17,303 · 2.8 · .5 · 95.4% · −20.8 · 1e-3.
NUMBER_RE = re.compile(
    r'(?<![A-Za-z0-9_.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
    r'|(?<![A-Za-z0-9_])\.\d+')


def material_cards(text: str) -> list[dict]:
    """Split material into verbatim evidence units with nearest headings."""
    cards: list[dict] = []
    heading = ''
    paragraph: list[str] = []
    table_header = ''

    def emit(unit: str) -> None:
        unit = unit.strip()
        if unit:
            cards.append({'id': f'M{len(cards) + 1}', 'kind': 'material',
                          'section': heading, 'point': unit})

    def flush() -> None:
        if paragraph:
            emit(' '.join(line.strip() for line in paragraph))
            paragraph.clear()

    for line in (text or '').splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(1).strip()
            table_header = ''
            continue
        if not line.strip():
            flush()
            table_header = ''
            continue
        if _TABLE_RE.match(line):
            flush()
            if _TABLE_RULE_RE.match(line):
                continue
            if not table_header:
                table_header = line.strip()
                emit(table_header)
            else:
                emit(f'{table_header}\n{line.strip()}')
            continue
        table_header = ''
        if _LIST_RE.match(line) or _CAPTION_RE.match(line):
            flush()
            paragraph.append(line)
            continue
        paragraph.append(line)
    flush()
    return cards


def format_material_cards(cards: list[dict]) -> str:
    """Outline-prompt listing: one ``[M#] (§ heading) text`` line per unit."""
    lines = []
    for card in cards:
        section = f' (§ {card["section"]})' if card.get('section') else ''
        lines.append(f'[{card["id"]}]{section} {card["point"]}')
    return '\n'.join(lines)


def evidence_numbers(texts) -> set:
    """Every number written in ``texts`` as an exact Decimal."""
    out = set()
    for text in texts:
        for token in NUMBER_RE.findall(str(text or '')):
            try:
                out.add(Decimal(token.replace(',', '')))
            except InvalidOperation:
                continue
    return out
