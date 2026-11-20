"""Semantic Turn revision shared by execution evidence and durable continuation.

This value projection has no repository or storage dependency. Stores publish
modelContextRevision; readers of older/private rows derive the same value.
"""


def model_context_revision(row):
    if 'modelContextRevision' in row:
        return row['modelContextRevision']
    return int(row.get('projectionRevision') or 0) - int(row.get('_modelContextDisplayOffset') or 0)
