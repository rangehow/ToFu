"""routes/api_v1/research.py — durable read path for auto-research artifacts.

WHY THIS BLUEPRINT EXISTS (epic pt_a40dbd9569194b52, second half)
-----------------------------------------------------------------
Persisting the research artifacts gave them a durable home; this gives them a
door. Without it the artifacts sat in ``paper_reports`` with no way for the
product to ask for them, which is the same orphan shape the persistence layer
itself was fixing.

**Why this is not served by ``GET /api/v1/tasks/<id>``.** That endpoint resolves
against the in-memory ``TaskRuntime`` registry, so it 404s the moment
``cleanup_stale()`` sweeps the finished task (TTL 7200s) or the process
restarts — exactly the window durable storage exists to cover. It is also
addressed by TASK id, while a persisted research row is addressed by
DIRECTION: the same direction re-researched later is the same row. So this is a
structurally different lookup, not a duplicate of the generic task surface, and
the two are complementary:

    live job, progress + abort   → /api/v1/tasks/<id>
    finished work, any time later → /api/v1/research/lookup?direction=…

``found: false`` is a normal 200 answer, not a 404: the re-attach path calls
this on every open, and "this direction has not been researched" is information,
not an error.
"""

from __future__ import annotations

from flask import Blueprint, request

from lib.api_response import api_bad_request, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_research_bp = Blueprint('api_v1_research', __name__)


@api_v1_research_bp.route('/api/v1/research/lookup', methods=['GET'])
@require_auth
@api_meta(
    summary='Look up persisted auto-research artifacts by direction',
    description=(
        'Returns the survey markdown, the open-gap map, the accepted ideas and '
        'the full rejection audit (with four-axis rubric scores) persisted for '
        'a research direction. Served from durable storage, so it keeps working '
        'after the in-memory task has been TTL-swept or the server restarted — '
        'unlike GET /api/v1/tasks/{id}, which is task-id addressed and '
        'in-memory only. An unresearched direction is a 200 with found=false.'),
    tags=['research'],
    parameters=[
        {'name': 'direction', 'in': 'query', 'required': True,
         'schema': {'type': 'string'},
         'description': 'The research direction (case/whitespace-insensitive).'},
        {'name': 'lang', 'in': 'query',
         'schema': {'type': 'string', 'default': 'en'}},
    ])
def research_lookup():
    """Serve persisted research artifacts for a direction."""
    direction = (request.args.get('direction') or '').strip()
    if not direction:
        return api_bad_request("'direction' is required and must be non-empty")
    lang = (request.args.get('lang') or 'en').strip() or 'en'
    try:
        from lib.research.persistence import load_research_artifacts
        artifacts = load_research_artifacts(direction, lang)
    except Exception as e:
        logger.error('[api_v1.research] lookup failed for %.60s: %s',
                     direction, e, exc_info=True)
        return api_internal_error('internal_error')
    logger.info('[api_v1.research] lookup dir=%.60s lang=%s → found=%s '
                '(%d accepted / %d rejected)', direction, lang,
                artifacts.get('found'), len(artifacts.get('accepted') or []),
                len(artifacts.get('rejected') or []))
    return api_ok(artifacts)


__all__ = ['api_v1_research_bp']
