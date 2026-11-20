"""Block-level translation for the PDF translation pipeline.

Entry point: :func:`lib.pdf_translate.adapter.translate_page`.  It consumes the
page model produced by :func:`lib.pdf_parser.structure.extract_pdf_structure`
and returns one :class:`~lib.pdf_translate.model.BlockTranslation` per block:
translated text, a source-geometry layout budget (available width/height), and
a status of ``translated``, ``skipped_no_text`` or ``failed``.

The model is injected as a ``model_call(prompt) -> str`` callable, so the caller
uses the product's own model path and tests can substitute a fake.  Source text
is treated strictly as data inside a fixed instruction template; see
:mod:`lib.pdf_translate.adapter` for the transport and prompt-injection
contract.  This package performs no HTTP, provider-SDK or key handling, and the
fit check is pure CPU through :mod:`lib.pdf_relayout`.
"""

from .adapter import ModelCall, translate_page
from .layout import (
    NON_TEXT_KINDS,
    TEXT_BEARING_KINDS,
    SourceBand,
    fit_refusal,
    font_spec_for_block,
    is_text_bearing,
    layout_budget_with_band,
    source_band,
)
from .model import (
    BlockTranslation,
    GlossaryHit,
    LayoutBudget,
    PageTranslation,
    TranslationInputError,
    TranslationStatus,
)
from .prompt import apply_glossary, build_translation_prompt

__all__ = [
    'BlockTranslation',
    'GlossaryHit',
    'LayoutBudget',
    'ModelCall',
    'NON_TEXT_KINDS',
    'PageTranslation',
    'SourceBand',
    'TEXT_BEARING_KINDS',
    'TranslationInputError',
    'TranslationStatus',
    'apply_glossary',
    'build_translation_prompt',
    'fit_refusal',
    'font_spec_for_block',
    'is_text_bearing',
    'layout_budget_with_band',
    'source_band',
    'translate_page',
]
