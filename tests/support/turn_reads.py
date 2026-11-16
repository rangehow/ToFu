"""Emulate the authority's Turn read modes for in-memory ``turn_get`` ports.

``get_turn`` reads a full Turn, its metadata (``include_projection=False``) or
metadata plus ``contentPreview`` (``content_preview_chars``), which the
storage authority cuts from the public projection content.
"""
from copy import deepcopy


def turn_read(row, *, include_projection=True, content_preview_chars=None, **_ignored):
    if not isinstance(row, dict) or include_projection:
        return deepcopy(row)
    metadata = {key: deepcopy(value) for key, value in row.items() if key != 'projection'}
    if content_preview_chars is not None:
        content = (row.get('projection') or {}).get('content')
        metadata['contentPreview'] = content[:content_preview_chars] if isinstance(content, str) else None
    return metadata
