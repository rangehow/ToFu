"""Source coverage: the headline results and figures a deck must carry.

Responsibility: before any page is authored, establish what the source's
key results are and check that the outline carries each of them with its
numbers. Two inventories feed one check:

- ``figure_index(cards)`` (zero LLM): every figure or table the material
  captions, with the evidence units that state its measured values (units
  that name it and carry decimals or percentages, wherever they sit: the
  results text, a caption, an appendix table).
- the headline inventory: one model call lists the results the source
  presents as its main findings, each with the ``[M#]`` units and the
  numbers that carry it. ``parse_inventory`` keeps only units that exist
  and numbers written in those units, so the inventory cannot introduce a
  value the source does not state.

``assign_coverage(coverage, pages)`` maps each inventory entry to the pages
that carry it and lists the gaps: a headline result no page carries with
its numbers, or a page that presents a figure without any of its values.
The outline stage fills the gaps before it hands over; the author gate
(``lib/slides/author.py``) then requires every assigned entry's numbers
on its page, so a figure described only in words is caught while the page
is still being written.

Entry points: ``figure_index``, ``inventory_prompt``, ``parse_inventory``,
``assign_coverage``, ``shows_entry``.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

from lib.slides.material import evidence_numbers

__all__ = ['figure_index', 'inventory_prompt', 'parse_inventory',
           'assign_coverage', 'shows_entry', 'measured_numbers']

_CAPTION_RE = re.compile(
    r'^\s*(Figure|Fig\.|Table|图|表)\s*(\d+)[a-z]?\s*[:：.]', re.IGNORECASE)
#: "Fig- ure 7" is a PDF line break inside the word, joined by the splitter.
_REFERENCE_RE = re.compile(
    r'\b(Fig(?:ure|-\s+ure|s?\.)|Table)\s*(\d+)|(图|表)\s*(\d+)', re.IGNORECASE)
#: Measured values: a percentage or a number with a fractional part. Plain
#: integers in a figure's text are mostly sizes, years, counts and section
#: numbers, which do not show a figure's result; a number joined to a name
#: by a hyphen is a version ("GPT-3.5", "Claude-1.3").
_MEASURE_RE = re.compile(
    r'(?<![\w.§-])(\d+(?:\.\d+)?)\s?[%％]'
    r'|(?<![\w.§,-])(\d+\.\d+)(?![\w.]*\d)')
_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def _reference_key(kind: str, number: str) -> str:
    family = 'Table' if kind.lower() in ('table', '表') else 'Figure'
    return f'{family} {int(number)}'


def _references(text: str) -> set[str]:
    out = set()
    for match in _REFERENCE_RE.finditer(text or ''):
        kind, number = ((match.group(1), match.group(2)) if match.group(1)
                        else (match.group(3), match.group(4)))
        out.add(_reference_key(kind, number))
    return out


def measured_numbers(text: str) -> set:
    """Percentages and fractional numbers written in ``text``, as Decimals."""
    out = set()
    for match in _MEASURE_RE.finditer(text or ''):
        out.add(Decimal(match.group(1) or match.group(2)))
    return out


def figure_index(cards: list[dict]) -> dict:
    """``{'Figure 5': {'caption': 'M12', 'units': [...], 'numbers': [...]}}``.

    Only captioned figures and tables enter the index (a bare "表 3" inside
    a Chinese word is not a figure); only those with measured values in the
    material are kept, because a figure without stated values has nothing
    a page could be required to show.
    """
    captions = {}
    for card in cards:
        match = _CAPTION_RE.match(str(card.get('point') or ''))
        if match:
            captions.setdefault(
                _reference_key(match.group(1), match.group(2)), card['id'])
    index = {}
    for card in cards:
        point = str(card.get('point') or '')
        values = measured_numbers(point)
        if not values:
            continue
        for key in _references(point) & set(captions):
            entry = index.setdefault(
                key, {'caption': captions[key], 'units': [], 'numbers': set()})
            entry['units'].append(card['id'])
            entry['numbers'] |= values
    return {key: {'caption': entry['caption'], 'units': entry['units'],
                  'numbers': sorted(str(n) for n in entry['numbers'])}
            for key, entry in sorted(index.items())}


def content_page_capacity(max_pages: int) -> int:
    """Content pages in a deck of ``max_pages``: the outline contract opens
    with a cover and closes with a final page, and plans one claim per
    content page, so this many results can each have a page."""
    return max(1, int(max_pages) - 2)


def inventory_prompt(material_listing: str, figures: dict, *, lang: str,
                     capacity: int) -> str:
    """One call: the source's headline results with their units and numbers,
    no more than the deck has content pages for."""
    figure_doc = '\n'.join(
        f'- {key}: caption [{entry["caption"]}], values in '
        + ', '.join(f'[{unit}]' for unit in entry['units'])
        for key, entry in figures.items()) or '- (none captioned)'
    language = 'Chinese' if lang == 'zh' else 'English'
    return (
        'You are preparing a talk from the source below; it has '
        f'{capacity} content pages, one claim each. List the source\'s '
        f'headline results, at most {capacity}, most important first: the '
        'findings the source itself presents as its main results or '
        'contributions (abstract, introduction, results, conclusion), '
        'covering each main experiment or task it reports, so that a deck '
        'missing any of them would misrepresent the source. Merge related '
        'findings into one result rather than listing details separately. '
        'Do not list background, related work or setup details.\n'
        'For each result give: "claim" (one sentence in ' + language + '), '
        '"units" (the [M#] ids whose text states its numbers), "figures" '
        '(the figure/table names it is shown in, e.g. "Figure 7", "Table 1", '
        'or []), and "numbers" (its decisive values copied exactly as '
        'written in those units, without units of measure; [] for a purely '
        'qualitative result).\n'
        'Output ONLY JSON: {"results": [{"claim": "...", "units": ["M3"], '
        '"figures": ["Figure 5"], "numbers": ["56.1"]}]}\n\n'
        '## Figures and tables with stated values\n' + figure_doc + '\n\n'
        '## Source, as evidence units\n' + material_listing + '\n')


def parse_inventory(content: str, cards: list[dict], figures: dict, *,
                    capacity: int) -> list[dict]:
    """Validated headline entries ``H1..Hn``; unknown units and values dropped.

    A number is kept only if it is written in one of the entry's units (its
    own, or the value units of a figure it names), so the inventory holds
    the source's values exactly and nothing else.
    """
    match = _JSON_RE.search(content or '')
    if not match:
        raise ValueError('coverage inventory reply has no JSON object')
    raw = json.loads(match.group(0)).get('results')
    if not isinstance(raw, list):
        raise ValueError('coverage inventory reply has no results list')
    by_id = {card['id']: card for card in cards}
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        claim = re.sub(r'\s+', ' ', str(item.get('claim') or '')).strip()
        named = (_references(' '.join(str(f) for f in item.get('figures') or []))
                 | _references(claim))
        figure_keys = sorted(key for key in named if key in figures)
        units = [str(unit) for unit in item.get('units') or []
                 if str(unit) in by_id]
        for key in figure_keys:
            units.extend(figures[key]['units'])
        units = list(dict.fromkeys(units))
        if not claim or not units:
            continue
        stated = evidence_numbers(by_id[unit]['point'] for unit in units)
        numbers = []
        for value in item.get('numbers') or []:
            found = evidence_numbers([str(value)])
            if len(found) == 1 and next(iter(found)) in stated:
                numbers.append(str(next(iter(found))))
        entries.append({'id': f'H{len(entries) + 1}', 'claim': claim,
                        'units': units, 'figures': figure_keys,
                        'numbers': list(dict.fromkeys(numbers))})
        if len(entries) == capacity:
            break
    return entries


def _extractor(kind: str):
    """Figures count measured values; headline results count any number."""
    return measured_numbers_in if kind == 'figure' else evidence_numbers


def measured_numbers_in(texts) -> set:
    out = set()
    for text in texts:
        out |= measured_numbers(str(text or ''))
    return out


def shows_entry(entry: dict, texts) -> bool:
    """Whether reader-visible ``texts`` show one of an entry's numbers."""
    wanted = {Decimal(number) for number in entry.get('numbers') or []}
    return not wanted or bool(wanted & _extractor(entry.get('kind'))(texts))


