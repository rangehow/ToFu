"""Length-compensation stress specification for the PDF translation pipeline.

Five synthetic born-digital layouts are generated here with PyMuPDF; no user
PDF, download, model call, network or OCR path is involved.  Deterministic fake
translators stretch or shrink each block's source text by 0.6x, 1.0x, 1.5x and
2.2x while keeping word boundaries intact.

The measurements are taken from the *rendered* document, not from the solver's
own plan, so a plan/render disagreement cannot hide a defect:

* page overflow — a rendered text block outside the page rectangle;
* column overflow — a body text block outside its own column measure;
* body overlap — two rendered text block rectangles intersecting by more than
  the measurement tolerance (0.5 pt on each axis: PyMuPDF text-block rounding
  and glyph side bearings, not a layout allowance);
* dropped — a translated block whose text does not appear in the rendered page;
* fill ratio — rendered text-block area divided by page area.

Thresholds follow from the geometry rather than a tuning knob: the page box and
the solver's column boxes are hard bounds, and two body blocks cannot both own
the same points.  A block that genuinely cannot fit must be refused explicitly
by the pipeline (source text retained), never painted over its neighbour.
"""
from __future__ import annotations

import re

import pymupdf
import pytest

from lib.paper.builds.pdf_translation import (
    DEFAULT_DECLARED_FONTS,
    translate_pdf,
)
from lib.pdf_parser.layout import page_layout_from_structure
from lib.pdf_parser.structure import extract_pdf_structure
from lib.pdf_relayout.fonts import FontLibrary

pytestmark = pytest.mark.unit

_SOURCE_RE = re.compile(r'<source_text>\n(.*)\n</source_text>', re.DOTALL)
_FONTS = FontLibrary()
#: Block rectangles closer than this are extraction rounding, not an overlap.
_OVERLAP_TOLERANCE_PT = 0.5
#: A block this close to the page or column edge is inside it.
_BOX_TOLERANCE_PT = 0.5
#: A rendered text block wider than this fraction of the page is a spanning
#: title/heading, not body text in one column.
_SPANNING_FRACTION = 0.60

#: The owner's stretch ladder.  2.2x is the largest compensated case.
RATIOS = (0.6, 1.0, 1.5, 2.2)


# ── fixtures ─────────────────────────────────────────────────────────────


def _write_lines(page, lines, x, y, size, leading):
    face = _FONTS.face_for_char('M', 'sans', 'regular')
    font = pymupdf.Font(fontfile=face.path)
    writer = pymupdf.TextWriter(page.rect)
    for line in lines:
        writer.append((x, y), line, font=font, fontsize=size)
        y += leading
    writer.write_text(page)


def _paragraph(tag: str, count: int, *, short: bool = False) -> list[str]:
    """Distinct source lines for one block.

    Source blocks must stay separate in extraction and must not share text with
    another block, otherwise the gate's one-to-one manifest match (not the
    geometry under test) decides the result.
    """
    if short:
        return [f'{tag} column line {index} stays short and clear.'
                for index in range(count)]
    return [f'{tag} paragraph line {index} carries ordinary words for the reflow test now.'
            for index in range(count)]


def _single_column_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['A Synthetic Single Column Document'], 72, 80, 16, 20)
    _write_lines(page, _paragraph('a1', 12), 72, 120, 10, 12)
    _write_lines(page, _paragraph('b1', 8), 72, 276, 10, 12)
    _write_lines(page, _paragraph('c1', 8), 72, 384, 10, 12)
    _write_lines(page, _paragraph('d1', 6), 72, 492, 10, 12)
    _write_lines(page, _paragraph('e1', 4), 72, 564, 10, 12)
    payload = document.tobytes()
    document.close()
    return payload


def _two_column_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(
        page,
        ['Two Column Synthetic Layout With A Full Width Title Spanning'],
        50, 70, 15, 18,
    )
    _write_lines(page, _paragraph('left alpha', 8, short=True), 50, 120, 10, 12)
    _write_lines(page, _paragraph('left beta', 6, short=True), 50, 226, 10, 12)
    _write_lines(page, _paragraph('left gamma', 4, short=True), 50, 310, 10, 12)
    _write_lines(page, _paragraph('right alpha', 8, short=True), 312, 120, 10, 12)
    _write_lines(page, _paragraph('right beta', 6, short=True), 312, 226, 10, 12)
    _write_lines(page, _paragraph('right gamma', 4, short=True), 312, 310, 10, 12)
    payload = document.tobytes()
    document.close()
    return payload


