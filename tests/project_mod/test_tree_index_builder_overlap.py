"""A shared scanner cannot be retired while its next root is being admitted."""
import threading
from pathlib import Path

import pytest

from lib.project_mod import tree_index

pytestmark = pytest.mark.unit


def test_admission_before_stale_builder_cleanup_retains_scanner(
        tmp_path, monkeypatch):
    root = str(tmp_path)
    (tmp_path / 'one.py').write_text('one')
    first_finished = threading.Event()
    release_first_cleanup = threading.Event()
    finish_original = tree_index._finish_builder_job

    def first_finish(root, builder, scanner):
        first_finished.set()
        assert release_first_cleanup.wait(2)
        finish_original(root, builder, scanner)

    def blocked_build(*_args, **_kwargs):
        return

    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(tree_index, '_building', set())
    monkeypatch.setattr(tree_index, '_builder', None)
    monkeypatch.setattr(tree_index, '_scanner', None)
    monkeypatch.setattr(tree_index, '_build_sync', blocked_build)
    monkeypatch.setattr(tree_index, '_load_disk', lambda _root: None)
    monkeypatch.setattr(tree_index, '_finish_builder_job', first_finish)
    tree_index.warm(root)
    assert first_finished.wait(2)
    with tree_index._lock:
        builder = tree_index._builder
        scanner = tree_index._scanner
        tree_index._building.clear()
    tree_index.warm(root)
    with tree_index._lock:
        current_scanner = tree_index._scanner
        assert current_scanner is scanner
        assert tree_index._scanner_admissions == 2
    tree_index._finish_builder_job = finish_original
    release_first_cleanup.set()
    assert not current_scanner._shutdown
    assert current_scanner.submit(lambda: 'alive').result(timeout=2) == 'alive'
    tree_index._finish_builder_job(root, builder, current_scanner)
    assert current_scanner._shutdown
    builder.shutdown(wait=True)
