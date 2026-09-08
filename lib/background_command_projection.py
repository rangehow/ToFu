"""Pure presentation of durable background-command completion evidence.

Queue list and conversation snapshots share this projection. It never owns
execution or delivery state; a remaining row means delivery is unconfirmed,
not that the subprocess is still running.
"""
from __future__ import annotations

from typing import Any, Mapping


def bounded_completion_preview(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Bound display text without repairing identity, status, or malformed types."""
    from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS

    result = dict(metadata)
    properties = OPENAPI_SCHEMAS['BackgroundCommandCompletion']['properties']
    for field in ('command', 'resultPreview'):
        value = result.get(field)
        if isinstance(value, str):
            limit = properties[field]['maxLength']
            if len(value) > limit:
                result[field] = value[:limit]
                if field == 'resultPreview':
                    result['resultTruncated'] = True
    return result


def bound_projection_completion_previews(projection: dict[str, Any]) -> None:
    """Copy nested display metadata; never mutate durable input or model text."""
    completion = projection.get('backgroundCommand')
    if isinstance(completion, Mapping):
        projection['backgroundCommand'] = bounded_completion_preview(completion)
    injections = projection.get('_bgCommandInjects')
    if not isinstance(injections, list):
        return
    normalized = []
    for injection in injections:
        if not isinstance(injection, Mapping) or not isinstance(injection.get('previews'), list):
            normalized.append(injection)
            continue
        previews = []
        for preview in injection['previews']:
            if isinstance(preview, Mapping) and isinstance(preview.get('backgroundCommand'), Mapping):
                preview = {**preview, 'backgroundCommand': bounded_completion_preview(preview['backgroundCommand'])}
            previews.append(preview)
        normalized.append({**injection, 'previews': previews})
    projection['_bgCommandInjects'] = normalized


def background_command_completion(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project explicit producer metadata; never infer success from output text."""
    command_id = payload.get('_backgroundCommand')
    if not isinstance(command_id, str) or not command_id:
        return None
    metadata = payload.get('_backgroundCommandCompletion')
    if not isinstance(metadata, Mapping):
        # Old durable rows remain readable without interpreting model-facing XML.
        metadata = {}
    return bounded_completion_preview({
        'commandId': command_id[:128],
        'status': 'completed',
        'command': str(metadata.get('command') or ''),
        'resultPreview': str(metadata.get('resultPreview') or ''),
        'resultTruncated': bool(metadata.get('resultTruncated')),
    })
