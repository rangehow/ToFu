"""lib/pdf_parser/_common.py — Shared constants and initialization for PDF parsing."""

import importlib
import os
import threading

from lib.log import get_logger

logger = get_logger(__name__)


# ── ORT / OpenMP thread counts (must be set BEFORE ``import docling``) ──
# Docling pulls in onnxruntime, which by default spawns one worker per
# host CPU and pins each one to a physical core via
# ``pthread_setaffinity_np``. On cgroup/cpuset-restricted hosts (k8s
# pods, YARN/Hope containers, ``taskset``-launched jobs) most of those
# cores are NOT in the process's allowed set → ``EINVAL`` storm in
# stderr. Specifying the thread count explicitly (every allowed core)
# skips the affinity loop. The per-converter count also goes through
# Docling's ``AcceleratorOptions(num_threads=...)`` in
# ``lib/pdf_parser/docling.py``; this is the belt-and-braces early hint.
def _allowed_cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError) as _e_audit:
        logger.debug('[_common] _allowed_cpu_count caught %s: %s', type(_e_audit).__name__, _e_audit)
        return max(1, os.cpu_count() or 1)


_thread_hint = os.environ.get('TOFU_DOCLING_THREADS', '').strip()
if not _thread_hint:
    _thread_hint = str(_allowed_cpu_count())
for _var in ('OMP_NUM_THREADS', 'ORT_NUM_THREADS',
             'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS'):
    os.environ.setdefault(_var, _thread_hint)

# Keep pymupdf4llm on the classic implementation Tofu actually calls.  Do not
# import onnxruntime merely to install a future guard: importing ORT itself
# retains a large native pool on this platform.  Docling installs that guard at
# its real first use instead (lib/pdf_parser/docling.py::_get_converter).
from runtime_guards import install_pymupdf_classic_policy
install_pymupdf_classic_policy()

__all__ = ['HAS_PYMUPDF4LLM', 'HAS_PYMUPDF', 'HAS_DOCLING',
           'PYMUPDF_LOCK', 'PYMUPDF4LLM_UNAVAILABLE_REASON',
           'current_parser_version', 'expected_parser_version',
           'import_preserving_quad_corrections', 'pymupdf4llm_available',
           'pymupdf4llm_module', 'pymupdf4llm_status']

# PyMuPDF's C library (MuPDF) is NOT thread-safe. The official docs state:
# "PyMuPDF does not support running on multiple threads - doing so may cause
# incorrect behaviour or even crash Python itself."
# All pymupdf operations MUST be serialized behind this lock.
PYMUPDF_LOCK = threading.Lock()


# ─── PyMuPDF (core PDF engine) ───
# PyMuPDF exposes its top-level package as ``pymupdf`` since 1.24.3; older
# installs (e.g. 1.24.1) ship ONLY the legacy ``fitz`` module name. Importing
# solely as ``pymupdf`` silently disabled PDF parsing on a host that actually
# had the library (real R4 harvest run: every parse raised
# ``'NoneType' has no attribute 'open'`` because HAS_PYMUPDF was False). Try
# the modern name, then fall back to ``fitz`` — same C library, identical API
# (open / TOOLS.mupdf_display_*).
try:
    import pymupdf
    HAS_PYMUPDF = True
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
except ImportError:
    try:
        import fitz as pymupdf  # PyMuPDF <1.24.3 legacy module name
        HAS_PYMUPDF = True
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
        logger.info('[PDF] pymupdf imported via legacy "fitz" module name '
                    '(PyMuPDF <1.24.3)')
    except ImportError as e:
        pymupdf = None  # type: ignore[assignment]
        HAS_PYMUPDF = False
        logger.warning('[PDF] pymupdf/fitz not installed — PDF parsing disabled: %s', e)

# ─── pymupdf4llm (preferred for table/header-aware extraction) ───
# Distinguish two failure modes that BOTH surface as ImportError so the log
# tells the truth (per CLAUDE.md §2.2 — this is a recoverable degradation to
# raw-text extraction, so warning level is correct):
#   • genuinely not installed  → no module spec on the path
#   • installed but ABI/version-incompatible (e.g. pymupdf4llm pins a newer
#     pymupdf than is installed) → the package's __init__ raises ImportError
#     with a version message. Reporting this as "not installed" sent us
#     chasing a phantom missing dep; it's actually a pin mismatch to fix.
# PYMUPDF4LLM_UNAVAILABLE_REASON is '' when available, else a human string
# beginning with 'version/ABI mismatch' or 'not installed'.
PYMUPDF4LLM_UNAVAILABLE_REASON = ''