def _table_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['A Table Bearing Synthetic Page'], 72, 80, 16, 20)
    _write_lines(page, _paragraph('t1', 6), 72, 120, 10, 12)
    x0, y0, x1, y1 = 72, 200, 380, 340
    for index in range(4):
        page.draw_line((x0, y0 + index * (y1 - y0) / 3),
                       (x1, y0 + index * (y1 - y0) / 3),
                       color=(0, 0, 0), width=0.8)
        page.draw_line((x0 + index * (x1 - x0) / 3, y0),
                       (x0 + index * (x1 - x0) / 3, y1),
                       color=(0, 0, 0), width=0.8)
    for row in range(3):
        for column in range(3):
            page.insert_text(
                (x0 + column * (x1 - x0) / 3 + 8,
                 y0 + row * (y1 - y0) / 3 + 18),
                f'{row * 3 + column}.{row + 1}', fontsize=9, fontname='helv')
    _write_lines(page, ['Table 1. A ruled synthetic table with numeric cells.'],
                 72, 358, 9, 11)
    _write_lines(page, _paragraph('t2', 6), 72, 382, 10, 12)
    _write_lines(page, _paragraph('t3', 5), 72, 462, 10, 12)
    payload = document.tobytes()
    document.close()
    return payload


def _figure_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['A Vector Figure Synthetic Page'], 72, 80, 16, 20)
    _write_lines(page, _paragraph('f1', 6), 72, 120, 10, 12)
    # Vector-only figure: stroked path, no raster image.
    page.draw_rect(pymupdf.Rect(72, 200, 320, 370), color=(0, 0, 0), width=1.2)
    page.draw_line((80, 360), (150, 250), color=(0, 0, 0), width=2.0)
    page.draw_line((150, 250), (230, 310), color=(0, 0, 0), width=2.0)
    page.draw_circle((265, 260), 30, color=(0, 0, 0), width=2.0)
    _write_lines(page, ['Figure 1. A synthetic vector figure with a caption below.'],
                 72, 388, 9, 11)
    _write_lines(page, _paragraph('f2', 6), 72, 412, 10, 12)
    _write_lines(page, _paragraph('f3', 5), 72, 492, 10, 12)
    payload = document.tobytes()
    document.close()
    return payload


def _footnote_source() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    _write_lines(page, ['Synthetic Journal Header 2026'], 72, 34, 8, 10)
    _write_lines(page, ['A Footnote Bearing Synthetic Page'], 72, 80, 16, 20)
    _write_lines(page, _paragraph('n1', 12), 72, 120, 10, 12)
    _write_lines(page, _paragraph('n2', 8), 72, 276, 10, 12)
    _write_lines(page, _paragraph('n3', 8), 72, 384, 10, 12)
    _write_lines(page, _paragraph('n4', 4), 72, 492, 10, 12)
    page.draw_line((72, 650), (300, 650), color=(0, 0, 0), width=0.6)
    _write_lines(page, [
        '1 This is the first footnote with a few ordinary words.',
        '2 This is the second footnote with a few more words.',
        '3 This is the third footnote closing the fixture page.',
    ], 72, 664, 8, 10)
    _write_lines(page, ['Page 1'], 300, 770, 8, 10)
    payload = document.tobytes()
    document.close()
    return payload


FIXTURES = {
    'single_column': _single_column_source,
    'two_column': _two_column_source,
    'table': _table_source,
    'figure': _figure_source,
    'footnotes': _footnote_source,
}


# ── deterministic fake translators ───────────────────────────────────────


