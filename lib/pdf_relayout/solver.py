"""Length-aware, pure-CPU placement of translated PDF text blocks.

Owner: this module decides *where* translated text goes.  It never parses a
PDF, renders glyphs, or calls a model or network.  The extraction task supplies
:class:`~lib.pdf_relayout.model.PageLayout` and one translation per flowable
block; this solver returns a :class:`~lib.pdf_relayout.model.PagePlan`.

Layout rules (the contract the later renderer relies on):

* Columns never change.  A flowable block keeps its original top edge and its
  original horizontal margins, clipped to its column, so text cannot bleed into
  another column.
* The page is solved top-to-bottom.  A block may extend downward into free
  space, but only until the first obstacle that horizontally overlaps it in the
  same column: another block's original top, a figure/table top, or the column
  bottom.
* Figures and tables keep their exact input geometry and are never moved or
  resized.  Captions keep their original top and their attachment; a caption
  above a figure cannot grow into it.
* To fit, the solver first tightens leading to the documented floor, then
  shrinks the font to the documented readable floor.  It never enlarges type.
* If the text still does not fit, the block is refused with a reason and the
  page keeps every other valid placement.  Refusal is always preferable to an
  overlap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .fonts import FontFace, FontLibrary, is_cjk_char
from .model import (
    Block,
    BlockPlacement,
    Column,
    FontSpec,
    LayoutInputError,
    LayoutInvariantError,
    LayoutRefusal,
    PageLayout,
    PagePlan,
    PlacedLine,
    Rect,
    TextRun,
    minimum_readable_size_for,
)
from .typography import (
    BLOCK_GAP_PT,
    CAPTION_MIN_GAP_PT,
    MIN_LEADING_RATIO,
    MIN_READABLE_FONT_SIZE_PT,
    style_for_family,
    weight_for_font_weight,
)

_EPS = 1e-6
_HEIGHT_EPS = 0.01
_SIZE_STEP_PT = 0.25
_LEADING_STEP_RATIO = 0.02

_NO_LINE_START = frozenset('，。、；：！？）】》」』”’％‰℃…—～·')
_NO_LINE_END = frozenset('（【《「『“‘')


@dataclass(frozen=True, slots=True)
class _Piece:
    text: str
    is_space: bool = False


@dataclass(frozen=True, slots=True)
class _Glyph:
    ch: str
    face: FontFace
    advance: float


@dataclass(frozen=True, slots=True)
class _Fit:
    size_pt: float
    leading_pt: float
    lines: tuple[tuple[_Glyph, ...], ...]

    @property
    def height_pt(self) -> float:
        return len(self.lines) * self.leading_pt


@dataclass(frozen=True, slots=True)
class _NoFit:
    required_height_pt: float
    minimum_font_size_pt: float
    minimum_leading_pt: float


def _tokenize(text: str) -> tuple[tuple[_Piece, ...], ...]:
    """Split into paragraphs of atomic, breakable pieces (CJK-aware)."""
    paragraphs: list[tuple[_Piece, ...]] = []
    current: list[_Piece] = []
    buffer: list[str] = []

    def flush_buffer() -> None:
        if buffer:
            current.append(_Piece(''.join(buffer)))
            buffer.clear()

    index = 0
    while index < len(text):
        ch = text[index]
        if ch in '\r\n':
            flush_buffer()
            paragraphs.append(tuple(current))
            current = []
            if ch == '\r' and index + 1 < len(text) and text[index + 1] == '\n':
                index += 1
        elif ch.isspace():
            flush_buffer()
            if current and not current[-1].is_space:
                current.append(_Piece(' ', is_space=True))
        elif is_cjk_char(ch):
            flush_buffer()
            previous = current[-1] if current else None
            if (
                ch in _NO_LINE_START
                and previous is not None
                and not previous.is_space
            ):
                current[-1] = _Piece(previous.text + ch)
            elif (
                previous is not None
                and not previous.is_space
                and previous.text[-1] in _NO_LINE_END
            ):
                current[-1] = _Piece(previous.text + ch)
            else:
                current.append(_Piece(ch))
        else:
            buffer.append(ch)
            following = text[index + 1] if index + 1 < len(text) else ''
            if ch in '-/–—' and following and not following.isspace() and not is_cjk_char(following):
                flush_buffer()
        index += 1
    flush_buffer()
    paragraphs.append(tuple(current))
    return tuple(paragraphs)


def _measure(text: str, size_pt: float, style: str, weight: str, fonts: FontLibrary) -> list[_Glyph]:
    glyphs = []
    for ch in text:
        face = fonts.face_for_char(ch, style, weight)
        glyphs.append(_Glyph(ch, face, face.advance(ch, size_pt)))
    return glyphs


def _split_glyphs(glyphs: list[_Glyph], max_width: float) -> list[list[_Glyph]]:
    chunks: list[list[_Glyph]] = []
    chunk: list[_Glyph] = []
    width = 0.0
    for glyph in glyphs:
        if chunk and width + glyph.advance > max_width + _EPS:
            chunks.append(chunk)
            chunk = []
            width = 0.0
        chunk.append(glyph)
        width += glyph.advance
    if chunk:
        chunks.append(chunk)
    return chunks


def _wrap_paragraph(
    paragraph: tuple[_Piece, ...],
    max_width: float,
    size_pt: float,
    style: str,
    weight: str,
    fonts: FontLibrary,
    lines: list[tuple[_Glyph, ...]],
) -> None:
    current: list[_Glyph] = []
    current_width = 0.0
    pending_space: _Glyph | None = None

    def flush() -> None:
        nonlocal current, current_width
        if current:
            lines.append(tuple(current))
        current = []
        current_width = 0.0

    for piece in paragraph:
        if piece.is_space:
            if current:
                pending_space = _measure(' ', size_pt, style, weight, fonts)[0]
            continue
        glyphs = _measure(piece.text, size_pt, style, weight, fonts)
        piece_width = sum(glyph.advance for glyph in glyphs)
        separator = pending_space.advance if (pending_space and current) else 0.0
        if current and current_width + separator + piece_width <= max_width + _EPS:
            if pending_space and current:
                current.append(pending_space)
                current_width += pending_space.advance
            current.extend(glyphs)
            current_width += piece_width
            pending_space = None
            continue
        if current:
            flush()
        pending_space = None
        if piece_width <= max_width + _EPS:
            current = list(glyphs)
            current_width = piece_width
            continue
        chunks = _split_glyphs(glyphs, max_width)
        for chunk in chunks[:-1]:
            lines.append(tuple(chunk))
        current = chunks[-1]
        current_width = sum(glyph.advance for glyph in current)
    flush()


def _wrap_text(
    paragraphs: tuple[tuple[_Piece, ...], ...],
    max_width: float,
    size_pt: float,
    style: str,
    weight: str,
    fonts: FontLibrary,
) -> tuple[tuple[_Glyph, ...], ...]:
    lines: list[tuple[_Glyph, ...]] = []
    for paragraph in paragraphs:
        _wrap_paragraph(paragraph, max_width, size_pt, style, weight, fonts, lines)
    return tuple(lines)


def _descending_ratios(start: float, floor: float) -> list[float]:
    values = [start]
    value = start
    while value - _LEADING_STEP_RATIO >= floor + _EPS:
        value -= _LEADING_STEP_RATIO
        values.append(value)
    if abs(values[-1] - floor) > _EPS:
        values.append(floor)
    return values


def _candidate_typography(font_size: float, leading: float):
    original_ratio = leading / font_size
    floor_ratio = min(original_ratio, MIN_LEADING_RATIO)
    for ratio in _descending_ratios(original_ratio, floor_ratio):
        yield font_size, font_size * ratio
    minimum_size = min(font_size, MIN_READABLE_FONT_SIZE_PT)
    size = font_size - _SIZE_STEP_PT
    while size > minimum_size + _EPS:
        yield size, size * floor_ratio
        size -= _SIZE_STEP_PT
    yield minimum_size, minimum_size * floor_ratio


def _fit_text(
    text: str,
    max_width: float,
    available_height: float,
    block: Block,
    fonts: FontLibrary,
) -> tuple[_Fit | None, _NoFit | None]:
    spec = block.font
    assert spec is not None
    style = style_for_family(spec.family)
    weight = weight_for_font_weight(spec.weight)
    if not text.strip():
        return _Fit(spec.size_pt, spec.leading_pt, ()), None

    paragraphs = _tokenize(text)
    last: _Fit | None = None
    for size_pt, leading_pt in _candidate_typography(spec.size_pt, spec.leading_pt):
        lines = _wrap_text(paragraphs, max_width, size_pt, style, weight, fonts)
        fit = _Fit(size_pt, leading_pt, lines)
        if fit.height_pt <= available_height + _HEIGHT_EPS:
            return fit, None
        last = fit

    assert last is not None
    return None, _NoFit(
        required_height_pt=last.height_pt,
        minimum_font_size_pt=last.size_pt,
        minimum_leading_pt=last.leading_pt,
    )


def _horizontal_overlap(left: Rect, right: Rect) -> bool:
    return min(left.x1, right.x1) - max(left.x0, right.x0) > _EPS


def _measure_width(block: Block, column: Column) -> tuple[float, float, float]:
    left = max(block.bbox.x0, column.bbox.x0)
    right = min(block.bbox.x1, column.bbox.x1)
    return left, right, right - left


def _floor_typography(spec: FontSpec) -> tuple[float, float]:
    """The smallest legal (size, leading) pair; the refusal boundary."""
    ratio = min(spec.leading_pt / spec.size_pt, MIN_LEADING_RATIO)
    size = min(spec.size_pt, MIN_READABLE_FONT_SIZE_PT)
    return size, size * ratio


def _minimum_height(text: str, max_width: float, block: Block,
                    fonts: FontLibrary) -> float:
    """Height this text needs at the readable floor, used for reservation."""
    if not text.strip():
        return 0.0
    spec = block.font
    assert spec is not None
    size, leading = _floor_typography(spec)
    lines = _wrap_text(
        _tokenize(text), max_width, size,
        style_for_family(spec.family), weight_for_font_weight(spec.weight), fonts,
    )
    return len(lines) * leading


def _obstacles(column: Column, blocks: tuple[Block, ...]) -> tuple[Block, ...]:
    """Fixed elements that share horizontal space with ``column``."""
    return tuple(
        block for block in blocks
        if block.is_fixed and _horizontal_overlap(block.bbox, column.bbox)
    )


def _clear_of_obstacles(top: float, obstacles: tuple[Block, ...]) -> float:
    """Push ``top`` below any fixed element it would otherwise enter."""
    for obstacle in obstacles:
        if top + _EPS < obstacle.bbox.y0:
            break
        if top < obstacle.bbox.y1 - _EPS:
            top = obstacle.bbox.y1 + BLOCK_GAP_PT
    return top


def _gap_before(block: Block, obstacle: Block) -> float:
    """Separation kept between ``block`` and a fixed element below it."""
    if (
        block.kind == 'caption'
        and block.attached_to == obstacle.block_id
        and obstacle.bbox.y0 >= block.bbox.y1
    ):
        return max(CAPTION_MIN_GAP_PT, obstacle.bbox.y0 - block.bbox.y1)
    return BLOCK_GAP_PT


def _region_limit(block: Block, top: float, column: Column,
                  obstacles: tuple[Block, ...]) -> tuple[float, str]:
    """Lowest y the block's region may reach, and what bounds it there."""
    limit = column.bbox.y1
    limiting = f'column {column.column_id} bottom'
    for obstacle in obstacles:
        if obstacle.bbox.y0 < top - _EPS:
            continue
        candidate = obstacle.bbox.y0 - _gap_before(block, obstacle)
        if candidate < limit:
            limit = candidate
            limiting = f'{obstacle.kind} {obstacle.block_id!r}'
    return limit, limiting


