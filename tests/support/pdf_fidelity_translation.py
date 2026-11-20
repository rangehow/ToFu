"""Synthetic translation pairs for the translation-mode fidelity gate.

The renderer fixtures in ``tests/pdf_parser/test_pdf_render_pipeline.py``
cover the real extract → reflow → render path.  These small pairs isolate one
translation property at a time so a negative control can fail exactly one
gate check: page geometry, a translated block that stays in its column, a
moved figure, a dropped target block and a shifted non-translated block.

Everything is generated locally with the same curated DejaVu face the
renderer uses; no real document, model or network is involved.
"""
from __future__ import annotations

import pymupdf

from lib.pdf_relayout.fonts import FontLibrary

PAGE_WIDTH = 612
PAGE_HEIGHT = 792

_FACE = FontLibrary().face_for_char('M', 'sans', 'regular')
_FONT = pymupdf.Font(fontfile=_FACE.path)
FONT_STACK = {'latin': ['DejaVu Sans']}

TITLE = 'A Translation Layout Fixture'
LEFT = (
    'Left column alpha line one stays short.',
    'Left column beta line two stays short.',
    'Left column gamma line three stays short.',
    'Left column delta line four stays short.',
)
RIGHT = (
    'Right column alpha line one is short.',
    'Right column beta line two is short.',
    'Right column gamma line three is short.',
)
TRANSLATED = (
    'The translated left column is longer than its source and must wrap '
    'inside the same column band without touching the figure or the other '
    'column. It keeps growing for a realistic thirty percent expansion.'
)
FIGURE = (72, 300, 232, 420)
CAPTION = 'Figure 1. A synthetic figure caption.'

VARIANTS = {
    None, 'translated_column', 'figure_shift', 'drop_block',
    'non_translated_shift',
}


def _lines(page, lines, x, y, size=10, leading=12):
    """One text block made of the given lines (TextWriter keeps it whole)."""
    writer = pymupdf.TextWriter(page.rect)
    for line in lines:
        writer.append((x, y), line, font=_FONT, fontsize=size)
        y += leading
    writer.write_text(page)


def _wrap(text: str, width: float, size: float = 10) -> list[str]:
    lines, current = [], ''
    for word in text.split():
        trial = f'{current} {word}'.strip()
        if not current or _FONT.text_length(trial, fontsize=size) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _figure(page, rect):
    page.draw_rect(pymupdf.Rect(*rect), color=(0, 0, 0), width=0.8)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10))
    pixmap.set_rect(pixmap.irect, (30, 90, 160))
    page.insert_image(pymupdf.Rect(*rect), pixmap=pixmap)


def build_source(path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    _lines(page, [TITLE], 72, 80, size=15, leading=18)
    _lines(page, LEFT, 72, 140)
    _lines(page, RIGHT, 332, 140)
    _figure(page, FIGURE)
    _lines(page, [CAPTION], 72, 440, size=9, leading=11)
    document.save(path)
    document.close()


def build_rendered(path, variant: str | None = None) -> None:
    """Write the rendered page; ``variant`` changes exactly one property."""
    if variant not in VARIANTS:
        raise ValueError(f'unknown translation fixture variant: {variant}')
    document = pymupdf.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    _lines(page, [TITLE], 72, 80, size=15, leading=18)

    # The left column is translated and 30%+ longer; every control keeps the
    # other blocks stable except the one it is about.
    if variant == 'translated_column':
        _lines(page, _wrap(TRANSLATED, 228), 332, 200)
    else:
        _lines(page, _wrap(TRANSLATED, 228), 72, 140)

    right_y = 150 if variant == 'non_translated_shift' else 140
    _lines(page, RIGHT, 332, right_y)

    figure = FIGURE
    if variant == 'figure_shift':
        figure = tuple(value + 2 for value in FIGURE)
    _figure(page, figure)
    if variant != 'drop_block':
        _lines(page, [CAPTION], 72, 440, size=9, leading=11)
    document.save(path)
    document.close()


def build_pair(source_path, rendered_path, variant: str | None = None) -> None:
    build_source(source_path)
    build_rendered(rendered_path, variant)