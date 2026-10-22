"""Public API/handler equality, root/owner denial, epochs and exact finalization."""
from pathlib import Path
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
from lib.identity import PrincipalContext

OWNER = 900001


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-public-repository-port-') as folder:
        root = Path(folder)/'authorized'; other = Path(folder)/'foreign'
        root.mkdir(); other.mkdir()
        (root/'module.py').write_text('def original():\n    return source_reference\n')
        (other/'module.py').write_text('def private_symbol():\n    return 1\n')
        pool = CacheReservationPool(CacheBudgetPlan(2*1024*1024, 1024*1024, 2, 'fixture'))
        kernel = TaskRuntime('benchmark.public-repository-port', push_channel='')
        task = kernel.create(user_id=OWNER); execution = task['_executionSession']
        group = create_bootstrap_group(pool, OWNER, task['id'], [root], epoch_source=lambda root: 0)
        bind_tool_environment(execution, lambda: group.close(OWNER))
        execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted()
        assert kernel.mark_running(task['id'])
        port = PublicTaskRepositoryToolPort(root, group.for_root(OWNER, root))
        reference = PublicTaskRepositoryToolPort(root)
        try:
            for _ in range(3):
                assert task_operation(port, task) == task_operation(reference, task)
            (root/'module.py').write_text('def edited():\n    return changed_reference\n')
            assert task_operation(port, task) == task_operation(reference, task)
            with patch.object(outline, 'STOP_WORDS', outline.STOP_WORDS | {'changed_reference'}):
                assert task_operation(port, task) == task_operation(reference, task)
            foreign = dict(task, _userId=OWNER+1, _principalContext=PrincipalContext.user(
                subject_id='foreign', owner_user_id=OWNER+1).to_payload())
            for current, project_root in ((task, other), (foreign, root)):
                settlements = []
                def finalize(task, number, entry, metadata, **options):
                    settlements.append(options.get('status'))
                result = port.invoke(current, {}, finalize, project_root=project_root)
                assert result[2] and settlements == ['error']
            rejected = []
            try:
                port.invoke(dict(task, _userId=OWNER+1), {}, lambda *args, **kwargs: rejected.append(True))
            except ValueError as error:
                assert 'principal owner' in str(error)
            else:
                raise AssertionError('mismatched task principal admitted')
            assert rejected == []
            assert 'user_id' not in task
        finally:
            end_task(kernel, task); group.close(OWNER)
        assert pool.snapshot()['reserved_bytes'] == 0 and kernel.task_count == 0
    print('public handler/API match across cold/warm/edit/rules; canonical task ownership; root/owner denial settles once; terminal release0')


if __name__ == '__main__':
    check()