def _flow_region(
    region: list[Block],
    top: float,
    limit: float,
    column: Column,
    fonts: FontLibrary,
    texts: list[str],
    limiting: str,
    obstacles: tuple[Block, ...],
) -> list[BlockPlacement | LayoutRefusal]:
    """Place one contiguous run of flowable blocks between fixed obstacles.

    Every block is reserved its readable-floor height first; leftover space is
    then handed to blocks in reading order.  If the run's floor heights do not
    fit, the longest prefix that keeps clear of the first refused block's
    original rectangle is placed and the rest are refused, so preserved source
    text can never be overprinted by a neighbouring translated block.
    """
    widths = [_measure_width(block, column) for block in region]
    floors = [
        _minimum_height(text, width, block, fonts)
        for text, (_, _, width), block in zip(texts, widths, region)
    ]
    # Minimum readable layout: every block at the plain readable floor, with
    # original tops preserved when the original gap is still large enough.
    base_bottoms: list[float] = []
    cursor = top
    for index, block in enumerate(region):
        block_top = (top if index == 0
                     else max(block.bbox.y0, cursor + BLOCK_GAP_PT))
        cursor = block_top + floors[index]
        base_bottoms.append(cursor)

    count = len(region)
    slack = 0.0
    if base_bottoms[-1] > limit + _HEIGHT_EPS:
        count = 0
        for index in range(len(region)):
            if base_bottoms[index] > limit + _HEIGHT_EPS:
                break
            if index + 1 < len(region):
                # Refused blocks keep their source text at the original
                # rectangle, so the placed prefix must stay above it.
                if base_bottoms[index] > region[index + 1].bbox.y0 - BLOCK_GAP_PT:
                    break
            count = index + 1
    else:
        slack = limit - base_bottoms[-1]

    results: list[BlockPlacement | LayoutRefusal] = []
    cursor = top
    for index in range(count):
        block = region[index]
        block_top = (top if index == 0
                     else max(block.bbox.y0, cursor + BLOCK_GAP_PT))
        block_limit = limit
        for obstacle in obstacles:
            if obstacle.bbox.y0 >= block_top - _EPS:
                block_limit = min(
                    block_limit,
                    obstacle.bbox.y0 - _gap_before(block, obstacle),
                )
        usable = min(block_limit - block_top, floors[index] + slack)
        _, _, width = widths[index]
        fit, failure = _fit_text(texts[index], width, usable, block, fonts)
        if fit is None:
            # The floor itself always fits a placed block by construction.
            assert failure is not None
            results.append(_refusal(block, failure, usable, limiting, column))
            cursor = block_top
            continue
        results.append(_build_placement(block, column, fit, fonts, block_top))
        slack = max(0.0, slack - max(0.0, fit.height_pt - floors[index]))
        cursor = block_top + fit.height_pt

    for index in range(count, len(region)):
        block = region[index]
        size, leading = _floor_typography(block.font)
        results.append(_refusal(
            block,
            _NoFit(floors[index], size, leading),
            max(0.0, limit - top),
            limiting,
            column,
        ))
    return results