def _carries(page: dict, units, numbers, kind: str) -> bool:
    """The brief cites one of the entry's own units and states one of its
    numbers written there. Matching a number against any cited material
    would let a page about one result "carry" every result that shares a
    small integer with it."""
    own = [source for source in page.get('sources') or []
           if isinstance(source, dict) and source.get('kind') == 'material'
           and source.get('id') in set(units)]
    if not own:
        return False
    if not numbers:
        return True
    extract = _extractor(kind)
    stated = extract([page.get('key_message'), page.get('content_notes')])
    cited = extract(source.get('point') for source in own)
    wanted = {Decimal(number) for number in numbers}
    return bool(wanted & stated & cited)


def assign_coverage(coverage: dict | None, pages: list[dict]
                    ) -> tuple[list[list[dict]], list[str]]:
    """Per page: the entries it carries; and the gaps no page closes.

    An entry is carried by a page whose brief cites one of the entry's units
    and states one of its numbers written there (a qualitative entry: cites
    one of its units). Each result is assigned to one carrier, the page
    presenting it: a content page over a closing summary, then the page
    stating most of its numbers, then the first. A closing page that cites
    every result is not held to showing all of them. A page whose brief
    names a figure with stated values must carry one of them: that is the
    page presenting the figure.
    """
    coverage = coverage or {}
    figures = coverage.get('figures') or {}
    assigned: list[list[dict]] = [[] for _ in pages]
    gaps: list[str] = []
    for entry in coverage.get('inventory') or []:
        carriers = [index for index, page in enumerate(pages)
                    if _carries(page, entry['units'], entry['numbers'],
                                'result')]
        if carriers:
            wanted = {Decimal(number) for number in entry['numbers']}
            primary = max(carriers, key=lambda index: (
                pages[index].get('pageType', 'content') == 'content',
                len(wanted & evidence_numbers(
                    [pages[index].get('key_message'),
                     pages[index].get('content_notes')])),
                -index))
            assigned[primary].append({
                'kind': 'result', 'label': entry['claim'],
                'numbers': entry['numbers']})
        else:
            units = ', '.join(f'[{unit}]' for unit in entry['units'])
            values = (' with its numbers (' + ', '.join(entry['numbers']) + ')'
                      if entry['numbers'] else '')
            gaps.append(
                f'headline result [{entry["id"]}] is on no page: "{entry["claim"]}". '
                f'Put it on a page{values}: cite {units} and write the '
                "values in that page's content_notes.")
    for index, page in enumerate(pages):
        text = ' '.join(str(page.get(field) or '') for field in
                        ('purpose', 'key_message', 'content_notes'))
        for key in sorted(_references(text) & set(figures)):
            entry = figures[key]
            if _carries(page, entry['units'], entry['numbers'], 'figure'):
                assigned[index].append({'kind': 'figure', 'label': key,
                                        'numbers': entry['numbers']})
                continue
            units = ', '.join(f'[{unit}]' for unit in entry['units'])
            gaps.append(
                f'page {index + 1} presents {key} without its values: cite '
                f'{units} and write the values it shows (as written there) '
                'in content_notes, so the page can plot or tabulate them '
                'instead of describing the figure in words.')
    return assigned, gaps
