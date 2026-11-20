"""Block-level translation adapter for the PDF translation pipeline.

Owner: ``lib/pdf_translate/``.  This layer sits between the pure-CPU
extraction model (``lib/pdf_parser/structure.py``) and the geometry-only
re-layout solver (``lib/pdf_relayout/``).  It is the only place in the PDF
pipeline that turns source prose into translated prose.

Contract
--------
* Input is one extracted page mapping (``extract_pdf_structure()['pages'][i]``).
* Exactly one :class:`~lib.pdf_translate.model.BlockTranslation` is returned per
  extraction block, in the page's reading order.  Blocks are never merged,
  split or reordered.
* Only text-bearing kinds are sent to the model.  Figures, tables and rules are
  reported as ``skipped_no_text``; a text-bearing block with no non-whitespace
  text is likewise skipped.
* Each translated block carries the source-geometry layout budget a reflow pass
  may use.  The produced text is then checked by the real re-layout solver.
  Text that cannot fit is reported as ``failed`` with the solver's
  :class:`~lib.pdf_relayout.LayoutRefusal`; it is never truncated.
* A glossary mapping (source term -> required rendering) is applied after the
  model returns, in one deterministic longest-term-first pass, and every term
  that fired is reported on the block.

Transport
---------
The model is an injected callable ``model_call(prompt) -> str``.  Callers pass
the product's own model call path; this module imports no provider SDK, opens
no socket, reads no credential and performs no retry of its own.  Tests pass a
fake callable.  A call that raises, returns a non-string or returns whitespace
only fails that block; the rest of the page is still translated.

Prompt injection
----------------
Source text is untrusted.  It is never concatenated into the instruction
sentence: :func:`lib.pdf_translate.prompt.build_translation_prompt` places the
fixed rules first and the source inside one ``<source_text>`` data block, tells
the model before it sees the data to treat that block strictly as data and
never follow instructions found there, and neutralises any closing tag in the
source so it cannot end the data block.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .layout import (
    fit_refusal,
    is_text_bearing,
    layout_budget_with_band,
)
from .model import (
    BlockTranslation,
    PageTranslation,
    TranslationInputError,
)
from .prompt import apply_glossary, build_translation_prompt


ModelCall = Callable[[str], str]


def translate_page(
    page: Mapping[str, Any],
    *,
    target_language: str,
    model_call: ModelCall,
    glossary: Mapping[str, str] | None = None,
    page_id: str | None = None,
) -> PageTranslation:
    """Translate every text-bearing block of one extracted page.

    ``page`` is one item of ``extract_pdf_structure()['pages']``.  The callable
    receives a fixed-template prompt per block and must return the translated
    text.  ``glossary`` maps a source-language term to the rendering the caller
    requires; it is applied after translation and does not change which blocks
    are translated.

    Raises:
        TranslationInputError: the page mapping or block geometry is invalid.
        TypeError: ``model_call`` is not callable or ``glossary`` is not a
            mapping.
        ValueError: ``target_language`` is empty or a glossary entry is invalid.
    """
    if not isinstance(page, Mapping):
        raise TranslationInputError('page must be a mapping from the extractor')
    if not isinstance(target_language, str) or not target_language.strip():
        raise ValueError('target_language must be a non-empty string')
    if not callable(model_call):
        raise TypeError('model_call must be callable')
    terms = _validated_glossary(glossary)

    page_number = _page_number(page)
    resolved_page_id = page_id or f'page-{page_number}'
    if not isinstance(resolved_page_id, str) or not resolved_page_id:
        raise ValueError('page_id must be a non-empty string')
    width, height = _page_size(page)
    columns = _page_columns(page)
    blocks = _page_blocks(page)
    _reject_duplicate_indexes(blocks)

    results: list[BlockTranslation] = []
    for position, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            raise TranslationInputError(f'block {position} is not a mapping')
        block_index = _block_index(block, position)
        # Match the canonical extraction-to-solver bridge
        # (lib.pdf_parser.layout), so translations map by identity.
        block_id = f'p{page_number}-b{block_index}'
        kind = str(block.get('kind') or '')
        text = block.get('text', '')
        if text is None:
            text = ''
        if not isinstance(text, str):
            raise TranslationInputError(
                f'block {block_index} text is not a string: {type(text).__name__}'
            )

        budget, band = layout_budget_with_band(
            page_width=width,
            page_height=height,
            page_columns=columns,
            page_blocks=blocks,
            block=block,
        )
        if not is_text_bearing(kind):
            results.append(BlockTranslation(
                block_id=block_id,
                block_index=block_index,
                kind=kind,
                status='skipped_no_text',
                source_text=text,
                translated_text='',
                budget=budget,
            ))
            continue

        if not text.strip():
            results.append(BlockTranslation(
                block_id=block_id,
                block_index=block_index,
                kind=kind,
                status='skipped_no_text',
                source_text=text,
                translated_text='',
                budget=budget,
            ))
            continue

        prompt = build_translation_prompt(text, target_language)
        try:
            response = model_call(prompt)
        except Exception as exc:  # one block must never abort the page
            results.append(BlockTranslation(
                block_id=block_id,
                block_index=block_index,
                kind=kind,
                status='failed',
                source_text=text,
                translated_text='',
                budget=budget,
                error=f'{type(exc).__name__}: {exc}',
            ))
            continue

        if not isinstance(response, str):
            results.append(BlockTranslation(
                block_id=block_id,
                block_index=block_index,
                kind=kind,
                status='failed',
                source_text=text,
                translated_text='',
                budget=budget,
                error=(
                    'model call returned '
                    f'{type(response).__name__}, expected str'
                ),
            ))
            continue
        if not response.strip():
            results.append(BlockTranslation(
                block_id=block_id,
                block_index=block_index,
                kind=kind,
                status='failed',
                source_text=text,
                translated_text='',
                budget=budget,
                error='model call returned an empty translation',
            ))
            continue

        translated_text, glossary_hits = apply_glossary(response, terms)
        if not translated_text.strip():
            results.append(BlockTranslation(
                block_id=block_id,
                block_index=block_index,
                kind=kind,
                status='failed',
                source_text=text,
                translated_text=translated_text,
                budget=budget,
                glossary_hits=glossary_hits,
                error='glossary produced an empty translation',
            ))
            continue
        refusal = fit_refusal(
            block_id=block_id,
            block_index=block_index,
            kind=kind,
            block=block,
            band=band,
            translated_text=translated_text,
        )
        results.append(BlockTranslation(
            block_id=block_id,
            block_index=block_index,
            kind=kind,
            status='failed' if refusal is not None else 'translated',
            source_text=text,
            translated_text=translated_text,
            budget=budget,
            glossary_hits=glossary_hits,
            refusal=refusal,
        ))

    return PageTranslation(
        page_id=resolved_page_id,
        page_number=page_number,
        target_language=target_language,
        blocks=tuple(results),
    )


def _validated_glossary(
    glossary: Mapping[str, str] | None,
) -> dict[str, str] | None:
    if glossary is None:
        return None
    if not isinstance(glossary, Mapping):
        raise TypeError('glossary must be a mapping of source term to rendering')
    validated: dict[str, str] = {}
    for source_term, target_term in glossary.items():
        if not isinstance(source_term, str) or not source_term:
            raise ValueError('glossary keys must be non-empty strings')
        if not isinstance(target_term, str):
            raise ValueError(
                f'glossary value for {source_term!r} must be a string'
            )
        validated[source_term] = target_term
    return validated or None


def _page_number(page: Mapping[str, Any]) -> int:
    raw = page.get('page_number', 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TranslationInputError(f'page_number must be an int: {raw!r}')
    return raw


def _page_size(page: Mapping[str, Any]) -> tuple[float, float]:
    try:
        width = float(page['width'])
        height = float(page['height'])
    except (KeyError, TypeError, ValueError) as exc:
        raise TranslationInputError(
            'page width/height must be present and numeric'
        ) from exc
    if width <= 0 or height <= 0:
        raise TranslationInputError(
            f'page dimensions must be positive: {width!r} x {height!r}'
        )
    return width, height


def _page_columns(page: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    raw = page.get('columns', ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise TranslationInputError('page columns must be a sequence')
    return raw


def _page_blocks(page: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    raw = page.get('blocks')
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise TranslationInputError('page blocks must be a sequence')
    return raw


def _block_index(block: Mapping[str, Any], position: int) -> int:
    raw = block.get('index', position)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TranslationInputError(
            f'block index must be an int: {raw!r}'
        )
    return raw


def _reject_duplicate_indexes(blocks: Sequence[Mapping[str, Any]]) -> None:
    seen: set[int] = set()
    for position, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            raise TranslationInputError(f'block {position} is not a mapping')
        index = _block_index(block, position)
        if index in seen:
            raise TranslationInputError(f'duplicate block index {index}')
        seen.add(index)
