"""Result model for block-level translation of extracted PDF pages.

One :class:`BlockTranslation` is emitted per extraction block, in the page's
reading order.  Blocks are never merged, split or reordered: a figure keeps its
position in the result as ``skipped_no_text``, and each translated block still
corresponds to exactly one source block.

The layout budget is the source-geometry band a later reflow pass may use for
that block: the block's clipped width and the distance from its top edge to the
first obstacle below it.  It is a *hint* computed before translation; the
translation adapter additionally runs the real re-layout solver to reject text
the budget cannot hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from ..pdf_relayout import LayoutRefusal


TranslationStatus = Literal['translated', 'skipped_no_text', 'failed']


class TranslationInputError(ValueError):
    """The extracted page model is missing required geometry or is inconsistent."""


@dataclass(frozen=True, slots=True)
class LayoutBudget:
    """Source-geometry space reported for one extraction block.

    Every block gets a budget, including fixed geometry, so the result stays
    one-to-one with the extractor.  Only a text-bearing block's budget guides
    translation reflow; a figure/table budget is informational and is never a
    text target.

    ``available_width_pt`` is the block's width clipped to its extracted
    column.  ``available_height_pt`` is distance from the block's top edge to
    the nearest obstacle below it in the same horizontal span, before the
    documented block gap, or the page bottom.  ``limiting_element`` names that
    obstacle so a refusal can be diagnosed without re-reading the page.
    """

    available_width_pt: float
    available_height_pt: float
    limiting_element: str


@dataclass(frozen=True, slots=True)
class GlossaryHit:
    """One caller-supplied terminology mapping that fired in a translation."""

    source_term: str
    target_term: str
    occurrences: int


@dataclass(frozen=True, slots=True)
class BlockTranslation:
    """Translation outcome for exactly one extraction block.

    ``status`` is the block's outcome:

    ``translated``
        A non-empty model result was produced, terminology was applied, and
        the re-layout solver can fit the result in the block's budget.
    ``skipped_no_text``
        The block is fixed geometry (figure/table/rule), or it carries no
        non-whitespace text.  No model call was made.
    ``failed``
        The transport call raised, returned a non-string or empty result, or
        the produced text cannot fit.  A layout refusal is carried on
        :attr:`refusal`; a transport/invalid-result failure is carried on
        :attr:`error`.  A page never aborts because one block failed.

    When the solver refuses a translation, :attr:`translated_text` still holds
    the complete produced text.  It is retained, never truncated, so the
    refusal decision can be inspected and retried.
    """

    block_id: str
    block_index: int
    kind: str
    status: TranslationStatus
    source_text: str
    translated_text: str
    budget: LayoutBudget | None
    glossary_hits: tuple[GlossaryHit, ...] = ()
    refusal: LayoutRefusal | None = None
    error: str | None = None

    @property
    def is_translated(self) -> bool:
        return self.status == 'translated'


@dataclass(frozen=True, slots=True)
class PageTranslation:
    """Per-block translations for one extracted page, in reading order."""

    page_id: str
    page_number: int
    target_language: str
    blocks: tuple[BlockTranslation, ...]

    def block(self, block_id: str) -> BlockTranslation | None:
        return next((item for item in self.blocks if item.block_id == block_id), None)

    @property
    def translations(self) -> Mapping[str, str]:
        """Usable ``block_id -> translated text`` map for the reflow solver."""
        return {
            item.block_id: item.translated_text
            for item in self.blocks
            if item.status == 'translated'
        }

    @property
    def failures(self) -> tuple[BlockTranslation, ...]:
        return tuple(item for item in self.blocks if item.status == 'failed')

    @property
    def refusals(self) -> tuple[LayoutRefusal, ...]:
        return tuple(item.refusal for item in self.blocks if item.refusal is not None)

    @property
    def accepted(self) -> bool:
        """True when every block is translated or legitimately text-free."""
        return not self.failures
