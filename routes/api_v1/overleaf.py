"""routes/api_v1/overleaf.py — Live Overleaf PDF preview for the chat pane.

The browser's Overleaf live-preview panel posts here after an agent edit to
a TeX project; the route compiles the remote project through the MCP bridge
(``mcp__overleaf__*`` tools), asks SyncTeX where the edit landed, and the
panel re-renders the PDF scrolled to that spot.

Routes:
  POST /api/v1/overleaf/preview                — compile + locate; pdf_url in body
  GET  /api/v1/overleaf/preview/<project_id>/pdf — serve the cached PDF bytes
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_not_found, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.overleaf_preview import (
    PROJECT_ID_RE,
    PreviewError,
    cached_pdf_path,
    run_preview,
)
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_overleaf_bp = Blueprint('api_v1_overleaf', __name__)


@api_v1_overleaf_bp.route('/api/v1/overleaf/preview', methods=['POST'])
@require_auth
@api_meta(
    summary='Compile an Overleaf project and locate an edit in the PDF',
    description=(
        'Body: ``{project_id, file?, needle?, line?}``. Compiles the remote '
        'project through the Overleaf MCP bridge, stages the PDF in the '
        'local cache, and — when ``file`` + (``needle`` or ``line``) are '
        'given — resolves the SyncTeX page/rects of the edit so the chat '
        'preview panel can scroll to it. Returns ``pdf_url`` (this API), '
        '``page`` and ``rects`` (PostScript points, ``v`` from page top).'
    ),
    tags=['overleaf'],
)
def overleaf_preview_v1():
    data = parse_body()
    project_id = (data.get('project_id') or '').strip()
    if not PROJECT_ID_RE.match(project_id):
        return api_bad_request(
            'project_id must be a 24-hex Overleaf project id',
            field='project_id')
    line = data.get('line')
    if line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            return api_bad_request('line must be an integer', field='line')
        if line < 1:
            return api_bad_request('line must be >= 1', field='line')
    from lib.mcp import get_bridge
    try:
        result = run_preview(
            get_bridge(), project_id,
            file=data.get('file') or '',
            needle=data.get('needle') or '',
            line=line)
    except PreviewError as exc:
        logger.warning('[Overleaf.v1] preview failed: %s', exc)
        return api_error(str(exc), status=502, kind='overleaf_preview_failed')
    import os
    version = int(os.path.getmtime(result['pdf_path']))
    return api_ok({
        'project_id': project_id,
        'pdf_url': (f'/api/v1/overleaf/preview/{project_id}/pdf'
                    f'?v={version}'),
        'compiled_at': result['compiled_at'],
        'page': result['page'],
        'rects': result['rects'],
    })


@api_v1_overleaf_bp.route(
    '/api/v1/overleaf/preview/<project_id>/pdf', methods=['GET'])
@require_auth
def serve_preview_pdf_v1(project_id):
    """Serve the last compiled PDF for a project (404 until first preview).

    SYNC: pure file serving through the sync-safe shim (same carve-out as
    serve_deck_file). Path is assembled from a validated hex id only.
    """
    if not PROJECT_ID_RE.match(project_id or ''):
        return api_not_found('not_found')
    import os
    path = cached_pdf_path(project_id)
    if not os.path.isfile(path):
        return api_not_found('file_not_ready')
    from lib.file_serving import send_file_conditional
    return send_file_conditional(path, mimetype='application/pdf')


__all__ = ['api_v1_overleaf_bp']
