"""A Turn's semantic (model-visible) revision, free of storage dependencies.

Entry: model_context_revision. ``lib.storage.model_continuation`` re-exports it;
project dispatch modules on a storage-free run path import it from here.
"""


def model_context_revision(row):
    """The Turn's semantic revision: its projection revision minus declared display-only updates.

    Stores publish ``modelContextRevision``; a row without it (no display-only
    update recorded, or a private document) derives the same value.
    """
    if 'modelContextRevision' in row:
        return row['modelContextRevision']
    return int(row.get('projectionRevision') or 0) - int(row.get('_modelContextDisplayOffset') or 0)