def _missing_translation(block: Block) -> LayoutRefusal:
    return LayoutRefusal(
        block_id=block.block_id,
        code='missing_translation',
        message=f'no translated text was supplied for block {block.block_id!r}',
        available_height_pt=0.0,
        required_height_pt=0.0,
        minimum_font_size_pt=minimum_readable_size_for(block.font),
        limiting_element='missing translation',
    )


def _refusal(block: Block, failure: _NoFit, available: float,
             limiting: str, column: Column) -> LayoutRefusal:
    return LayoutRefusal(
        block_id=block.block_id,
        code='does_not_fit',
        message=(
            f'block {block.block_id!r} needs {failure.required_height_pt:.1f}pt at the '
            f'{failure.minimum_font_size_pt:.1f}pt minimum (leading '
            f'{failure.minimum_leading_pt:.1f}pt) but only {available:.1f}pt remains '
            f'in column {column.column_id} before {limiting}; refusing rather than '
            f'overlapping'
        ),
        available_height_pt=available,
        required_height_pt=failure.required_height_pt,
        minimum_font_size_pt=failure.minimum_font_size_pt,
        limiting_element=limiting,
    )


def _line_runs(glyphs: tuple[_Glyph, ...], size_pt: float, left: float) -> tuple[TextRun, ...]:
    runs: list[TextRun] = []
    x = left
    for glyph in glyphs:
        if runs and runs[-1].font_family == glyph.face.family and runs[-1].font_path == glyph.face.path:
            previous = runs[-1]
            runs[-1] = TextRun(
                text=previous.text + glyph.ch,
                font_family=previous.font_family,
                font_path=previous.font_path,
                font_size_pt=size_pt,
                x0=previous.x0,
                x1=x + glyph.advance,
            )
        else:
            runs.append(
                TextRun(
                    text=glyph.ch,
                    font_family=glyph.face.family,
                    font_path=glyph.face.path,
                    font_size_pt=size_pt,
                    x0=x,
                    x1=x + glyph.advance,
                )
            )
        x += glyph.advance
    return tuple(runs)