def _diagnose_pymupdf4llm_failure(exc: ImportError, installed: bool) -> str:
    """Classify why ``import pymupdf4llm`` raised ImportError.

    Args:
        exc: The ImportError raised by the import.
        installed: Whether a module spec for ``pymupdf4llm`` exists on the
            path (True ⇒ the package IS present, so the failure is a
            version/ABI incompatibility raised by its ``__init__``; False ⇒
            genuinely not installed).

    Returns:
        A human-readable reason string prefixed with ``'version/ABI mismatch'``
        or ``'not installed'`` so callers can branch on the cause.
    """
    if installed:
        return f'version/ABI mismatch: {exc}'
    return f'not installed: {exc}'


def _pymupdf4llm_installed() -> bool:
    """True if a module spec for pymupdf4llm exists (independent of whether
    it imports cleanly)."""
    import importlib.util as _ilu
    try:
        return _ilu.find_spec('pymupdf4llm') is not None
    except (ImportError, ValueError) as e:
        logger.debug('[_common] find_spec(pymupdf4llm) failed: %s', e)
        return False


# IMPORTANT: never import pymupdf4llm at module import time.
#
# pymupdf4llm.helpers.pymupdf_rag / document_layout call
# ``pymupdf.TOOLS.unset_quad_corrections(True)`` while their module body runs.
# That is a process-global MuPDF switch: it changes ascender/descender
# geometry for EVERY later ``page.get_text``/``find_tables`` call in the
# process, so merely importing this domain corrupted table cell text in
# unrelated PDF paths. Availability is probed here with ``find_spec`` only;
# the module itself is imported by :func:`pymupdf4llm_module` at the call
# sites that actually need Markdown extraction.
HAS_PYMUPDF4LLM = _pymupdf4llm_installed()
PYMUPDF4LLM_UNAVAILABLE_REASON = (
    '' if HAS_PYMUPDF4LLM
    else 'not installed: no module spec for pymupdf4llm')

_pymupdf4llm_module = None
_pymupdf4llm_resolved = False
# Reentrant: ``pymupdf4llm_module`` and the classic-rag loader in text.py both
# snapshot/restore the MuPDF flag around their import.
_PYMUPDF4LLM_IMPORT_LOCK = threading.RLock()


def import_preserving_quad_corrections(module_name: str):
    """Import ``module_name`` without leaking MuPDF's global quad flag.

    pymupdf4llm's helpers disable quad corrections (a process-global MuPDF
    setting) at import. The flag has no setter beyond
    ``TOOLS.unset_quad_corrections``; the no-argument call returns its current
    value without modifying it, so the prior state can be captured and
    restored once the import — including a failed one that leaves a partially
    initialized package behind — completes.

    Returns the imported module. Raises whatever the import raises, after the
    caller's flag state has been restored.
    """
    with _PYMUPDF4LLM_IMPORT_LOCK:
        if not HAS_PYMUPDF or not hasattr(pymupdf.TOOLS,
                                          'unset_quad_corrections'):
            return importlib.import_module(module_name)
        previous = pymupdf.TOOLS.unset_quad_corrections()
        try:
            return importlib.import_module(module_name)
        finally:
            pymupdf.TOOLS.unset_quad_corrections(previous)


def pymupdf4llm_module():
    """Return the imported ``pymupdf4llm`` module, or None if unavailable.

    Imports lazily on first use. ``HAS_PYMUPDF4LLM`` and
    ``PYMUPDF4LLM_UNAVAILABLE_REASON`` are refined from the cheap spec probe
    to the real outcome here (an installed-but-ABI-incompatible package fails
    only when imported).
    """
    global HAS_PYMUPDF4LLM, PYMUPDF4LLM_UNAVAILABLE_REASON
    global _pymupdf4llm_module, _pymupdf4llm_resolved
    if _pymupdf4llm_resolved:
        return _pymupdf4llm_module
    with _PYMUPDF4LLM_IMPORT_LOCK:
        if _pymupdf4llm_resolved:
            return _pymupdf4llm_module
        try:
            module = import_preserving_quad_corrections('pymupdf4llm')
            HAS_PYMUPDF4LLM = True
            PYMUPDF4LLM_UNAVAILABLE_REASON = ''
        except ImportError as e:
            module = None
            HAS_PYMUPDF4LLM = False
            PYMUPDF4LLM_UNAVAILABLE_REASON = _diagnose_pymupdf4llm_failure(
                e, _pymupdf4llm_installed())
            if PYMUPDF4LLM_UNAVAILABLE_REASON.startswith('version'):
                logger.warning('[PDF] pymupdf4llm installed but failed to '
                               'import (version/ABI mismatch) — Markdown PDF '
                               'extraction disabled, falling back to raw '
                               'text. Pin a compatible pymupdf: %s', e)
            else:
                logger.warning('[PDF] pymupdf4llm not installed — Markdown '
                               'PDF extraction disabled: %s', e)
        _pymupdf4llm_module = module
        _pymupdf4llm_resolved = True
        return module


