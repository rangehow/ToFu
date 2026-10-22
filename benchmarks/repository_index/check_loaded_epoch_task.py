"""Actual-task tool equality, automatic rule invalidation and failed-query release."""
from pathlib import Path
import tempfile
from unittest.mock import patch

import loaded_epoch_task_measure as candidate
from budget import CacheBudgetPlan
from index import outline
from quota import CacheReservationPool
from task_port import TaskRepositoryToolPort
from task_measure import task_operation, end_task
from lib.agent_core.execution_session import bind_tool_environment
from lib.agent_core.task_runtime import TaskRuntime

OWNER = 900001


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-loaded-task-epoch-') as folder:
        root = Path(folder)
        (root/'module.py').write_text('def original():\n    return source_reference\n')
        pool = CacheReservationPool(CacheBudgetPlan(2*1024*1024, 1024*1024, 2, 'fixture'))
        for fault in (False, True):
            kernel = TaskRuntime('benchmark.loaded-task-epoch', push_channel='')
            task = kernel.create(user_id=OWNER)
            execution = task['_executionSession']
            class FailedQuery(candidate.LoadedExtractionEpoch):
                def __call__(self):
                    raise OSError('injected loaded identity read failure')
            factory = FailedQuery if fault else candidate.LoadedExtractionEpoch
            with patch.object(candidate, 'LoadedExtractionEpoch', factory):
                group = candidate.create_loaded_epoch_group(pool, OWNER, task['id'], [root], epoch_source=lambda root: 0)
            bind_tool_environment(execution, lambda: group.close(OWNER))
            execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted()
            assert kernel.mark_running(task['id'])
            port = TaskRepositoryToolPort(str(root), group.for_root(OWNER, root))
            try:
                if fault:
                    try:
                        group.scan(OWNER, root)
                    except OSError as error:
                        assert str(error) == 'injected loaded identity read failure'
                    else:
                        raise AssertionError('failed epoch query returned stale output')
                    settlements = []
                    def finalize(task, number, entry, metadata, **options):
                        settlements.append((metadata, options.get('status')))
                    result = port.invoke(task, {}, finalize)
                    assert len(settlements) == 1 and settlements[0][1] == 'error'
                    assert 'injected loaded identity read failure' in str(result)
                else:
                    reference = TaskRepositoryToolPort(str(root))
                    assert task_operation(port, task) == task_operation(reference, task)
                    assert task_operation(port, task) == task_operation(reference, task)
                    assert group.retained_bytes(OWNER) > 0
                    with patch.object(outline, 'STOP_WORDS', outline.STOP_WORDS | {'source_reference'}):
                        assert task_operation(port, task) == task_operation(reference, task)
                    assert task_operation(port, task) == task_operation(reference, task)
            finally:
                end_task(kernel, task, fault)
                group.close(OWNER)
            assert pool.snapshot()['reserved_bytes'] == 0
            assert group.retained_bytes(OWNER) == 0
            assert kernel.task_count == 0
    print('actual task/tool equality and loaded-rule invalidation; failed epoch query preserves error; terminal release balanced')


if __name__ == '__main__':
    check()
