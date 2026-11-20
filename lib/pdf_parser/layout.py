"""Bridge from extracted PDF structure to the re-layout input model.

``extract_pdf_structure`` (``lib.pdf_parser.structure``) reports lines, spans
and typed blocks in PyMuPDF page space.  The re-layout solver
(``lib.pdf_relayout``) consumes :class:`~lib.pdf_relayout.model.PageLayout`.
This module performs that pure mapping: no PDF is opened here, no model is
called, and no pixels are produced.

Blocks the layout model cannot represent are *preserved* rather than dropped:
running heads/folios, and full-width flowable blocks on a multi-column page
(the model has no column-spanning flowable).  The renderer copies their source
text unchanged; ``AdaptedPage.preserved`` says which blocks and why.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from lib.pdf_relayout import (
    Block,
    Column,
    FontSpec,
    LayoutInputError,
    PageLayout,
    Rect,
)

#: Extraction kind -> re-layout flowable kind.  ``title`` and ``heading`` are
#: both headings for placement purposes; paragraphs are plain text.
_FLOWABLE_KINDS = {
    'paragraph': 'text',
    'list': 'list',
    'caption': 'caption',
    'heading': 'heading',
    'title': 'heading',
}
_FIXED_KINDS = {'figure': 'figure', 'table': 'table'}
_PRESERVED_KINDS = {'header', 'footer'}

_CAPTION_ATTACH_GAP_PT = 48.0

#: Right side bearing allowance as a fraction of the em.  The curated faces
#: (DejaVu, Lato, Noto Sans SC) stay well below this bound.
_INK_SLACK_RATIO = 0.05


@dataclass(frozen=True, slots=True)
class PreservedBlock:
    """One source block the layout model does not own."""

    block_id: str
    kind: str
    bbox: Rect
    reason: str


@dataclass(frozen=True, slots=True)
class AdaptedPage:
    """A page layout plus the extraction blocks deliberately left in place."""

    layout: PageLayout
    preserved: tuple[PreservedBlock, ...]

    def translated_block_ids(self) -> tuple[str, ...]:
        return tuple(
            block.block_id for block in self.layout.blocks if not block.is_fixed
        )


def page_layout_from_structure(page: dict) -> AdaptedPage:
    """Map one ``extract_pdf_structure`` page to a solver input.

    Raises:
        LayoutInputError: the extracted page has no usable page size, columns
            or consistent block geometry.
    """
    if not page.get('supported', False):
        raise LayoutInputError(
            f"page {page.get('page_number')!r} has no usable text layer: "
            f"{page.get('unsupported_reason')!r}"
        )
    try:
        width = float(page['width'])
        height = float(page['height'])
    except (KeyError, TypeError, ValueError) as exc:
        raise LayoutInputError(f'page has no usable size: {page!r}') from exc
    if width <= 0 or height <= 0:
        raise LayoutInputError(f'page has non-positive size: {width}x{height}')

    page_number = int(page.get('page_number', 0))
    page_rect = Rect(0.0, 0.0, width, height)
    raw_blocks = tuple(page.get('blocks') or ())

    content_top, content_bottom = _content_bounds(raw_blocks, height)
    columns = _columns(page.get('columns') or (), page_rect, raw_blocks,
                       content_top, content_bottom, page_number)
    column_ids = {column.column_id for column in columns}

    blocks: list[Block] = []
    preserved: list[PreservedBlock] = []
    for raw in raw_blocks:
        block = _block(raw, page_number, page_rect, columns, column_ids,
                       content_top, content_bottom, preserved)
        if block is not None:
            blocks.append(block)

    _link_captions(blocks)
    layout = PageLayout(
        page_id=f'page-{page_number}',
        page=page_rect,
        columns=columns,
        blocks=tuple(blocks),
    )
    return AdaptedPage(layout=layout, preserved=tuple(preserved))


def page_layouts_from_structure(structure: dict) -> tuple[AdaptedPage, ...]:
    """Map every supported page of an extracted document."""
    return tuple(
        page_layout_from_structure(page)
        for page in structure.get('pages') or ()
        if page.get('supported', False)
    )


# ── geometry ──────────────────────────────────────────────────────────────


def _content_bounds(blocks, page_height: float) -> tuple[float, float]:
    """Vertical measure between the running head and the folio."""
    top = 0.0
    bottom = page_height
    for raw in blocks:
        kind = raw.get('kind')
        bbox = raw.get('bbox') or ()
        if len(bbox) != 4:
            continue
        if kind == 'header':
            top = max(top, float(bbox[3]))
        elif kind == 'footer':
            bottom = min(bottom, float(bbox[1]))
    if bottom - top <= 0:
        return 0.0, page_height
    return top, bottom


def _columns(raw_columns, page: Rect, raw_blocks, top: float, bottom: float,
             page_number: int) -> tuple[Column, ...]:
    columns: list[Column] = []
    for raw in raw_columns:
        bbox = raw.get('bbox') or ()
        if len(bbox) != 4:
            continue
        x0, y0, x1, y1 = (float(value) for value in bbox)
        x1 += _ink_to_advance_slack(
            _max_span_size(raw_blocks, raw.get('index')), page)
        # Extraction reports the union of member blocks; the solver needs a
        # band that reaches the page measure so translated text can grow.
        band = Rect(
            max(page.x0, min(x0, page.x1)),
            max(page.y0, min(min(y0, top), page.y1)),
            max(page.x0, min(x1, page.x1)),
            max(page.y0, min(max(y1, bottom), page.y1)),
        )
        if band.width <= 0 or band.height <= 0:
            continue
        columns.append(Column(int(raw.get('index', len(columns))), band))
    if not columns:
        columns.append(Column(0, Rect(page.x0, top, page.x1, bottom)))
    columns.sort(key=lambda column: (column.bbox.x0, column.column_id))
    seen: set[int] = set()
    for column in columns:
        if column.column_id in seen:
            raise LayoutInputError(
                f'page {page_number} reports duplicate column id {column.column_id}'
            )
        seen.add(column.column_id)
    return tuple(columns)


def _block(raw: dict, page_number: int, page: Rect, columns: tuple[Column, ...],
           column_ids: set[int], content_top: float, content_bottom: float,
           preserved: list[PreservedBlock]) -> Block | None:
    kind = raw.get('kind')
    index = raw.get('index')
    bbox = raw.get('bbox') or ()
    if index is None or len(bbox) != 4:
        raise LayoutInputError(f'page {page_number} has a malformed block: {raw!r}')
    block_id = f'p{page_number}-b{index}'
    rect = Rect(*(float(value) for value in bbox))
    if not page.contains(rect, tolerance=1e-3):
        raise LayoutInputError(
            f'block {block_id!r} lies outside page {page_number}'
        )

    if kind in _PRESERVED_KINDS:
        preserved.append(PreservedBlock(block_id, kind, rect, f'running {kind}'))
        return None

    if kind in _FIXED_KINDS:
        return Block(
            block_id=block_id,
            kind=_FIXED_KINDS[kind],
            bbox=rect,
            column=_nearest_column(rect, columns),
        )

    if kind not in _FLOWABLE_KINDS:
        preserved.append(PreservedBlock(block_id, str(kind), rect,
                                        'unknown block kind'))
        return None

    text = (raw.get('text') or '').strip()
    if not text:
        preserved.append(PreservedBlock(block_id, str(kind), rect, 'empty text'))
        return None

    column = raw.get('column')
    if column is None or column not in column_ids:
        if len(columns) == 1:
            column = columns[0].column_id
        else:
            preserved.append(
                PreservedBlock(block_id, str(kind), rect,
                               'full-width flowable on a multi-column page')
            )
            return None
    assigned = next(item for item in columns if item.column_id == column)
    left = max(rect.x0, assigned.bbox.x0)
    right = min(rect.x1, assigned.bbox.x1)
    if right <= left:
        preserved.append(
            PreservedBlock(block_id, str(kind), rect,
                           'block does not lie inside its column')
        )
        return None
    spec = _font_spec(raw, rect)
    clipped = Rect(left, max(rect.y0, content_top),
                   min(right + spec.size_pt * _INK_SLACK_RATIO,
                       assigned.bbox.x1),
                   min(rect.y1, content_bottom))
    if clipped.height <= 0:
        preserved.append(
            PreservedBlock(block_id, str(kind), rect,
                           'block does not lie inside the page measure')
        )
        return None

    return Block(
        block_id=block_id,
        kind=_FLOWABLE_KINDS[kind],
        bbox=clipped,
        column=column,
        font=spec,
        source_text=text,
    )


def _max_span_size(raw_blocks, column_id) -> float:
    if column_id is None:
        return 0.0
    sizes = [
        float(span.get('font_size') or 0.0)
        for raw in raw_blocks
        if raw.get('column') == column_id
        for line in raw.get('lines') or ()
        for span in line.get('spans') or ()
    ]
    return max(sizes, default=0.0)


def _ink_to_advance_slack(font_size: float, page: Rect) -> float:
    """Room for the last glyph's right side bearing.

    A line bbox is the ink extent; the pen advance used for wrapping includes
    the final glyph's right side bearing.  Without this allowance an exactly
    full source line would reflow at the same font, so an identity translation
    could never preserve the source line breaks.
    """
    return min(max(font_size, 7.0) * _INK_SLACK_RATIO, page.width * 0.02)


def _nearest_column(rect: Rect, columns: tuple[Column, ...]) -> int:
    center = (rect.x0 + rect.x1) / 2
    return min(
        columns,
        key=lambda column: 1.0 if column.bbox.x0 <= center <= column.bbox.x1
        else min(abs(center - column.bbox.x0), abs(center - column.bbox.x1)),
    ).column_id


def _font_spec(raw: dict, rect: Rect) -> FontSpec:
    lines = raw.get('lines') or []
    weighted: dict[tuple[str, float, str], int] = {}
    for line in lines:
        for span in line.get('spans') or ():
            text = span.get('text') or ''
            if not text.strip():
                continue
            key = (
                str(span.get('font_family') or 'unknown'),
                float(span.get('font_size') or 0.0),
                'bold' if span.get('font_weight') == 'bold' else 'regular',
            )
            weighted[key] = weighted.get(key, 0) + len(text)
    if not weighted:
        size = max(rect.height, 1.0)
        return FontSpec('unknown', min(size, 12.0), max(min(size, 12.0) * 1.2, size))
    (family, size, weight), _chars = max(weighted.items(), key=lambda item: item[1])
    if size <= 0:
        size = max(rect.height, 1.0)
    leading = _leading(lines, size)
    return FontSpec(family, size, max(leading, size * 1.02), weight)


def _leading(lines, size: float) -> float:
    tops = [
        float(line['bbox'][1]) for line in lines
        if (line.get('bbox') or ()) and len(line['bbox']) == 4
    ]
    deltas = [
        later - earlier for earlier, later in zip(tops, tops[1:])
        if later - earlier > 0.1
    ]
    if not deltas:
        return size * 1.2
    return float(statistics.median(deltas))


def _link_captions(blocks: list[Block]) -> None:
    """Attach a caption to the nearest fixed element it visually belongs to."""
    fixed = [block for block in blocks if block.is_fixed]
    for index, block in enumerate(blocks):
        if block.kind != 'caption' or block.attached_to is not None:
            continue
        candidates = []
        for element in fixed:
            if not block.bbox.horizontally_overlaps(element.bbox):
                continue
            gap = min(
                abs(block.bbox.y1 - element.bbox.y0),
                abs(element.bbox.y1 - block.bbox.y0),
            )
            if gap <= _CAPTION_ATTACH_GAP_PT:
                candidates.append((gap, element.block_id))
        if candidates:
            _, attached = min(candidates)
            blocks[index] = Block(
                block_id=block.block_id,
                kind=block.kind,
                bbox=block.bbox,
                column=block.column,
                font=block.font,
                attached_to=attached,
                source_text=block.source_text,
            )