def _build_placement(
    block: Block,
    column: Column,
    fit: _Fit,
    fonts: FontLibrary,
    top: float,
) -> BlockPlacement:
    spec = block.font
    assert spec is not None
    left = max(block.bbox.x0, column.bbox.x0)
    right = min(block.bbox.x1, column.bbox.x1)
    lines: list[PlacedLine] = []
    for offset, glyphs in enumerate(fit.lines):
        line_top = top + offset * fit.leading_pt
        width = sum(glyph.advance for glyph in glyphs)
        ascent = max(
            (glyph.face.ascent_ratio for glyph in glyphs),
            default=fonts.face_for_char('M', style_for_family(spec.family), weight_for_font_weight(spec.weight)).ascent_ratio,
        )
        lines.append(
            PlacedLine(
                text=''.join(glyph.ch for glyph in glyphs),
                bbox=Rect(left, line_top, min(right, left + width), line_top + fit.leading_pt),
                baseline_y=line_top + fit.size_pt * ascent,
                runs=_line_runs(glyphs, fit.size_pt, left),
            )
        )
    height = len(fit.lines) * fit.leading_pt
    return BlockPlacement(
        block_id=block.block_id,
        kind=block.kind,
        column=block.column,
        bbox=Rect(left, top, right, top + height),
        font_size_pt=fit.size_pt,
        leading_pt=fit.leading_pt,
        lines=tuple(lines),
        scale=fit.size_pt / spec.size_pt,
        preserved_geometry=False,
        attached_to=block.attached_to,
        source_bbox=block.bbox,
    )


