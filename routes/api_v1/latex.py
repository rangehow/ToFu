"""routes/api_v1/latex.py — Live LOCAL LaTeX PDF preview for the chat pane.

The local twin of routes/api_v1/overleaf.py: when an agent edit lands on a
``.tex`` file inside a workspace root (no Overleaf involved), the preview
panel posts here; the route compiles the paper with the host TeX engine,
locates the edited line via SyncTeX, and the panel re-renders scrolled to
that spot.

Routes:
  POST /api/v1/latex/preview                 — compile + locate; pdf_url in body
  GET  /api/v1/latex/preview/<pdf_key>/pdf   — serve the cached PDF bytes
"""

from __future__ import annotations

import os

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_not_found, api_ok
from lib.latex_preview import PreviewError, cached_pdf, run_local_preview
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_latex_bp = Blueprint('api_v1_latex', __name__)


@api_v1_latex_bp.route('/api/v1/latex/preview', methods=['POST'])
@require_auth
@api_meta(
    summary='Compile a local LaTeX paper and locate an edit in the PDF',
    description=(
        'Body: ``{path, needle?, line?, conv_id?}``. ``path`` is the edited '
        '``.tex`` file as the model wrote it (absolute, relative to the '
        'primary root, or ``rootname:rel``); it must resolve inside a '
        'known workspace root. Compiles with the host TeX engine '
        '(pdflatex/xelatex) into a per-document cache dir, then resolves '
        'the SyncTeX page/rects of the edit so the chat preview panel can '
        'scroll to it. Returns ``pdf_url`` (this API), ``page`` and '
        '``rects`` (PostScript points, ``v`` from page top).'
    ),
    tags=['latex'],
)
def latex_preview_v1():
    data = parse_body()
    raw_path = (data.get('path') or '').strip()
    if not raw_path:
        return api_bad_request('path is required', field='path')
    line = data.get('line')
    if line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            return api_bad_request('line must be an integer', field='line')
        if line < 1:
            return api_bad_request('line must be >= 1', field='line')
    conv_id = (data.get('conv_id') or '').strip() or None
    try:
        result = run_local_preview(
            raw_path,
            needle=data.get('needle') or '',
            line=line,
            conv_id=conv_id)
    except PreviewError as exc:
        logger.warning('[Latex.v1] preview failed: %s', exc)
        return api_error(str(exc), status=502, kind='latex_preview_failed')
    version = int(os.path.getmtime(result['pdf_path']))
    return api_ok({
        'pdf_key': result['pdf_key'],
        'pdf_url': (f"/api/v1/latex/preview/{result['pdf_key']}/pdf"
                    f'?v={version}'),
        'compiled_at': result['compiled_at'],
        'page': result['page'],
        'rects': result['rects'],
    })


@api_v1_latex_bp.route(
    '/api/v1/latex/preview/<pdf_key>/pdf', methods=['GET'])
@require_auth
def serve_latex_pdf_v1(pdf_key):
    """Serve the last compiled PDF for a key (404 until first preview).

    SYNC: pure file serving through the sync-safe shim (same carve-out as
    serve_deck_file). Path comes from a validated 16-hex cache key only.
    """
    path = cached_pdf(pdf_key)
    if not path:
        return api_not_found('file_not_ready')
    from lib.file_serving import send_file_conditional
    return send_file_conditional(path, mimetype='application/pdf')


__all__ = ['api_v1_latex_bp']
