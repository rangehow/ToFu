"""Shared mutable export-pipeline state.

These two globals are REBOUND from multiple sites (``_stage_head_snapshot``,
``export_project``) and READ from several others. A dedicated module lets the
split ``export_pkg`` modules share them without import cycles and without
``from X import name`` snapshots going stale.

Tests rebind these attributes directly (``monkeypatch.setattr(_state,
'_EXPORT_SOURCE_SHA', …)``). The facade (``export.py``) keeps launch-time
mirror copies of both names purely for surface stability — nothing reads
them at runtime.
"""

_EXPORT_VERSION: str = ''

# Set by _stage_head_snapshot: the exact commit the export copied. The
# post-copy integrity check must list files from THIS commit, not HEAD —
# a sibling commit landing mid-export would otherwise flag files the
# snapshot legitimately predates.
_EXPORT_SOURCE_SHA: str | None = None