def _fixed_placement(block: Block) -> BlockPlacement:
    spec = block.font
    return BlockPlacement(
        block_id=block.block_id,
        kind=block.kind,
        column=block.column,
        bbox=block.bbox,
        font_size_pt=spec.size_pt if spec else 0.0,
        leading_pt=spec.leading_pt if spec else 0.0,
        lines=(),
        scale=1.0,
        preserved_geometry=True,
        attached_to=block.attached_to,
        source_bbox=block.bbox,
    )


def _validate_input(layout: PageLayout) -> None:
    page = layout.page
    columns: dict[int, Column] = {}
    for column in layout.columns:
        if column.column_id in columns:
            raise LayoutInputError(f'duplicate column id {column.column_id!r}')
        if not page.contains(column.bbox, tolerance=1e-3):
            raise LayoutInputError(
                f'column {column.column_id!r} lies outside page {layout.page_id!r}'
            )
        columns[column.column_id] = column

    for index, column in enumerate(layout.columns):
        for other in layout.columns[index + 1:]:
            if column.bbox.intersects(other.bbox, tolerance=1e-3):
                raise LayoutInputError(
                    f'columns {column.column_id!r} and {other.column_id!r} overlap'
                )

    blocks: dict[str, Block] = {}
    for block in layout.blocks:
        if block.block_id in blocks:
            raise LayoutInputError(f'duplicate block id {block.block_id!r}')
        blocks[block.block_id] = block

    for block in layout.blocks:
        if not page.contains(block.bbox, tolerance=1e-3):
            raise LayoutInputError(
                f'block {block.block_id!r} lies outside page {layout.page_id!r}'
            )
        if block.is_fixed:
            continue
        column = columns.get(block.column)
        if column is None:
            raise LayoutInputError(
                f'block {block.block_id!r} names unknown column {block.column!r}'
            )
        if (
            block.bbox.x0 < column.bbox.x0 - 1e-3
            or block.bbox.x1 > column.bbox.x1 + 1e-3
            or block.bbox.y0 < column.bbox.y0 - 1e-3
            or block.bbox.y1 > column.bbox.y1 + 1e-3
        ):
            raise LayoutInputError(
                f'block {block.block_id!r} is not inside column {column.column_id!r}'
            )
        if block.kind == 'caption' and block.attached_to:
            attached = blocks.get(block.attached_to)
            if attached is None:
                raise LayoutInputError(
                    f'caption {block.block_id!r} attaches to unknown block {block.attached_to!r}'
                )
            if not attached.is_fixed:
                raise LayoutInputError(
                    f'caption {block.block_id!r} attaches to non-fixed block {attached.block_id!r}'
                )
            if not _horizontal_overlap(block.bbox, attached.bbox):
                raise LayoutInputError(
                    f'caption {block.block_id!r} does not overlap its attached {attached.kind!r}'
                )

    all_blocks = tuple(layout.blocks)
    for index, block in enumerate(all_blocks):
        for other in all_blocks[index + 1:]:
            if block.bbox.intersects(other.bbox, tolerance=1e-3):
                raise LayoutInputError(
                    f'source blocks {block.block_id!r} and {other.block_id!r} overlap'
                )


def _validate_plan(plan: PagePlan, layout: PageLayout) -> None:
    columns = {column.column_id: column for column in layout.columns}
    fixed = {
        placement.block_id: placement
        for placement in plan.placements
        if placement.preserved_geometry
    }
    flowable = [placement for placement in plan.placements if not placement.preserved_geometry]
    source = {block.block_id: block for block in layout.blocks}

    for placement in plan.placements:
        block = source[placement.block_id]
        if placement.column != block.column:
            raise LayoutInvariantError(
                f"block {placement.block_id!r} changed column from {block.column!r} to {placement.column!r}"
            )
        if placement.preserved_geometry:
            if placement.bbox != block.bbox:
                raise LayoutInvariantError(f'fixed block {placement.block_id!r} moved')
            continue
        column = columns[placement.column]
        if not column.bbox.contains(placement.bbox, tolerance=1e-3):
            raise LayoutInvariantError(
                f'block {placement.block_id!r} escaped column {placement.column!r}'
            )

    for placement in flowable:
        for fixed_placement in fixed.values():
            if placement.bbox.intersects(fixed_placement.bbox, tolerance=1e-3):
                raise LayoutInvariantError(
                    f'block {placement.block_id!r} overlaps {fixed_placement.block_id!r}'
                )
    for index, placement in enumerate(flowable):
        for other in flowable[index + 1:]:
            if placement.bbox.intersects(other.bbox, tolerance=1e-3):
                raise LayoutInvariantError(
                    f'blocks {placement.block_id!r} and {other.block_id!r} overlap'
                )

    for placement in flowable:
        if not placement.attached_to:
            continue
        attached = fixed.get(placement.attached_to)
        if attached is None:
            continue
        if placement.bbox.y0 >= attached.bbox.y1 - 1e-3:
            continue
        gap = attached.bbox.y0 - placement.bbox.y1
        if gap < CAPTION_MIN_GAP_PT - 1e-3:
            raise LayoutInvariantError(
                f'caption {placement.block_id!r} lost its attachment gap'
            )


