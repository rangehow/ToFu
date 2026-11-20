"""Checkout and owner ownership of a manager, against both managers (test_supervisor_ownership.py).

A manager is owned by its checkout while the checkout's ``supervisor.py``
exists, and by its declared owner process. A transient replacement (git
rewriting the file) is not loss; a deleted or moved checkout retires the
manager and its worker. Each manager runs from a disposable copy of its
generation closure, never the shipped tree.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import time

import pytest

from tests.server.rust_manager_parity.conftest import pid_alive, wait_for
from tests.server.rust_manager_parity.test_reload_parity import source_root  # noqa: F401  (fixture)

pytestmark = pytest.mark.api


def retired(manager, worker_pid: int) -> None:
    manager.process.wait(timeout=60)
    wait_for(lambda: not pid_alive(worker_pid), 'the owned worker to be retired')
    assert 'ownership sentinel disappeared' in manager.log_text()


def test_a_transiently_replaced_source_is_not_ownership_loss(manager, source_root):  # noqa: F811
    first = manager.start_serving()
    sentinel = source_root / 'supervisor.py'
    content = sentinel.read_bytes()

    sentinel.unlink()
    time.sleep(1.5)
    sentinel.write_bytes(content)
    time.sleep(1.5)

    assert manager.process.poll() is None
    assert manager.wait_serving()['pid'] == first['pid']
    assert 'supervisor ownership sentinel missing' in manager.log_text()
    assert 'supervisor ownership sentinel reappeared' in manager.log_text()


def test_a_deleted_checkout_retires_the_manager_and_its_worker(manager, source_root):  # noqa: F811
    first = manager.start_serving()

    shutil.rmtree(source_root)

    retired(manager, first['pid'])
    assert '(checkout directory is gone)' in manager.log_text()


def test_a_moved_checkout_retires_even_after_its_path_is_recreated(manager, source_root, tmp_path):  # noqa: F811
    first = manager.start_serving()

    os.rename(source_root, tmp_path / 'moved-checkout')
    Path(source_root).mkdir()

    retired(manager, first['pid'])
    assert '(checkout directory was replaced)' in manager.log_text()
