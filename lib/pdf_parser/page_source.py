"""Per-page born-digital vs scanned detection for PDF translation.

Responsibility
--------------
Decide, page by page, whether the extractor produced a real text layer. Only
the extractor's own output is inspected (text and text-block geometry); no OCR,
model call or image analysis participates in this classification. The
born-digital path keeps the layout-fidelity guarantee; a page without a
sufficient text layer is routed away from it and reported as
``scanned_unsupported`` so translation can never emit an empty result for it.

Entry points
------------
``extract_page_sources(pdf_bytes)`` returns one :class:`PageSource` per page.
``classify_page_text(page_index, text, ...)`` classifies an already-extracted
page. ``route_pages(pages)`` builds the machine-readable routing report.
``require_translation_text(page, text)`` is the boundary guard: it refuses a
scanned page (``scanned_unsupported``) and an empty source (``empty_source``).

Dependencies
------------
``lib.pdf_parser._common`` for the shared PyMuPDF lock and optional import.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.pdf_parser._common import HAS_PYMUPDF, PYMUPDF_LOCK

if HAS_PYMUPDF:  # pragma: no cover - the import is already resolved in _common
    from lib.pdf_parser._common import pymupdf
else:  # pragma: no cover - hosts without PyMuPDF cannot extract pages
    pymupdf = None

BORN_DIGITAL = 'born_digital'
SCANNED = 'scanned'
SCANNED_UNSUPPORTED = 'scanned_unsupported'
EMPTY_SOURCE = 'empty_source'

PAGE_READY = 'ready'

# Mirrors the document-level rule in ``lib/pdf_parser/core.py`` (``isScanned``
# is true below an average of 50 characters per page). Applied per page here:
# a page whose text layer holds fewer than this many non-whitespace characters
# is not a usable born-digital source for translation.
MIN_PAGE_TEXT_CHARS = 50

__all__ = [
    'BORN_DIGITAL',
    'EMPTY_SOURCE',
    'MIN_PAGE_TEXT_CHARS',
    'PAGE_READY',
    'PageSource',
    'PageTranslationRefused',
    'PageRouting',
    'SCANNED',
    'SCANNED_UNSUPPORTED',
    'ScannedPageUnsupported',
    'classify_page_text',
    'classify_structure',
    'extract_page_sources',
    'require_translation_text',
    'route_pages',
]


@dataclass(frozen=True, slots=True)
class PageSource:
    """What the extractor produced for one page."""

    page_index: int
    kind: str
    text_chars: int
    text_blocks: int
    text_coverage: float
    reason: str

    @property
    def is_born_digital(self) -> bool:
        return self.kind == BORN_DIGITAL

    @property
    def status(self) -> str:
        return PAGE_READY if self.is_born_digital else SCANNED_UNSUPPORTED


class PageTranslationRefused(RuntimeError):
    """A page may not enter the text-translation path."""

    def __init__(self, page_index: int, status: str, reason: str) -> None:
        super().__init__(f'page {page_index + 1}: {reason}')
        self.page_index = page_index
        self.status = status
        self.reason = reason


class ScannedPageUnsupported(PageTranslationRefused):
    """A scanned page needs an OCR path that the translator does not own."""

    def __init__(self, page_index: int, reason: str) -> None:
        super().__init__(page_index, SCANNED_UNSUPPORTED, reason)


@dataclass(frozen=True, slots=True)
class PageRouting:
    """Per-page routing plus the document-level status a caller must report."""

    pages: tuple[PageSource, ...]
    born_digital_pages: tuple[int, ...]
    scanned_pages: tuple[int, ...]

    @property
    def status(self) -> str:
        return SCANNED_UNSUPPORTED if self.scanned_pages else PAGE_READY

    def report(self) -> dict:
        """Machine-readable routing evidence for the translation task."""
        return {
            'status': self.status,
            'bornDigitalPages': list(self.born_digital_pages),
            'scannedPages': list(self.scanned_pages),
            'scannedStatus': (
                SCANNED_UNSUPPORTED if self.scanned_pages else None),
            'pages': [
                {
                    'page': page.page_index + 1,
                    'pageIndex': page.page_index,
                    'kind': page.kind,
                    'status': page.status,
                    'textChars': page.text_chars,
                    'textBlocks': page.text_blocks,
                    'textCoverage': page.text_coverage,
                    'reason': page.reason,
                }
                for page in self.pages
            ],
        }


def _count_text_chars(text: str) -> int:
    return sum(1 for character in text if not character.isspace())


def classify_page_text(
    page_index: int,
    text: str,
    *,
    text_blocks: int | None = None,
    text_coverage: float = 0.0,
) -> PageSource:
    """Classify one page from the extractor's text layer alone.

    ``text_blocks`` defaults to one block when the extractor returned text, so
    a caller holding only page text is not misclassified as scanned.
    """
    if isinstance(page_index, bool) or not isinstance(page_index, int) \
            or page_index < 0:
        raise ValueError('page_index must be a non-negative integer')
    if not isinstance(text, str):
        raise TypeError('text must be the extractor string for this page')
    characters = _count_text_chars(text)
    blocks = (1 if text.strip() else 0) if text_blocks is None else max(
        0, int(text_blocks))
    coverage = min(1.0, max(0.0, float(text_coverage)))
    if characters == 0:
        return PageSource(page_index, SCANNED, 0, blocks, coverage,
                          'no extractable text layer')
    if characters < MIN_PAGE_TEXT_CHARS:
        return PageSource(
            page_index, SCANNED, characters, blocks, coverage,
            f'only {characters} extractable characters; '
            f'minimum is {MIN_PAGE_TEXT_CHARS}')
    if blocks < 1:
        return PageSource(page_index, SCANNED, characters, blocks, coverage,
                          'text characters without a text block')
    return PageSource(page_index, BORN_DIGITAL, characters, blocks, coverage,
                      f'{characters} extractable characters in {blocks} block(s)')


def _source_from_page(page, page_index: int) -> PageSource:
    raw_blocks = page.get_text('blocks') or []
    text_blocks = []
    for block in raw_blocks:
        # (x0, y0, x1, y1, text, block_no, block_type); type 0 is text.
        if len(block) < 7 or int(block[6]) != 0:
            continue
        if str(block[4]).strip():
            text_blocks.append(block)
    text = page.get_text() or ''
    page_area = float(page.rect.width) * float(page.rect.height)
    covered = 0.0
    for x0, y0, x1, y1, *_rest in text_blocks:
        covered += max(0.0, float(x1) - float(x0)) * max(0.0, float(y1) - float(y0))
    coverage = covered / page_area if page_area > 0 else 0.0
    return classify_page_text(
        page_index, text,
        text_blocks=len(text_blocks), text_coverage=coverage)


def extract_page_sources(pdf_bytes: bytes) -> tuple[PageSource, ...]:
    """Read each page's text layer and classify it; never runs OCR.

    The caller receives only extractor output. PyMuPDF is an optional
    dependency, so its absence is an explicit error rather than an empty
    document: a caller must not mistake "cannot inspect" for "scanned".
    """
    if pymupdf is None:
        raise RuntimeError(
            'PyMuPDF is unavailable; page-source detection cannot run')
    with PYMUPDF_LOCK:
        document = pymupdf.open(stream=pdf_bytes, filetype='pdf')
        try:
            return tuple(
                _source_from_page(document[index], index)
                for index in range(document.page_count)
            )
        finally:
            document.close()


def classify_structure(structure: dict) -> PageRouting:
    """Classify the output of ``lib.pdf_parser.structure.extract_pdf_structure``.

    The structural extractor already names a page without a usable text layer.
    Its decision is the first gate; the per-page character rule refines pages
    that carry only a stray text line. Only extractor output is consulted.
    """
    sources: list[PageSource] = []
    for page in structure.get('pages') or []:
        page_index = int(page.get('page_number', len(sources)))
        text = page.get('text') or ''
        blocks = [
            block for block in (page.get('blocks') or [])
            if block.get('kind') not in ('figure', 'table')
            and str(block.get('text') or '').strip()
        ]
        page_area = float(page.get('width') or 0.0) * float(page.get('height') or 0.0)
        covered = 0.0
        for block in blocks:
            bbox = block.get('bbox') or (0.0, 0.0, 0.0, 0.0)
            covered += max(0.0, float(bbox[2]) - float(bbox[0])) * max(
                0.0, float(bbox[3]) - float(bbox[1]))
        coverage = covered / page_area if page_area > 0 else 0.0
        if not page.get('supported', True):
            reason = str(page.get('unsupported_reason')
                         or 'extractor reported no usable text layer')
            sources.append(PageSource(
                page_index, SCANNED,
                sum(1 for character in text if not character.isspace()),
                len(blocks), min(1.0, coverage), reason))
            continue
        sources.append(classify_page_text(
            page_index, text,
            text_blocks=len(blocks), text_coverage=coverage))
    return route_pages(sources)


def route_pages(pages: tuple[PageSource, ...] | list[PageSource]) -> PageRouting:
    """Split extractor classifications into the two translation paths."""
    ordered = tuple(pages)
    born_digital = tuple(page.page_index for page in ordered
                         if page.is_born_digital)
    scanned = tuple(page.page_index for page in ordered
                    if not page.is_born_digital)
    return PageRouting(ordered, born_digital, scanned)


def require_translation_text(page: PageSource, text: str) -> str:
    """Return ``text`` only for a usable born-digital page.

    This is the path boundary that prevents a scanned page or an empty source
    from being silently translated as an empty string.
    """
    if not page.is_born_digital:
        raise ScannedPageUnsupported(
            page.page_index,
            'page has no sufficient extractable text layer; '
            f'reported as {SCANNED_UNSUPPORTED}')
    if not isinstance(text, str) or not text.strip():
        raise PageTranslationRefused(
            page.page_index, EMPTY_SOURCE,
            'born-digital page produced no translation source text')
    return text