def solve_page(
    layout: PageLayout,
    translations: Mapping[str, str],
    *,
    font_dirs: Iterable[str] | None = None,
) -> PagePlan:
    """Return the placement plan for one page.

    ``translations`` maps flowable ``block_id`` to translated text.  A missing
    entry is an explicit refusal, never a silent source-text fallback.  Fixed
    blocks need no translation and keep their exact geometry.
    """
    _validate_input(layout)
    fonts = FontLibrary(font_dirs)
    {column.column_id: column for column in layout.columns}
    blocks = layout.blocks
    outcomes: dict[str, BlockPlacement | LayoutRefusal] = {}

    for block in blocks:
        if block.is_fixed:
            outcomes[block.block_id] = _fixed_placement(block)
            continue
        if translations.get(block.block_id) is None:
            # The source text stays where it was and becomes a hard obstacle
            # for every translated block in the column.
            outcomes[block.block_id] = _missing_translation(block)

    for column in sorted(layout.columns, key=lambda item: item.bbox.x0):
        flowable = [
            block for block in blocks
            if not block.is_fixed and block.column == column.column_id
        ]
        missing = [
            block for block in flowable
            if translations.get(block.block_id) is None
        ]
        flow = sorted(
            (block for block in flowable if translations.get(block.block_id) is not None),
            key=lambda block: (block.bbox.y0, block.bbox.x0),
        )
        for block in flow:
            _, _, width = _measure_width(block, column)
            if width <= _EPS:
                raise LayoutInputError(
                    f'block {block.block_id!r} has no usable width'
                )
        obstacles = tuple(sorted(
            _obstacles(column, blocks) + tuple(missing),
            key=lambda block: (block.bbox.y0, block.bbox.x0),
        ))

        cursor: float | None = None
        position = 0
        while position < len(flow):
            block = flow[position]
            top = (block.bbox.y0 if cursor is None
                   else max(block.bbox.y0, cursor + BLOCK_GAP_PT))
            top = _clear_of_obstacles(top, obstacles)
            limit, limiting = _region_limit(block, top, column, obstacles)
            region = [
                candidate for candidate in flow[position:]
                if candidate.bbox.y0 < limit - _EPS
            ]
            if not region:
                region = [block]
            region_texts = [translations[block.block_id] for block in region]
            for outcome in _flow_region(region, top, limit, column, fonts,
                                        region_texts, limiting, obstacles):
                outcomes[outcome.block_id] = outcome
            position += len(region)
            cursor = limit

    placements: list[BlockPlacement] = []
    refusals: list[LayoutRefusal] = []
    for block in blocks:
        outcome = outcomes.get(block.block_id)
        if outcome is None:
            outcome = _missing_translation(block)
        if isinstance(outcome, BlockPlacement):
            placements.append(outcome)
        else:
            refusals.append(outcome)

    index = {placement.block_id: placement for placement in placements}
    plan = PagePlan(
        page_id=layout.page_id,
        page=layout.page,
        columns=layout.columns,
        placements=tuple(placements),
        refusals=tuple(refusals),
        missing_glyphs=tuple(sorted(fonts.missing_glyphs)),
        _index=index,
    )
    _validate_plan(plan, layout)
    return plan