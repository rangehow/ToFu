"""Cold callers share one complete index build instead of duplicate live walks."""
import threading

import pytest

from lib.project_mod import tree_index

pytestmark = pytest.mark.unit


def test_concurrent_find_files_shares_one_cold_build(tmp_path, monkeypatch):
    from lib.project_mod import read_tools

    (tmp_path / 'a.py').write_text('alpha')
    (tmp_path / 'b.py').write_text('beta')
    root = str(tmp_path)
    build_entered = threading.Event()
    release_build = threading.Event()
    waiters_entered = threading.Event()
    waiter_count = 0
    lock = threading.Lock()
    original_wait = tree_index._wait_for_build

    def build(root, *, scan_executor=None):
        build_entered.set()
        assert release_build.wait(2)
        tree_index._mem[root] = tree_index.TreeIndex(
            root, ['a.py', 'b.py'], [1, 2], [0, 0], 1_000_000, True)

    def wait_build(root):
        nonlocal waiter_count
        with lock:
            waiter_count += 1
            if waiter_count == 2:
                waiters_entered.set()
        return original_wait(root)

    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(tree_index, '_building', set())
    monkeypatch.setattr(tree_index, '_builder', None)
    monkeypatch.setattr(tree_index, '_scanner', None)
    monkeypatch.setattr(tree_index, '_load_disk', lambda _root: None)
    monkeypatch.setattr(tree_index, '_build_sync', build)
    monkeypatch.setattr(tree_index, '_wait_for_build', wait_build)
    monkeypatch.setattr(tree_index, '_build_condition', threading.Condition(tree_index._lock))
    monkeypatch.setattr(tree_index.time, 'time', lambda: 1_000_000.0)
    monkeypatch.setattr(read_tools, '_fd_find',
        lambda *_args, **_kwargs: pytest.fail('cold callers must not duplicate fd walks'))

    tree_index.warm(root)
    assert build_entered.wait(1)
    entries = []
    threads = [threading.Thread(
        target=lambda: entries.append(read_tools.tool_find_files(root, '*.py'))) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert waiters_entered.wait(1)
    release_build.set()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert len(entries) == 2
    assert all('a.py (' in entry and 'b.py (' in entry for entry in entries)


def test_failed_cold_build_wakes_waiters_and_never_installs_partial_index(
        tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setattr(tree_index, '_mem', {})
    monkeypatch.setattr(tree_index, '_building', set())
    monkeypatch.setattr(tree_index, '_builder', None)
    monkeypatch.setattr(tree_index, '_scanner', None)
    monkeypatch.setattr(tree_index, '_build_sync', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tree_index, '_load_disk', lambda _root: None)
    monkeypatch.setattr(tree_index, '_build_condition', threading.Condition(tree_index._lock))
    monkeypatch.setattr(tree_index.time, 'time', lambda: 1_000_000.0)
    settled = threading.Event()
    finish = tree_index._finish_builder_job

    def observe_finish(*args):
        finish(*args)
        settled.set()

    monkeypatch.setattr(tree_index, '_finish_builder_job', observe_finish)
    tree_index.warm(root)
    assert settled.wait(2)
    assert root not in tree_index._building
    assert tree_index.acquire(root) is None
