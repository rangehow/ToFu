"""Pure-CPU length-aware re-layout for translated PDF pages.

Entry point: :func:`lib.pdf_relayout.solver.solve_page`.  Input is the block
model produced by the extraction task; output is a placement plan with
reflowed lines, per-run font faces, and explicit refusals.  No PDF or model is
read here, and no pixels are produced.
"""

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
)
from .solver import solve_page
from .typography import (
    BLOCK_GAP_PT,
    CAPTION_MIN_GAP_PT,
    FONT_FALLBACK_ORDER,
    MIN_LEADING_RATIO,
    MIN_READABLE_FONT_SIZE_PT,
)

__all__ = [
    'BLOCK_GAP_PT',
    'CAPTION_MIN_GAP_PT',
    'FONT_FALLBACK_ORDER',
    'MIN_LEADING_RATIO',
    'MIN_READABLE_FONT_SIZE_PT',
    'Block',
    'BlockPlacement',
    'Column',
    'FontSpec',
    'LayoutInputError',
    'LayoutInvariantError',
    'LayoutRefusal',
    'PageLayout',
    'PagePlan',
    'PlacedLine',
    'Rect',
    'TextRun',
    'solve_page',
]