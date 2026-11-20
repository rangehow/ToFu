"""Synthetic born-digital PDF pairs for the reproduction fidelity gate.

Every pair is generated locally with PyMuPDF's built-in fonts; nothing here
reads a real document, the network or a model. ``variant`` changes exactly one
structural property of the rendered side so a negative control can fail one
gate check and leave the others green.
"""
from __future__ import annotations

import pymupdf

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
TITLE = 'A Fidelity Gate for Born Digital Papers'
LEFT = ('Left column AA block text.', 'Left column BB block text.',
        'Left column CC block text.')
RIGHT = ('Right column DD block text.', 'Right column EE block text.',
         'Right column FF block text.')
CJK = '中文段落示例。'
FONT_STACK = {'latin': ['Helvetica'], 'cjk': ['Heiti']}

VARIANTS = {
    'page_count', 'page_size', 'columns', 'reading_order', 'bbox_shift',
    'figure_table_shift', 'drop_text', 'font', 'cjk_font', 'overlap',
}


def _blank_pixmap():
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10))
    pixmap.set_rect(pixmap.irect, (210, 60, 60))
    return pixmap


def _table(page, rect, shift: float = 0) -> None:
    x0, y0, x1, y1 = (value + shift for value in rect)
    for index in range(3):
        y = y0 + index * (y1 - y0) / 2
        page.draw_line((x0, y), (x1, y), color=(0, 0, 0), width=0.7)
    for index in range(3):
        x = x0 + index * (x1 - x0) / 2
        page.draw_line((x, y0), (x, y1), color=(0, 0, 0), width=0.7)
    for text, column, row in (('AA', 0, 0), ('BB', 1, 0), ('CC', 0, 1), ('DD', 1, 1)):
        x = x0 + (column + 0.5) * (x1 - x0) / 2
        y = y0 + (row + 0.7) * (y1 - y0) / 2
        page.insert_text((x, y), text, fontsize=9, fontname='helv')


def build(path, variant: str | None = None) -> None:
    """Write the synthetic PDF to ``path`` with at most one altered property."""
    if variant not in VARIANTS and variant is not None:
        raise ValueError(f'unknown fidelity fixture variant: {variant}')
    document = pymupdf.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_text((72, 80), TITLE, fontsize=16, fontname='helv')

    left = list(LEFT)
    if variant == 'reading_order':
        left[0], left[1] = left[1], left[0]
    for index, text in enumerate(left):
        if variant == 'drop_text' and index == 1:
            continue
        y = 120 + 30 * index + (8 if variant == 'bbox_shift' and index == 2 else 0)
        font = 'tiro' if variant == 'font' and index == 0 else 'helv'
        page.insert_text((72, y), text, fontsize=10, fontname=font)
    for index, text in enumerate(RIGHT):
        page.insert_text((380, 120 + 30 * index), text, fontsize=10, fontname='helv')

    cjk_font = 'japan' if variant == 'cjk_font' else 'china-s'
    page.insert_text((72, 220), CJK, fontsize=12, fontname=cjk_font)

    shift = 2 if variant == 'figure_table_shift' else 0
    page.insert_image(pymupdf.Rect(72 + shift, 260 + shift, 232 + shift, 380 + shift),
                      pixmap=_blank_pixmap())
    _table(page, (380, 260, 560, 360), shift)
    if variant == 'columns':
        page.insert_text((260, 130), 'Gutter', fontsize=10, fontname='helv')
    if variant == 'overlap':
        page.insert_text((100, 300), 'OVERLAY', fontsize=10, fontname='helv')

    second = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    second.insert_text((72, 80), 'Second Page', fontsize=16, fontname='helv')
    second.insert_text((72, 120), 'One column second page body text.',
                       fontsize=10, fontname='helv')
    second.insert_text((72, 150), 'Another one column body line here.',
                       fontsize=10, fontname='helv')
    second.insert_text((72, 200), CJK, fontsize=12, fontname='china-s')

    if variant == 'page_count':
        document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    if variant == 'page_size':
        for page in document:
            box = page.mediabox
            page.set_mediabox(pymupdf.Rect(0, 0, box.width + 3, box.height))
    document.save(path)
    document.close()


def build_pair(source, rendered) -> None:
    build(source)
    build(rendered)