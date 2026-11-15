"""In-app page preview for published PowerPoint attachments.

Entry points: ``preview_pages`` (page count + geometry) and ``preview_page``
(one rendered page + its hyperlink boxes). Ownership and integrity come from
``response_files.load_file``; rendering is delegated to the slides facade
(``lib.slides.api``), which imports only the requested slide. Nothing is
persisted: each page is derived from the immutable attachment bytes on demand.
"""
from __future__ import annotations

import base64
import os
import re
import tempfile

PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
_SAFE_LINK_RE = re.compile(r'^(?:https?://|mailto:)[^\s<>"]+$', re.IGNORECASE)


class PreviewUnsupported(ValueError):
    """The attachment has no page preview (not a .pptx)."""


class PreviewRenderError(RuntimeError):
    """Rendering failed for an owned, supported attachment."""


class PreviewPageNotFound(LookupError):
    """The caller's page index names no page of the presentation."""


def _owned_presentation(artifact_id, user_id):
    from lib.artifacts.response_files import load_file
    data, info = load_file(artifact_id, user_id=user_id)
    if info['mime'] != PPTX_MIME:
        raise PreviewUnsupported('Only .pptx attachments have a page preview')
    return data, info


def _with_file(data, fn):
    with tempfile.TemporaryDirectory(prefix='artifact-preview-') as work:
        path = os.path.join(work, 'deck.pptx')
        with open(path, 'wb') as handle:
            handle.write(data)
        return fn(path, work)


def preview_pages(artifact_id, *, user_id) -> dict:
    data, info = _owned_presentation(artifact_id, user_id)
    from lib.slides.api import pptx_geometry
    try:
        geometry = _with_file(data, lambda path, _work: pptx_geometry(path))
    except Exception as exc:
        raise PreviewRenderError('Presentation could not be read') from exc
    if geometry['pageCount'] < 1:
        raise PreviewUnsupported('Presentation has no pages')
    return {'version': 1, 'artifactId': artifact_id, 'sha256': info['sha256'], **geometry}


def _clamp(value) -> float:
    return min(1.0, max(0.0, float(value)))


def preview_page(artifact_id, index, *, user_id) -> dict:
    data, _info = _owned_presentation(artifact_id, user_id)
    if not isinstance(index, int) or index < 0:
        raise PreviewPageNotFound('page index must be a non-negative integer')
    from lib.slides.api import pptx_geometry, render_pptx_page

    def render(path, work):
        geometry = pptx_geometry(path)
        if index >= geometry['pageCount']:
            raise PreviewPageNotFound('page index outside the presentation')
        png = os.path.join(work, 'page.png')
        page = render_pptx_page(path, index, png)
        with open(png, 'rb') as handle:
            return geometry, page, handle.read()

    try:
        geometry, page, png = _with_file(data, render)
    except PreviewPageNotFound:
        raise
    except Exception as exc:
        raise PreviewRenderError('Presentation page could not be rendered') from exc
    links = [{'href': box['href'], 'x': _clamp(box['x']), 'y': _clamp(box['y']),
              'w': _clamp(box['w']), 'h': _clamp(box['h'])}
             for box in page['links']
             if isinstance(box.get('href'), str) and _SAFE_LINK_RE.match(box['href'])]
    return {'version': 1, 'index': index, 'pageCount': geometry['pageCount'],
            'width': page['width'], 'height': page['height'],
            'image': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii'),
            'links': links}
