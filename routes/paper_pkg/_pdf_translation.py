"""Paper routes — full-document PDF translation through Tofu's own pipeline.

``POST /api/v1/paper/translate/pdf`` resolves the owner's stored source PDF and
submits it to :func:`lib.paper.builds.pdf_translation.translate_pdf`; the
translated document is returned as ``application/pdf``. The pipeline owns
extraction, block translation, re-layout, rendering and the translation-mode
fidelity gate; this route owns only owner scoping, request parsing and the
binary response contract.

``GET /api/v1/paper/translate/pdf/status`` reports whether a translation
transport is configured. Absence is explicit: the POST answers 409 before any
pipeline work, and the browser entry disables itself instead of pretending.
"""

from __future__ import annotations

import asyncio
import os
import re

from quart import Response

from lib.api_response import (
    api_bad_request,
    api_error,
    api_not_found,
    api_ok,
)
from lib.api_v4 import raw_success_response
from lib.log import get_logger
from lib.openapi import media_responses, api_meta
from lib.paper.builds.pdf_translation import (
    TranslationPipelineError,
    translate_pdf,
)
from lib.paper.builds.pdf_translation_transport import (
    pdf_translation_configured,
    resolve_pdf_translator,
)
from lib.paper.library_repository import PaperLibraryRepository
from lib.paper_identity import PAPER_DIR
from lib.request_parser import async_parse_body
from routes.api_v1.auth import request_user_id
from routes.paper_pkg._common import api_v1_paper_bp

logger = get_logger(__name__)

_MAX_TARGET_LANGUAGE_CHARS = 32
_FILENAME_UNSAFE = re.compile(r'[^A-Za-z0-9_.-]+')


def _load_owned_pdf(
    owner_user_id: int, paper_id: str, paper_hash: str,
) -> bytes | None:
    """Read the exact source PDF an owner selected, or ``None``.

    The library lookup is the authority: the caller's filename never becomes a
    path. A supplied hash must match the stored identity when both are present.
    """
    entry = PaperLibraryRepository(owner_user_id).get(paper_id)
    if entry is None:
        return None
    if paper_hash and entry.paper_hash and entry.paper_hash != paper_hash:
        return None
    filename = os.path.basename(entry.pdf_filename or '')
    if not filename:
        return None
    path = os.path.join(PAPER_DIR, filename)
    try:
        with open(path, 'rb') as stream:
            return stream.read()
    except OSError:
        return None


def _resolve_pdf_translator(owner_user_id: int):
    """Route-local seam; tests inject the offline fake translator here."""
    return resolve_pdf_translator(owner_user_id=owner_user_id)


def _pdf_translation_configured() -> bool:
    return pdf_translation_configured()


@api_v1_paper_bp.route(
    '/api/v1/paper/translate/pdf/status', methods=['GET'])
async def paper_pdf_translation_status():
    """Report whether this server can translate a full document.

    Returns: ``{ok: true, configured: bool, reason: str}``. ``reason`` is an
    empty string when configured, else the stable code
    ``no_text_model_transport``. It never probes a provider.
    """
    configured = await asyncio.to_thread(_pdf_translation_configured)
    return api_ok({
        'configured': bool(configured),
        'reason': '' if configured else 'no_text_model_transport',
    })


@api_v1_paper_bp.route('/api/v1/paper/translate/pdf', methods=['POST'])
@api_meta(responses=media_responses('application/pdf'))
@raw_success_response
async def translate_paper_pdf():
    """Translate one owner-scoped stored PDF and return the rendered document.

    Body JSON:
        paper_id: str — the library row whose stored PDF is the source.
        paper_hash: str (optional) — content identity check against the row.
        target_language: str — the language name or code passed to the
            pipeline's translator prompt (e.g. ``zh``, ``English``).

    Success: ``200 application/pdf`` with the translated bytes and
    ``X-Tofu-Translation-Pages`` / ``-Translated`` / ``-Refused`` / ``-Gate``
    summary headers. Failure modes are explicit: 400 missing/invalid input,
    404 no owner-scoped stored PDF, 409 no configured translation transport,
    422 unusable source or a translated document that failed the fidelity gate.
    """
    owner_user_id = int(request_user_id())
    data = await async_parse_body(
        exact=('paper_hash', 'paper_id', 'target_language',))
    paper_id = str(data.get('paper_id') or '').strip()
    paper_hash = str(data.get('paper_hash') or '').strip()
    target_language = str(data.get('target_language') or '').strip()
    if not paper_id:
        return api_bad_request('paper_id required')
    if not target_language:
        return api_bad_request('target_language required')
    if len(target_language) > _MAX_TARGET_LANGUAGE_CHARS:
        return api_bad_request(
            f'target_language exceeds {_MAX_TARGET_LANGUAGE_CHARS} characters')

    translator = await asyncio.to_thread(_resolve_pdf_translator, owner_user_id)
    if translator is None:
        return api_error(
            'No translation transport is configured on this server, so the '
            'document cannot be translated.',
            status=409,
        )

    source_pdf = await asyncio.to_thread(
        _load_owned_pdf, owner_user_id, paper_id, paper_hash)
    if source_pdf is None:
        return api_not_found('Stored PDF not found for this paper')

    try:
        result = await asyncio.to_thread(
            translate_pdf, source_pdf, target_language, translate=translator)
    except TranslationPipelineError as exc:
        return api_error(str(exc), status=422)
    except (TypeError, ValueError) as exc:
        return api_bad_request(str(exc))

    if not result.pdf_bytes:
        return api_error('Translation produced no PDF document', status=422)
    if not result.ok:
        failures = list(result.failures)
        detail = (
            'The translated document did not pass the fidelity gate '
            f'({len(failures)} failure(s))')
        if failures:
            first = failures[0]
            detail += f": page {first.get('page')} check {first.get('check')}"
        return api_error(detail, status=422)

    summary = result.summary
    safe_language = _FILENAME_UNSAFE.sub('_', target_language)[:32] or 'target'
    identity = (paper_hash or paper_id)[:8] or 'paper'
    response = Response(
        result.pdf_bytes, status=200, mimetype='application/pdf')
    response.headers['Content-Disposition'] = (
        f'inline; filename="paper-{identity}-{safe_language}.pdf"')
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Tofu-Translation-Pages'] = str(
        len(result.page_reports))
    response.headers['X-Tofu-Translation-Translated'] = str(
        summary.get('translated', 0))
    response.headers['X-Tofu-Translation-Refused'] = str(
        summary.get('refused', 0))
    response.headers['X-Tofu-Translation-Gate'] = 'pass'
    logger.info(
        '[Paper:PDFTranslate] owner=%s paper=%s lang=%s pages=%d '
        'translated=%d refused=%d',
        owner_user_id, paper_id, target_language, len(result.page_reports),
        summary.get('translated', 0), summary.get('refused', 0))
    return response


__all__ = ['translate_paper_pdf']
