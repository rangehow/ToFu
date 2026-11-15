"""lib/slides/pptx_preview.py — preview single pages of an arbitrary PPTX.

Entry points: ``pptx_geometry`` (page count + page size) and
``render_pptx_page`` (one page → PNG + hyperlink boxes). Both reuse the one
PPTX understanding path (``import_pptx``) and the one renderer
(``render_page_png_with_links``) instead of a second office renderer, so
template import and attachment preview share fidelity fixes.
Dependencies: python-pptx, Playwright Chromium (lazy, per call).
"""

from __future__ import annotations

import os
import tempfile

__all__ = ['pptx_geometry', 'render_pptx_page']


def pptx_geometry(pptx_path: str) -> dict:
    from pptx import Presentation
    from lib.slides.import_pptx import _pt
    prs = Presentation(pptx_path)
    return {'pageCount': len(prs.slides), 'width': _pt(prs.slide_width),
            'height': _pt(prs.slide_height)}


def render_pptx_page(pptx_path: str, page_index: int, out_png: str, *,
                     scale: float = 1.5, abort_check=None) -> dict:
    """Import only page ``page_index`` (0-based) and render it.

    Returns ``{'width', 'height', 'links': [{href, x, y, w, h}]}`` with link
    boxes as page fractions. Raises IndexError for a page outside the deck.
    """
    from lib.slides.import_pptx import import_pptx
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_page_png_with_links
    with tempfile.TemporaryDirectory(prefix='pptx-preview-') as work:
        report = import_pptx(pptx_path, os.path.join(work, 'deck'),
                             slide_numbers=[page_index + 1])
        if report['pages'] != 1:
            raise IndexError(f'page {page_index} is outside the presentation')
        deck = parse_deck(report['manifest'])
        links = render_page_png_with_links(deck, 0, out_png, scale=scale,
                                           abort_check=abort_check)
        return {'width': deck.width, 'height': deck.height, 'links': links}
