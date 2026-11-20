"""Immutable input/output model for the pure-CPU PDF re-layout solver.

Coordinates use the extraction model's page space: origin at the top-left,
x grows right and y grows down, all values in PDF points.  A block's bbox is
its text/content band, not a padded cell.

The extraction layer (a separate task) produces :class:`PageLayout` plus a
translation per flowable block; :func:`lib.pdf_relayout.solver.solve_page`
returns a :class:`PagePlan`.  Nothing in this module reads a PDF, calls a
model, touches the network, or paints pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .typography import (
    FIXED_BLOCK_KINDS,
    FLOWABLE_BLOCK_KINDS,
    MIN_READABLE_FONT_SIZE_PT,
)

_EPS = 1e-6


class LayoutInputError(ValueError):
    """The extracted block model is internally inconsistent or out of bounds."""


class LayoutInvariantError(RuntimeError):
    """The solver produced an overlapping or out-of-column plan (never expected)."""


@dataclass(frozen=True, slots=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise LayoutInputError(f'inverted rectangle: {self!r}')

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def intersects(self, other: 'Rect', tolerance: float = 0.0) -> bool:
        return (
            min(self.x1, other.x1) - max(self.x0, other.x0) > tolerance
            and min(self.y1, other.y1) - max(self.y0, other.y0) > tolerance
        )

    def contains(self, inner: 'Rect', tolerance: float = _EPS) -> bool:
        return (
            inner.x0 >= self.x0 - tolerance
            and inner.y0 >= self.y0 - tolerance
            and inner.x1 <= self.x1 + tolerance
            and inner.y1 <= self.y1 + tolerance
        )

    def horizontally_overlaps(self, other: 'Rect', tolerance: float = _EPS) -> bool:
        return min(self.x1, other.x1) - max(self.x0, other.x0) > tolerance


@dataclass(frozen=True, slots=True)
class Column:
    column_id: int
    bbox: Rect


@dataclass(frozen=True, slots=True)
class FontSpec:
    """Source typography for one block.

    ``leading_pt`` is baseline-to-baseline distance.  ``family`` is the name
    reported by extraction; the solver maps it onto the curated stack rather
    than trusting an unavailable embedded face.
    """

    family: str = 'DejaVu Sans'
    size_pt: float = 10.0
    leading_pt: float = 12.0
    weight: str = 'regular'

    def __post_init__(self) -> None:
        if not (0 < self.size_pt <= 200):
            raise LayoutInputError(f'invalid font size: {self.size_pt!r}')
        if not (0 < self.leading_pt <= 400):
            raise LayoutInputError(f'invalid leading: {self.leading_pt!r}')
        if self.leading_pt < self.size_pt:
            raise LayoutInputError(
                f'leading {self.leading_pt!r} is smaller than font size {self.size_pt!r}'
            )


@dataclass(frozen=True, slots=True)
class Block:
    block_id: str
    kind: str
    bbox: Rect
    column: int
    font: FontSpec | None = None
    attached_to: str | None = None
    source_text: str = ''

    def __post_init__(self) -> None:
        if not self.block_id:
            raise LayoutInputError('block_id must not be empty')
        if self.kind not in FIXED_BLOCK_KINDS and self.kind not in FLOWABLE_BLOCK_KINDS:
            raise LayoutInputError(f'unknown block kind {self.kind!r}')
        if self.kind in FLOWABLE_BLOCK_KINDS and self.font is None:
            raise LayoutInputError(f'flowable block {self.block_id!r} needs a FontSpec')

    @property
    def is_fixed(self) -> bool:
        return self.kind in FIXED_BLOCK_KINDS


@dataclass(frozen=True, slots=True)
class PageLayout:
    page_id: str
    page: Rect
    columns: tuple[Column, ...]
    blocks: tuple[Block, ...]


@dataclass(frozen=True, slots=True)
class TextRun:
    text: str
    font_family: str
    font_path: str
    font_size_pt: float
    x0: float
    x1: float


@dataclass(frozen=True, slots=True)
class PlacedLine:
    text: str
    bbox: Rect
    baseline_y: float
    runs: tuple[TextRun, ...]


@dataclass(frozen=True, slots=True)
class BlockPlacement:
    block_id: str
    kind: str
    column: int
    bbox: Rect
    font_size_pt: float
    leading_pt: float
    lines: tuple[PlacedLine, ...]
    scale: float
    preserved_geometry: bool
    attached_to: str | None = None
    #: Where the source block's text was painted.  A length-compensating
    #: reflow moves a translated block down the column, so the renderer must
    #: remove the source glyphs at this original rectangle, not at ``bbox``.
    source_bbox: Rect | None = None

    @property
    def text(self) -> str:
        return '\n'.join(line.text for line in self.lines)


@dataclass(frozen=True, slots=True)
class LayoutRefusal:
    """One block that could not be placed without violating the contract."""

    block_id: str
    code: str
    message: str
    available_height_pt: float
    required_height_pt: float
    minimum_font_size_pt: float
    limiting_element: str


@dataclass(frozen=True, slots=True)
class PagePlan:
    page_id: str
    page: Rect
    columns: tuple[Column, ...]
    placements: tuple[BlockPlacement, ...]
    refusals: tuple[LayoutRefusal, ...]
    missing_glyphs: tuple[str, ...] = ()
    _index: Mapping[str, BlockPlacement] = field(default_factory=dict, repr=False)

    @property
    def accepted(self) -> bool:
        return not self.refusals

    def placement(self, block_id: str) -> BlockPlacement | None:
        return self._index.get(block_id)


def minimum_readable_size_for(font: FontSpec) -> float:
    """Never enlarge, and never shrink below the documented floor."""
    return min(font.size_pt, MIN_READABLE_FONT_SIZE_PT)