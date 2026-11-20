"""Source-geometry budgets and solver-backed fit checks for block translation.

The re-layout solver owns placement.  This module only asks it two questions
about an extracted block:

* how much width/height the source page leaves for that block, computed with
  the solver's documented obstacle rule; and
* whether a produced translation fits that band, by running the real solver on
  a one-block page whose column bottom is the budget limit.

There is no text truncation and no guessed font metric here: a refusal comes
from :func:`lib.pdf_relayout.solve_page` itself.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from ..pdf_relayout import (
    Block,
    Column,
    FontSpec,
    LayoutRefusal,
    PageLayout,
    Rect,
    solve_page,
)
from ..pdf_relayout.typography import BLOCK_GAP_PT, MIN_READABLE_FONT_SIZE_PT
from .model import LayoutBudget, TranslationInputError


# Extraction kinds that carry prose.  Everything else is fixed geometry and
# must never receive a model call.  ``rule``/``separator`` are accepted aliases
# in case another extractor represents graphic rules as blocks.
TEXT_BEARING_KINDS = frozenset({
    'title', 'heading', 'paragraph', 'text', 'list', 'caption', 'formula',
    'header', 'footer',
})
NON_TEXT_KINDS = frozenset({'figure', 'table', 'rule', 'separator'})

_SOLVER_KIND = {
    'title': 'heading',
    'heading': 'heading',
    'paragraph': 'text',
    'text': 'text',
    'list': 'list',
    'caption': 'caption',
    'formula': 'formula',
    'header': 'text',
    'footer': 'text',
}

_EPS = 1e-6

#: Same ink-to-advance allowance the layout bridge applies: a line bbox is the
#: ink extent, while wrapping measures the final glyph's advance.
_INK_SLACK_RATIO = 0.05


@dataclass(frozen=True, slots=True)
class SourceBand:
    """The rectangular band a block may occupy, in page points."""

    left_pt: float
    right_pt: float
    top_pt: float
    bottom_pt: float
    limiting_element: str

    @property
    def width_pt(self) -> float:
        return self.right_pt - self.left_pt

    @property
    def height_pt(self) -> float:
        return self.bottom_pt - self.top_pt


def is_text_bearing(kind: str) -> bool:
    """True only for extraction kinds whose content is prose to translate."""
    return kind in TEXT_BEARING_KINDS


def _bbox(block: Mapping[str, Any]) -> tuple[float, float, float, float]:
    raw = block.get('bbox')
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 4:
        raise TranslationInputError(f'block has no four-value bbox: {raw!r}')
    try:
        x0, y0, x1, y1 = (float(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise TranslationInputError(f'block bbox is not numeric: {raw!r}') from exc
    if x1 < x0 or y1 < y0:
        raise TranslationInputError(f'inverted block bbox: {raw!r}')
    return x0, y0, x1, y1


def _column_span(page_columns: Sequence[Mapping[str, Any]],
                 column: Any) -> tuple[float, float] | None:
    if not isinstance(column, int) or isinstance(column, bool):
        return None
    for position, entry in enumerate(page_columns):
        if not isinstance(entry, Mapping):
            continue
        index = entry.get('index', position)
        if index != column:
            continue
        raw = entry.get('bbox')
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 4:
            return None
        try:
            return float(raw[0]), float(raw[2])
        except (TypeError, ValueError):
            return None
    return None


def _horizontal_overlap(left: float, right: float,
                        bbox: tuple[float, float, float, float]) -> bool:
    return min(right, bbox[2]) - max(left, bbox[0]) > _EPS


def source_band(
    *,
    page_width: float,
    page_height: float,
    page_columns: Sequence[Mapping[str, Any]],
    page_blocks: Sequence[Mapping[str, Any]],
    block: Mapping[str, Any],
) -> SourceBand:
    """Compute the free band below one extracted block.

    The horizontal span is the block's bbox clipped to its extracted column.
    The vertical limit is the nearest *fixed* block below it whose horizontal
    span overlaps, keeping the solver's :data:`BLOCK_GAP_PT` clearance, or the
    page bottom.  Later flowable blocks are not obstacles: a
    length-compensating reflow may push them down the column, and the page
    solver owns whether the whole run fits.  A figure, table or rule never
    moves, so it remains a hard limit.
    """
    x0, y0, x1, _y1 = _bbox(block)
    left, right = x0, x1
    span = _column_span(page_columns, block.get('column'))
    if span is not None:
        left = max(left, span[0])
        right = min(right, span[1])
    if right <= left:
        left, right = x0, x1
    if is_text_bearing(str(block.get('kind') or '')):
        size = font_spec_for_block(block).size_pt
        slack = min(max(size, MIN_READABLE_FONT_SIZE_PT) * _INK_SLACK_RATIO,
                    page_width * 0.02)
        right_limit = span[1] if span is not None else float(page_width)
        right = min(right + slack, right_limit)

    limit = float(page_height)
    limiting = 'page bottom'
    for position, other in enumerate(page_blocks):
        if other is block:
            continue
        other_bbox = _bbox(other)
        if not _horizontal_overlap(left, right, other_bbox):
            continue
        if other_bbox[1] <= y0 + _EPS:
            continue
        if str(other.get('kind') or '') not in NON_TEXT_KINDS:
            continue  # flowable text reflows; the page solver decides fit
        candidate = other_bbox[1] - BLOCK_GAP_PT
        if candidate < limit - _EPS:
            limit = candidate
            other_kind = str(other.get('kind') or 'block')
            other_index = other.get('index', position)
            limiting = f'{other_kind} block {other_index}'
    bottom = max(y0, limit)
    return SourceBand(left, right, y0, bottom, limiting)


def layout_budget_with_band(
    *,
    page_width: float,
    page_height: float,
    page_columns: Sequence[Mapping[str, Any]],
    page_blocks: Sequence[Mapping[str, Any]],
    block: Mapping[str, Any],
) -> tuple[LayoutBudget, SourceBand]:
    band = source_band(
        page_width=page_width,
        page_height=page_height,
        page_columns=page_columns,
        page_blocks=page_blocks,
        block=block,
    )
    budget = LayoutBudget(
        available_width_pt=band.width_pt,
        available_height_pt=band.height_pt,
        limiting_element=band.limiting_element,
    )
    return budget, band


# ── source typography ─────────────────────────────────────────────────────


def _dominant(values: Mapping[str, int], default: str) -> str:
    if not values:
        return default
    return max(values, key=lambda value: (values[value], value))


def font_spec_for_block(block: Mapping[str, Any]) -> FontSpec:
    """Derive the source face/size/leading the solver should start from."""
    size_chars: dict[float, int] = {}
    family_chars: dict[str, int] = {}
    bold_chars = 0
    regular_chars = 0
    tops: list[float] = []

    for line in block.get('lines') or ():
        if not isinstance(line, Mapping):
            continue
        raw_line_bbox = line.get('bbox')
        if (isinstance(raw_line_bbox, Sequence)
                and not isinstance(raw_line_bbox, (str, bytes))
                and len(raw_line_bbox) == 4):
            try:
                tops.append(float(raw_line_bbox[1]))
            except (TypeError, ValueError):
                pass
        for span in line.get('spans') or ():
            if not isinstance(span, Mapping):
                continue
            text = span.get('text')
            count = len(text) if isinstance(text, str) else 0
            try:
                size = round(float(span.get('font_size') or 0.0), 3)
            except (TypeError, ValueError):
                size = 0.0
            if 0 < size <= 200:
                size_chars[size] = size_chars.get(size, 0) + max(count, 1)
            family = span.get('font_family')
            if isinstance(family, str) and family:
                family_chars[family] = family_chars.get(family, 0) + max(count, 1)
            weight = str(span.get('font_weight') or '').casefold()
            if weight == 'bold':
                bold_chars += max(count, 1)
            elif count:
                regular_chars += count

    if size_chars:
        size = max(size_chars, key=lambda value: (size_chars[value], value))
    else:
        heights = []
        for line in block.get('lines') or ():
            raw = line.get('bbox') if isinstance(line, Mapping) else None
            if (isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
                    and len(raw) == 4):
                try:
                    height = float(raw[3]) - float(raw[1])
                except (TypeError, ValueError):
                    continue
                if 0 < height <= 200:
                    heights.append(height)
        # A line bbox is roughly 1.2x its font size; without span metadata
        # derive the face size from that ratio rather than treating the full
        # line height as type size.
        size = round(statistics.median(heights) / 1.2, 3) if heights else 10.0

    leading = size * 1.2
    if len(tops) >= 2:
        deltas = [
            later - earlier
            for earlier, later in zip(tops, tops[1:])
            if later - earlier > _EPS
        ]
        if deltas:
            leading = statistics.median(deltas)
    leading = min(max(leading, size * 1.02), 400.0)
    size = min(max(size, 4.0), 200.0)
    leading = max(leading, size * 1.02)

    return FontSpec(
        family=_dominant(family_chars, 'DejaVu Sans'),
        size_pt=size,
        leading_pt=leading,
        weight='bold' if bold_chars > regular_chars else 'regular',
    )


def solver_kind_for(kind: str) -> str:
    try:
        return _SOLVER_KIND[kind]
    except KeyError as exc:
        raise TranslationInputError(
            f'block kind {kind!r} has no reflow-solver equivalent'
        ) from exc


# ── fit check ─────────────────────────────────────────────────────────────


def fit_refusal(
    *,
    block_id: str,
    block_index: int,
    kind: str,
    block: Mapping[str, Any],
    band: SourceBand,
    translated_text: str,
) -> LayoutRefusal | None:
    """Return the solver's refusal for this text, or ``None`` when it fits.

    The solver is run on a one-block page whose column is exactly the source
    band, so it measures real glyph advances, applies its leading/font floors,
    and reports ``does_not_fit`` with the same message a later page solve would
    produce.  Nothing is clipped: a refusal keeps every produced character.
    """
    if band.width_pt <= _EPS:
        raise TranslationInputError(
            f'block {block_index} has no usable translation width'
        )
    if band.height_pt <= _EPS:
        return LayoutRefusal(
            block_id=block_id,
            code='does_not_fit',
            message=(
                f'block {block_id!r} has no vertical budget before '
                f'{band.limiting_element}; refusing rather than overlapping'
            ),
            available_height_pt=band.height_pt,
            required_height_pt=0.0,
            minimum_font_size_pt=font_spec_for_block(block).size_pt,
            limiting_element=band.limiting_element,
        )

    x0, y0, _x1, y1 = _bbox(block)
    column_rect = Rect(band.left_pt, band.top_pt, band.right_pt, band.bottom_pt)
    source_rect = Rect(
        band.left_pt,
        band.top_pt,
        band.right_pt,
        min(max(y1, y0), band.bottom_pt),
    )
    solver_block = Block(
        block_id=block_id,
        kind=solver_kind_for(kind),
        bbox=source_rect,
        column=0,
        font=font_spec_for_block(block),
    )
    page_left = min(0.0, band.left_pt)
    page_top = min(0.0, band.top_pt)
    page_rect = Rect(
        page_left,
        page_top,
        max(x0, band.right_pt, page_left + 1.0),
        max(band.bottom_pt, band.top_pt, page_top + 1.0),
    )
    layout = PageLayout(
        page_id=f'{block_id}-budget',
        page=page_rect,
        columns=(Column(0, column_rect),),
        blocks=(solver_block,),
    )
    plan = solve_page(layout, {block_id: translated_text})
    if not plan.refusals:
        return None
    refusal = plan.refusals[0]
    # The one-block layout turns the source budget limit into its column
    # bottom.  Report the real obstacle (figure, table, block or page bottom)
    # so the refusal is diagnosable against the source geometry.
    return replace(
        refusal,
        message=refusal.message.replace(
            f'before {refusal.limiting_element}',
            f'before {band.limiting_element}',
        ),
        limiting_element=band.limiting_element,
    )
