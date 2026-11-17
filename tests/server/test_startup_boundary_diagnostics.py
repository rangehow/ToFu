"""Startup refusals distinguish unreadable source from database ownership."""
# A synthetic module under tmp_path, never a repository file.
_AUDIT_SYNTHETIC_REPO_PATHS = ('lib/example.py',)

from pathlib import Path

import pytest
from lib.storage.storage_boundary import require_exclusive_sidecar_boundary

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', ['syntax', 'unreadable'])
def test_invalid_source_is_reported_as_unscannable_not_database_ownership(tmp_path, monkeypatch, failure):
    source = tmp_path / 'lib/example.py'
    source.parent.mkdir()
    source.write_text('import sqlite3\ndef broken(:\n    pass\n')
    if failure == 'unreadable':
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            if path == source:
                raise FileNotFoundError('stale host lookup')
            return read_text(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', read)
    with pytest.raises(RuntimeError, match='cannot read or parse Python source.*lib/example.py'):
        require_exclusive_sidecar_boundary(tmp_path)


def test_database_ownership_refusal_remains_explicit(tmp_path):
    source = tmp_path / 'lib/example.py'
    source.parent.mkdir()
    source.write_text('import sqlite3\n')
    with pytest.raises(RuntimeError, match='direct database ownership remains'):
        require_exclusive_sidecar_boundary(tmp_path)
