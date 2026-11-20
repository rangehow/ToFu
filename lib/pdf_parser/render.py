"""Paint a solved PDF placement plan onto a born-digital source page.

Entry point: :func:`render_page_plan`.  Input is the original PDF bytes plus
the :class:`~lib.pdf_relayout.model.PagePlan` produced by
:func:`lib.pdf_relayout.solver.solve_page`; output is a one-page PDF whose

* non-text content (images, vector figures, rules, table rulings) is the
  source page's own content, copied at its original position and size;
* text blocks are repainted at the plan's exact baselines, font family, size
  and leading, inside the plan's (unchanged) columns;
* running heads/folios, extraction kinds the solver does not own, and blocks
  the solver refused keep their source text instead of disappearing.

A refusal is returned in :class:`RenderReport.refusals`; it is never silently
dropped.  The function reads no model, uses no network and performs no OCR.
Only locally installed faces named by the plan's ``TextRun`` are embedded; a
missing face is a typed error rather than a substituted family.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:  # PyMuPDF >= 1.24.3, with the legacy module name as a fallback.
    import pymupdf
except ImportError:  # pragma: no cover - the project declares PyMuPDF
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            'lib.pdf_parser.render requires the PyMuPDF package; it is a '
            'declared dependency and is never installed implicitly.'
        ) from exc

from lib.pdf_relayout import (
    BlockPlacement,
    LayoutRefusal,
    PagePlan,
    PlacedLine,
)

_PAGE_ID_RE = re.compile(r'(\d+)\s*$')
_PAGE_SIZE_TOLERANCE_PT = 0.5
_MATCH_TOP_TOLERANCE_PT = 3.0
_REDACT_MARGIN_PT = 1.0


class RenderInputError(ValueError):
    """The source PDF, plan or page selection cannot be rendered."""


class RenderFontError(RuntimeError):
    """A plan run names a face that cannot be embedded as-is."""


class RenderInvariantError(RuntimeError):
    """A placement would draw outside its column or the page."""


@dataclass(frozen=True, slots=True)
class PreservedSourceText:
    """Source text kept verbatim because the plan does not own it."""

    bbox: tuple[float, float, float, float]
    text: str
    reason: str


@dataclass(frozen=True, slots=True)
class RenderReport:
    """Result of one page render, including every refusal it carried."""

    page_index: int
    pdf_bytes: bytes
    drawn_blocks: tuple[str, ...]
    preserved_source_text: tuple[PreservedSourceText, ...]
    refusals: tuple[LayoutRefusal, ...]
    fonts: tuple[tuple[str, str], ...]

    @property
    def accepted(self) -> bool:
        return not self.refusals


@dataclass(frozen=True, slots=True)
class _SourceTextBlock:
    bbox: tuple[float, float, float, float]
    lines: tuple[tuple[float, float, float, float], ...]
    text: str


def render_page_plan(
    source_pdf: bytes,
    plan: PagePlan,
    output_path: str | os.PathLike[str] | None = None,
    *,
    page_index: int | None = None,
) -> RenderReport:
    """Render ``plan`` onto the matching source page and write a new PDF.

    ``output_path`` is written atomically when given; the bytes are also
    returned in the report so callers can feed them straight to the fidelity
    gate.  ``page_index`` overrides the index derived from ``plan.page_id``.
    """
    if not isinstance(source_pdf, (bytes, bytearray, memoryview)):
        raise RenderInputError('source_pdf must be PDF bytes')
    payload = bytes(source_pdf)
    try:
        source = pymupdf.open(stream=payload, filetype='pdf')
    except Exception as exc:
        raise RenderInputError(f'not a readable PDF: {exc}') from exc
    if source.needs_pass:
        source.close()
        raise RenderInputError('password-protected PDFs are not supported')

    try:
        index = _resolve_page_index(plan, page_index, source.page_count)
        rendered = pymupdf.open()
        try:
            rendered.insert_pdf(source, from_page=index, to_page=index)
            page = rendered[0]
            _validate_page(page, plan, index)
            protected = _fixed_rects(plan)
            flowable = _flowable_placements(plan, index)
            text_blocks = _source_text_blocks(page)
            preserved = _redact_source_text(page, text_blocks, protected, flowable)
            fonts = _draw_placements(page, plan, index)
            data = rendered.tobytes(garbage=3, deflate=True)
        finally:
            rendered.close()
    finally:
        source.close()

    if output_path is not None:
        _write_atomic(Path(output_path), data)

    return RenderReport(
        page_index=index,
        pdf_bytes=data,
        drawn_blocks=tuple(
            placement.block_id for placement in plan.placements
            if placement.lines
        ),
        preserved_source_text=tuple(preserved),
        refusals=tuple(plan.refusals),
        fonts=tuple(sorted(fonts.items())),
    )


# ── plan / page checks ────────────────────────────────────────────────────


def _resolve_page_index(plan: PagePlan, explicit: int | None,
                        page_count: int) -> int:
    if explicit is None:
        match = _PAGE_ID_RE.search(plan.page_id or '')
        explicit = int(match.group(1)) if match else 0
    if not isinstance(explicit, int) or not 0 <= explicit < page_count:
        raise RenderInputError(
            f'page index {explicit!r} is outside the source document '
            f'({page_count} page(s))'
        )
    return explicit


def _validate_page(page, plan: PagePlan, index: int) -> None:
    width, height = page.rect.width, page.rect.height
    if (abs(width - plan.page.width) > _PAGE_SIZE_TOLERANCE_PT
            or abs(height - plan.page.height) > _PAGE_SIZE_TOLERANCE_PT):
        raise RenderInputError(
            f'plan page {plan.page_id!r} is {plan.page.width:.2f}x'
            f'{plan.page.height:.2f}pt but source page {index} is '
            f'{width:.2f}x{height:.2f}pt'
        )
    refused = {refusal.block_id for refusal in plan.refusals}
    for placement in plan.placements:
        if placement.block_id in refused:
            raise RenderInvariantError(
                f'block {placement.block_id!r} is both placed and refused'
            )


def _fixed_rects(plan: PagePlan) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        (placement.bbox.x0, placement.bbox.y0,
         placement.bbox.x1, placement.bbox.y1)
        for placement in plan.placements
        if placement.preserved_geometry
    )


def _flowable_placements(plan: PagePlan, index: int) -> tuple[BlockPlacement, ...]:
    columns = {column.column_id: column for column in plan.columns}
    placements = []
    for placement in plan.placements:
        if placement.preserved_geometry or not placement.lines:
            continue
        column = columns.get(placement.column)
        if column is None:
            raise RenderInvariantError(
                f'block {placement.block_id!r} names unknown column '
                f'{placement.column!r} on page {index}'
            )
        for line in placement.lines:
            _check_line_in_column(placement, line, column)
        placements.append(placement)
    return tuple(placements)


def _check_line_in_column(placement: BlockPlacement, line: PlacedLine,
                          column) -> None:
    band = column.bbox
    box = line.bbox
    if (box.x0 < band.x0 - _PAGE_SIZE_TOLERANCE_PT
            or box.x1 > band.x1 + _PAGE_SIZE_TOLERANCE_PT
            or box.y0 < band.y0 - _PAGE_SIZE_TOLERANCE_PT
            or box.y1 > band.y1 + _PAGE_SIZE_TOLERANCE_PT):
        raise RenderInvariantError(
            f'line of block {placement.block_id!r} escapes column '
            f'{placement.column!r}: {box!r} not inside {band!r}'
        )
    for run in line.runs:
        if (run.x0 < box.x0 - _PAGE_SIZE_TOLERANCE_PT
                or run.x1 > box.x1 + _PAGE_SIZE_TOLERANCE_PT):
            raise RenderInvariantError(
                f'run of block {placement.block_id!r} escapes its line: '
                f'{run.x0:.2f}..{run.x1:.2f} vs {box.x0:.2f}..{box.x1:.2f}'
            )


# ── source text removal ───────────────────────────────────────────────────


def _source_text_blocks(page) -> tuple[_SourceTextBlock, ...]:
    blocks: list[_SourceTextBlock] = []
    for raw in page.get_text('rawdict').get('blocks', []):
        if raw.get('type', 0) != 0:
            continue
        lines: list[tuple[float, float, float, float]] = []
        text_parts: list[str] = []
        for line in raw.get('lines', []):
            spans = [
                span for span in line.get('spans', [])
                if ''.join(char.get('c', '') for char in span.get('chars', [])).strip()
            ]
            if not spans:
                continue
            lines.append(tuple(float(value) for value in line['bbox']))
            text_parts.append(''.join(
                char.get('c', '') for span in spans for char in span.get('chars', [])
            ))
        if not lines:
            continue
        bbox = tuple(float(value) for value in raw['bbox'])
        blocks.append(_SourceTextBlock(bbox=bbox, lines=tuple(lines),
                                       text=''.join(text_parts)))
    return tuple(blocks)


def _redact_source_text(page, blocks: tuple[_SourceTextBlock, ...],
                        protected, flowable) -> list[PreservedSourceText]:
    """Remove source glyphs only where a placed block replaces them."""
    preserved: list[PreservedSourceText] = []
    redact = False
    for block in blocks:
        if _inside_any(block, protected):
            preserved.append(_preserved(block, 'inside a fixed figure/table'))
            continue
        placement = _matching_placement(block, flowable)
        if placement is None:
            preserved.append(_preserved(
                block, 'no placed block owns this source text'))
            continue
        for x0, y0, x1, y1 in block.lines:
            rect = pymupdf.Rect(x0 - _REDACT_MARGIN_PT, y0 - _REDACT_MARGIN_PT,
                                x1 + _REDACT_MARGIN_PT, y1 + _REDACT_MARGIN_PT)
            page.add_redact_annot(rect, fill=None, cross_out=False)
            redact = True
    if redact:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    return preserved


def _inside_any(block: _SourceTextBlock, rects) -> bool:
    x0, y0, x1, y1 = block.bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return any(rx0 <= cx <= rx1 and ry0 <= cy <= ry1
               for rx0, ry0, rx1, ry1 in rects)


def _matching_placement(block: _SourceTextBlock,
                        placements: tuple[BlockPlacement, ...]):
    x0, y0, x1, _y1 = block.bbox
    for placement in placements:
        # A length-compensating reflow moves the translated block down the
        # column; its source glyphs are still at ``source_bbox``.
        box = placement.source_bbox or placement.bbox
        overlap = min(x1, box.x1) - max(x0, box.x0)
        if overlap <= 0.5:
            continue
        if abs(y0 - box.y0) <= _MATCH_TOP_TOLERANCE_PT:
            return placement
    return None


def _preserved(block: _SourceTextBlock, reason: str) -> PreservedSourceText:
    return PreservedSourceText(bbox=block.bbox, text=block.text, reason=reason)


# ── text painting ─────────────────────────────────────────────────────────


def _draw_placements(page, plan: PagePlan, index: int) -> dict[str, str]:
    placements = [
        placement for placement in plan.placements
        if placement.lines and placement.block_id not in
        {refusal.block_id for refusal in plan.refusals}
    ]
    if not placements:
        return {}
    writer = pymupdf.TextWriter(page.rect, color=(0, 0, 0))
    fonts: dict[str, str] = {}
    for placement in placements:
        for line in placement.lines:
            if not (0 <= line.baseline_y <= page.rect.height + 0.5):
                raise RenderInvariantError(
                    f'block {placement.block_id!r} baseline '
                    f'{line.baseline_y:.2f} is outside page {index}'
                )
            for run in line.runs:
                if not run.text.strip():
                    continue
                font = _font_for(run.font_path, run.font_family)
                fonts.setdefault(run.font_path, run.font_family)
                writer.append((run.x0, line.baseline_y), run.text,
                              font=font, fontsize=run.font_size_pt)
    writer.write_text(page, color=(0, 0, 0), overlay=True)
    return fonts


def _font_for(path: str, family: str):
    if not path or not os.path.isfile(path):
        raise RenderFontError(
            f'plan run names a missing font file {path!r} for family '
            f'{family!r}; no family was substituted'
        )
    try:
        font = pymupdf.Font(fontfile=path)
    except Exception as exc:
        raise RenderFontError(
            f'cannot embed font {path!r} for family {family!r}: {exc}'
        ) from exc
    if _family_key(font.name) != _family_key(family):
        raise RenderFontError(
            f'font file {path!r} reports family {font.name!r}, not the '
            f'planned {family!r}; refusing to draw a different face'
        )
    return font


_STYLE_TOKENS = frozenset({
    'regular', 'book', 'roman', 'normal', 'bold', 'italic', 'oblique',
    'light', 'medium', 'semibold', 'demibold', 'black', 'thin',
    'extrabold', 'extralight',
})


def _family_key(name: str) -> str:
    """Family comparison key without whitespace, punctuation or style suffix.

    PyMuPDF reports an embedded face's full name (``DejaVu Sans Book``) while
    the solver names the family (``DejaVu Sans``).  Removing a trailing style
    token compares the family, never a different weight or width family.
    """
    words = re.findall(r'[a-z0-9]+', (name or '').casefold())
    while len(words) > 1 and words[-1] in _STYLE_TOKENS:
        words.pop()
    return ''.join(words)


# ── output ────────────────────────────────────────────────────────────────


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise