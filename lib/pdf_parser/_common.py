"""lib/pdf_parser/_common.py — Shared constants and initialization for PDF parsing."""

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['MAX_PDF_BYTES', 'HAS_PYMUPDF4LLM', 'HAS_PYMUPDF', 'HAS_DOCLING']

MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB safety limit

# ─── PyMuPDF (core PDF engine) ───
try:
    import pymupdf
    HAS_PYMUPDF = True
    # Suppress noisy MuPDF C-library warnings — they are harmless;
    # MuPDF recovers gracefully.
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
except ImportError as e:
    pymupdf = None  # type: ignore[assignment]
    HAS_PYMUPDF = False
    logger.warning('[PDF] pymupdf not installed — PDF parsing disabled: %s', e)

# ─── pymupdf4llm (preferred for table/header-aware extraction) ───
try:
    import pymupdf4llm  # noqa: F401
    HAS_PYMUPDF4LLM = True
except ImportError as e:
    pymupdf4llm = None  # type: ignore[assignment]
    HAS_PYMUPDF4LLM = False
    logger.warning('[PDF] pymupdf4llm not installed — Markdown PDF extraction disabled: %s', e)

# ─── Docling (optional, OPT-IN — used by mode='structured') ───
# Heavy dep (~2 GB with torch). NOT auto-installed; user opts in via
# `pip install docling` or `install.sh --with-docling`. When present,
# `extract_pdf_text(..., mode='structured')` uses Docling's layout-aware
# pipeline (TableFormer + an internal equation model) for noticeably
# better tables/formulas vs. pymupdf4llm. Silent at import-time when
# missing — the structured-mode call site emits a single info-level
# hint on first use so users know how to enable it.
try:
    import docling  # noqa: F401
    HAS_DOCLING = True
except ImportError:
    docling = None  # type: ignore[assignment]
    HAS_DOCLING = False
    # Intentionally silent on import — Docling is opt-in. We don't want
    # to spam the log on every server start when the user never asked
    # for the structured mode in the first place.
