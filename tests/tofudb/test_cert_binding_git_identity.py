"""The certification binding reads source-tree state without locking its index.

``git status`` decides the binding's ``dirty`` flag in the checkout the
operator builds from. Plain status takes ``index.lock`` to rewrite a stale
index, and a generator killed mid-scan would leave it for every writer there.
"""
import importlib.util
from pathlib import Path

import pytest

from tests.support.git_index_probe import stat_dirty_checkout

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _generator():
    spec = importlib.util.spec_from_file_location(
        'gen_tofudb_cert_binding_under_test', ROOT / 'scripts' / 'gen_tofudb_cert_binding.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('writer_holds_lock', [False, True])
def test_source_identity_reads_status_without_the_index_lock(tmp_path, writer_holds_lock):
    probe = stat_dirty_checkout(tmp_path / 'checkout', writer_holds_lock=writer_holds_lock)

    identity = _generator()._git_identity(probe.root)

    assert identity['dirty'] is False and len(identity['head_commit']) == 40
    assert probe.unchanged()
    assert probe.lock.exists() is writer_holds_lock