def stretch(text: str, ratio: float) -> str:
    """Repeat or truncate whole words to reach ``ratio`` of the source length.

    Word boundaries are preserved and no content is invented: shrinking keeps
    a prefix of the source words, stretching cycles the same words.
    """
    tokens = text.split()
    if not tokens:
        return text
    source_length = len(' '.join(tokens))
    target = max(1, int(round(source_length * ratio)))
    if ratio <= 1.0:
        kept: list[str] = []
        used = 0
        for token in tokens:
            addition = len(token) + (1 if kept else 0)
            if kept and used + addition > target:
                break
            kept.append(token)
            used += addition
        return ' '.join(kept) if kept else tokens[0]
    grown: list[str] = []
    used = 0
    index = 0
    while used < target:
        token = tokens[index % len(tokens)]
        grown.append(token)
        used += len(token) + (1 if len(grown) > 1 else 0)
        index += 1
    return ' '.join(grown)


def _source_of(prompt: str) -> str:
    match = _SOURCE_RE.search(prompt)
    assert match is not None, prompt
    return match.group(1)


def _translator(ratio: float):
    return lambda prompt: stretch(_source_of(prompt), ratio)


# ── measurement ──────────────────────────────────────────────────────────


def _normal(text: str) -> str:
    return ' '.join(text.split())


def _text_blocks(pdf_bytes: bytes) -> list[dict]:
    document = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    try:
        blocks = []
        for raw in document[0].get_text('dict')['blocks']:
            if raw.get('type') != 0:
                continue
            text = ' '.join(
                ''.join(span['text'] for span in line.get('spans', []))
                for line in raw.get('lines', [])
            )
            if text.strip():
                blocks.append({'bbox': tuple(float(v) for v in raw['bbox']),
                               'text': text})
        return blocks
    finally:
        document.close()


def _intersects(left, right) -> bool:
    return (
        min(left[2], right[2]) - max(left[0], right[0]) > _OVERLAP_TOLERANCE_PT
        and min(left[3], right[3]) - max(left[1], right[1]) > _OVERLAP_TOLERANCE_PT
    )


def _inside(rect, container, tolerance=_BOX_TOLERANCE_PT) -> bool:
    return (
        rect[0] >= container[0] - tolerance
        and rect[1] >= container[1] - tolerance
        and rect[2] <= container[2] + tolerance
        and rect[3] <= container[3] + tolerance
    )


def _centre_inside(rect, container) -> bool:
    cx = (rect[0] + rect[2]) / 2
    cy = (rect[1] + rect[3]) / 2
    return container[0] <= cx <= container[2] and container[1] <= cy <= container[3]


def _band_for(rect, columns):
    cx = (rect[0] + rect[2]) / 2
    return min(
        columns,
        key=lambda column: 0.0 if column.bbox.x0 <= cx <= column.bbox.x1
        else min(abs(cx - column.bbox.x0), abs(cx - column.bbox.x1)),
    )


def measure(result, source_pdf: bytes, ratio: float) -> dict:
    """Measure overflow, overlap, dropped text and fill on the rendered page."""
    structure = extract_pdf_structure(source_pdf)
    page = structure['pages'][0]
    adapted = page_layout_from_structure(page)
    columns = adapted.layout.columns
    # Fixed content and preserved source text (running heads/folios, a
    # full-width title) are not body blocks and may span columns.
    exempt = [
        (block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1)
        for block in adapted.layout.blocks if block.is_fixed
    ] + [
        (block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1)
        for block in adapted.preserved
    ]
    rendered = _text_blocks(result.pdf_bytes)
    page_box = (0.0, 0.0, float(page['width']), float(page['height']))

    page_overflow = 0
    column_overflow = 0
    for block in rendered:
        if not _inside(block['bbox'], page_box):
            page_overflow += 1
        if any(_centre_inside(block['bbox'], rect) for rect in exempt):
            continue
        width = block['bbox'][2] - block['bbox'][0]
        if width > _SPANNING_FRACTION * page['width']:
            continue
        column = _band_for(block['bbox'], columns)
        if not _inside(block['bbox'], (column.bbox.x0, column.bbox.y0,
                                       column.bbox.x1, column.bbox.y1)):
            column_overflow += 1

    overlap = 0
    for index, block in enumerate(rendered):
        for other in rendered[index + 1:]:
            if _intersects(block['bbox'], other['bbox']):
                overlap += 1

    rendered_text = _normal(' '.join(block['text'] for block in rendered))
    dropped = 0
    for outcome in result.outcomes:
        if outcome.status != 'translated':
            continue
        source_block = next(
            (block for block in page['blocks']
             if f"p{page['page_number']}-b{block.get('index')}" == outcome.block_id),
            None,
        )
        if source_block is None:
            continue
        expected = _normal(stretch(source_block.get('text') or '', ratio))
        if expected and expected not in rendered_text:
            dropped += 1

    ink = sum(
        max(0.0, block['bbox'][2] - block['bbox'][0])
        * max(0.0, block['bbox'][3] - block['bbox'][1])
        for block in rendered
    )
    fill = ink / (page['width'] * page['height'])
    return {
        'page_overflow': page_overflow,
        'column_overflow': column_overflow,
        'overlap': overlap,
        'dropped': dropped,
        'fill': fill,
        'refused': result.summary['refused'],
    }


