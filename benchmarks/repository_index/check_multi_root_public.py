"""Actual public scanner ports under concurrent multi-root execution pressure.

Recording publication remains excluded. Canonical task identity, one execution
reservation, real loaded epochs and unchanged result/metadata are exercised.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import tempfile
from unittest.mock import patch

from bootstrap_epoch_task_measure import create_bootstrap_group
from budget import CacheBudgetPlan
from index import outline
from quota import CacheReservationPool
from public_tool_port import PublicTaskRepositoryToolPort
from task_measure import task_operation, end_task
from lib.agent_core.execution_session import bind_tool_environment
from lib.agent_core.task_runtime import TaskRuntime

OWNER = 900001


def task_group(roots, *, quota):
    pool = CacheReservationPool(CacheBudgetPlan(quota, quota, 1, 'fixture'))
    kernel = TaskRuntime('benchmark.multi-root-public', push_channel='')
    task = kernel.create(user_id=OWNER)
    group = create_bootstrap_group(pool, OWNER, task['id'], roots, epoch_source=lambda root: 0)
    session = task['_executionSession']
    bind_tool_environment(session, lambda: group.close(OWNER))
    session.mark_routed(); session.mark_reserved(); session.mark_admitted()
    assert kernel.mark_running(task['id'])
    return pool, kernel, task, group


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-multi-root-public-') as folder:
        roots = [Path(folder)/name for name in ('left', 'right', 'third')]
        for root in roots:
            root.mkdir()
            for i in range(6):
                (root/f'module{i}.py').write_text(f'def {root.name}_symbol_{i}(value):\n    return value+{i}\n')
        pool, kernel, task, group = task_group(roots, quota=2048)
        ports = [PublicTaskRepositoryToolPort(root, group.for_root(OWNER, root)) for root in roots]
        expected = [task_operation(PublicTaskRepositoryToolPort(root), task) for root in roots]
        try:
            def invoke(index):
                selected = index % len(roots)
                actual = task_operation(ports[selected], task)
                assert actual == expected[selected]
                with group._condition:
                    committed = sum(group._limits.values())+sum(
                        used for root, used in group._used.items() if root not in group._limits)
                    assert committed <= 2048
                return actual
            with ThreadPoolExecutor(max_workers=8) as workers:
                assert len(list(workers.map(invoke, range(48)))) == 48
            assert pool.snapshot()['active_scopes'] == 1
            assert group.retained_bytes(OWNER) <= 2048 and group._active == 0
        finally:
            end_task(kernel, task); group.close(OWNER)
        assert pool.snapshot()['reserved_bytes'] == 0 and kernel.task_count == 0

        # An actual terminal release must join a first uncached public scan.
        pool, kernel, task, group = task_group(roots, quota=1024*1024)
        port = PublicTaskRepositoryToolPort(roots[0], group.for_root(OWNER, roots[0]))
        entered, release = Event(), Event()
        original = outline._collect_source_tags
        def blocked(path, text):
            entered.set()
            assert release.wait(5), 'owned scan release deadline'
            return original(path, text)
        with patch.object(outline, '_collect_source_tags', blocked), ThreadPoolExecutor(max_workers=2) as workers:
            work = workers.submit(task_operation, port, task)
            try:
                assert entered.wait(3)
                closing = workers.submit(end_task, kernel, task)
                with group._condition:
                    assert group._condition.wait_for(lambda: not group._accepting, timeout=3)
                assert not closing.done() and pool.snapshot()['reserved_bytes'] > 0
                try:
                    group.scan(OWNER, roots[1])
                except RuntimeError as error:
                    assert 'closed' in str(error)
                else:
                    raise AssertionError('terminal execution admitted another root scan')
            finally:
                release.set()
                work.result(timeout=5)
                end_task(kernel, task); group.close(OWNER)
        assert pool.snapshot()['reserved_bytes'] == 0 and kernel.task_count == 0
    print('48 public calls/8threads/3roots match exact metadata under2KiB; one execution lease; terminal closes admission, joins first scan and releases')


if __name__ == '__main__':
    check()