def pymupdf4llm_available() -> bool:
    """True when pymupdf4llm can actually be imported in this environment."""
    return pymupdf4llm_module() is not None


def pymupdf4llm_status() -> tuple[bool, str]:
    """Resolved (available, unavailable_reason) pair for diagnostics."""
    available = pymupdf4llm_module() is not None
    return available, ('' if available else PYMUPDF4LLM_UNAVAILABLE_REASON)

# ─── Docling (optional, OPT-IN — used by mode='structured') ───
# Heavy dep (~2 GB with torch). NOT auto-installed; user opts in via
# `pip install docling` or `install.sh --with-docling`. When present,
# `extract_pdf_text(..., mode='structured')` uses Docling's layout-aware
# pipeline (TableFormer + an internal equation model) for noticeably
# better tables/formulas vs. pymupdf4llm. Silent at import-time when
# missing — the structured-mode call site emits a single info-level
# hint on first use so users know how to enable it.
# Presence detection must not import this ~2 GB optional stack.  Its actual
# import, model construction and possible download belong exclusively to an
# explicit ``mode='structured'`` call.
import importlib.util as _importlib_util
try:
    HAS_DOCLING = _importlib_util.find_spec('docling') is not None
except (ImportError, ValueError) as e:
    logger.debug('[_common] docling presence probe failed: %s', e)
    HAS_DOCLING = False


# ─── Parse-once cache key: which extractor + version produced a parsed_text ───
# The paper_library corpus is a LONG-LIVED asset (auto-research R1 contract,
# docs/modules/ingest_media.md): a row written by a degraded or older
# parser must NOT be served as a cache hit forever. Every writer of
# paper_library.parsed_text stamps `parser_version` with the string below;
# the harvest pre-download probe requires an exact match with
# expected_parser_version(). A parser-stack upgrade — or a raw-fallback write
# — therefore invalidates naturally instead of freezing degraded text into
# the corpus. Legacy rows written before this contract have '' and miss by
# construction (self-heal on next touch, no backfill script).
def current_parser_version(extractor: str) -> str:
    """Map the extractor tag that ACTUALLY produced a parsed_text (the
    per-document winner reported by ``extract_pdf_text_with_meta``) to its
    versioned cache-key string, e.g. ``pymupdf4llm-1.27.2.3`` or
    ``pymupdf-raw-1.27.2.3``. Unknown tags pass through unchanged so a
    foreign value never accidentally equals the expected one."""
    tag = (extractor or '').strip()
    if not tag or tag == 'error':
        return ''
    import importlib.metadata as _md
    pkg = {'pymupdf4llm': 'pymupdf4llm',
           'pymupdf4llm-partial': 'pymupdf4llm',
           'pymupdf-raw': 'pymupdf',
           'pymupdf_raw': 'pymupdf',
           'docling': 'docling'}.get(tag)
    if pkg is None:
        return tag
    canon = 'pymupdf-raw' if tag == 'pymupdf_raw' else tag
    try:
        return f'{canon}-{_md.version(pkg)}'
    except _md.PackageNotFoundError as _e:
        logger.debug('current parser version: PackageNotFoundError (%s)', _e)
        return f'{canon}-?'


def expected_parser_version() -> str:
    """The ``parser_version`` a FRESH rich-mode parse would write in this
    environment — the value the harvest cache probe requires. In a healthy
    env this is the pymupdf4llm markdown pipeline; when the markdown
    extractor is unavailable it is the raw-fallback tag, so a degraded env
    stays self-consistent (raw rows hit) while a FIXED env never serves
    raw-tagged rows as markdown-corpus hits."""
    return current_parser_version(
        'pymupdf4llm' if pymupdf4llm_available() else 'pymupdf-raw')