def _measure_run(source_pdf: bytes, ratio: float):
    return translate_pdf(
        source_pdf, 'zh', translate=_translator(ratio),
        declared_fonts=DEFAULT_DECLARED_FONTS,
    )


# ── specification ────────────────────────────────────────────────────────


@pytest.mark.parametrize('fixture', sorted(FIXTURES))
def test_length_stretch_never_overflows_or_overlaps(fixture, capsys):
    source_pdf = FIXTURES[fixture]()
    rows = []
    for ratio in RATIOS:
        result = _measure_run(source_pdf, ratio)
        assert result.pdf_bytes is not None
        assert result.ok is True, (fixture, ratio, result.failures)
        measured = measure(result, source_pdf, ratio)
        rows.append((ratio, measured))

        assert measured['page_overflow'] == 0, (fixture, ratio, measured)
        assert measured['column_overflow'] == 0, (fixture, ratio, measured)
        assert measured['overlap'] == 0, (fixture, ratio, measured)
        assert measured['dropped'] == 0, (fixture, ratio, measured)

    with capsys.disabled():
        print(f'[{fixture}]')
        print('  stretch  page_ovf  column_ovf  overlap  dropped  refused  fill')
        for ratio, measured in rows:
            print(f"  {ratio:>5.1f}x  {measured['page_overflow']:>8}  "
                  f"{measured['column_overflow']:>10}  {measured['overlap']:>7}  "
                  f"{measured['dropped']:>7}  {measured['refused']:>7}  "
                  f"{measured['fill']:.3f}")


@pytest.mark.parametrize('fixture', ['single_column', 'footnotes'])
def test_largest_stretch_reflows_down_the_column_instead_of_refusing(fixture):
    """2.2x fits by moving later blocks; before the reflow it was refused.

    The page has far more readable-floor height than the stretched text needs,
    so a refusal here means the solver capped each block at the next block's
    *original* top instead of using the column's remaining box.
    """
    source_pdf = FIXTURES[fixture]()
    result = _measure_run(source_pdf, 2.2)

    assert result.summary['refused'] == 0, result.outcomes
    assert result.ok is True, result.failures

    structure = extract_pdf_structure(source_pdf)
    page = structure['pages'][0]
    adapted = page_layout_from_structure(page)
    rendered = _text_blocks(result.pdf_bytes)
    # At least one later block must be pushed down: that is the length
    # compensation the 2.2x translation demands.
    moved = 0
    for block in adapted.layout.blocks:
        if block.is_fixed or block.kind == 'heading':
            continue
        expected = _normal(stretch(block.source_text, 2.2))
        candidate = next(
            (item for item in rendered if expected in _normal(item['text'])),
            None,
        )
        if candidate is not None and candidate['bbox'][1] > block.bbox.y0 + 1.0:
            moved += 1
    assert moved >= 1, 'no block was moved down the column to absorb 2.2x text'


def test_unsatisfiable_stretch_is_an_explicit_refusal_not_an_overflow():
    """6x cannot fit the readable-floor budget; the pipeline must say so."""
    source_pdf = _single_column_source()
    result = _measure_run(source_pdf, 6.0)

    assert result.summary['refused'] >= 1, result.outcomes
    assert result.ok is True, result.failures
    measured = measure(result, source_pdf, 6.0)
    assert measured['page_overflow'] == 0
    assert measured['column_overflow'] == 0
    assert measured['overlap'] == 0
    assert measured['dropped'] == 0