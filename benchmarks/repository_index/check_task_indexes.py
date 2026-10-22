"""Actual task ownership, multi-root quota and composed terminal cleanup checks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import tempfile
from unittest.mock import patch
from uuid import uuid4

from budget import CacheBudgetPlan
from index import outline
from quota import CacheReservationPool
from task_indexes import ExecutionRepositoryIndexes
from task_port import TaskRepositoryToolPort
from lib.agent_core.execution_session import ExecutionSession, ExecutionPhase, ResourceDisposition, bind_tool_environment
from lib.agent_core.task_runtime import TaskRuntime
from lib.identity import PrincipalContext
from lib.tools.tool_env import mint_tool_env, dispose_tool_env, count_tool_envs

OWNER = 900001
MIB = 1024*1024


def drive(port, task):
    settlements = []
    def finalize(task, number, entry, metadata, **options):
        settlements.append((metadata, options.get('status', 'done')))
    result = port.invoke(task, {}, finalize)
    assert len(settlements) == 1
    return result, settlements[0]


def check():
    initial_envs = count_tool_envs()
    with tempfile.TemporaryDirectory(prefix='tofu-task-indexes-') as folder:
        roots = [Path(folder)/name for name in ('left','right')]
        for root, name in zip(roots, ('left_symbol','right_symbol')):
            root.mkdir(); (root/(root.name+'.py')).write_text(f'def {name}():\n    return 1\n')
        versions = {str(root):0 for root in roots}
        pool = CacheReservationPool(CacheBudgetPlan(2*MIB, MIB, 2, 'fixture'))
        kernel = TaskRuntime('benchmark.canonical-task-indexes', push_channel='')
        task = kernel.create(task_id=uuid4().hex, user_id=OWNER)
        kernel.mark_running(task['id'])
        group = ExecutionRepositoryIndexes(pool, OWNER, task['id'], roots,
                                           epoch_source=lambda root: versions[root])
        try:
            for root in roots:
                port = TaskRepositoryToolPort(str(root), group.for_root(OWNER, root))
                expected = drive(TaskRepositoryToolPort(str(root)), task)
                assert drive(port, task) == expected
                assert drive(port, task) == expected
                assert 'user_id' not in task and task['_principalContext']['owner_user_id'] == OWNER
            assert pool.snapshot()['active_scopes'] == 1
            assert 0 < group.retained_bytes(OWNER) <= MIB
            # Authorized but unused roots do not divide the active root's budget.
            group.scan(OWNER, roots[0]); assert group._scopes[str(roots[0])]._index.maximum_bytes > MIB//2
            versions[str(roots[0])] += 1
            assert group.scan(OWNER, roots[0]) == outline.scan(str(roots[0]))
            bad = dict(task, _principalContext=PrincipalContext.user(
                subject_id='foreign', owner_user_id=OWNER+1).to_payload())
            try:
                drive(TaskRepositoryToolPort(str(roots[0]), group.for_root(OWNER, roots[0])), bad)
            except ValueError as error:
                assert 'principal owner' in str(error)
            else:
                raise AssertionError('mismatched task principal admitted')
            for owner, root in ((OWNER+1, roots[0]), (OWNER, Path(folder)/'foreign')):
                try:
                    group.scan(owner, root)
                except PermissionError:
                    pass
                else:
                    raise AssertionError('foreign owner/root admitted')
        finally:
            group.close(OWNER); kernel.finish(task['id'], result='fixture')
            kernel.cleanup_stale(max_age=0)
        assert pool.snapshot()['reserved_bytes'] == 0

        for outcome in (ExecutionPhase.COMPLETED, ExecutionPhase.FAILED,
                        ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
            execution = ExecutionSession(execution_id=uuid4().hex, kind='task', owner_user_id=OWNER)
            group = ExecutionRepositoryIndexes(pool, OWNER, execution.execution_id, roots,
                                               epoch_source=lambda root: versions[root])
            env = mint_tool_env(owner='fixture', tools=[{
                'type':'function', 'function':{'name':'custom__fixture',
                'description':'offline fixture', 'parameters':{'type':'object','properties':{}}},
                'execution':{'mode':'client'}}])
            entered, unblock, disposed = Event(), Event(), Event()
            original = outline._collect_source_tags
            def blocked(path, text):
                if path == 'left.py':
                    entered.set(); assert unblock.wait(10)
                return original(path, text)
            try:
                def dispose():
                    dispose_tool_env(env); disposed.set()
                bind_tool_environment(execution, group.environment_release(OWNER, dispose))
                execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted(); execution.mark_dispatch_started()
                with patch.object(outline, '_collect_source_tags', new=blocked), ThreadPoolExecutor(max_workers=4) as workers:
                    scanning = workers.submit(group.scan, OWNER, roots[0]); assert entered.wait(5)
                    assert workers.submit(group.scan, OWNER, roots[1]).result(timeout=5) == outline.scan(str(roots[1]))
                    waiting = workers.submit(group.scan, OWNER, roots[0])
                    closing = workers.submit(execution.settle, outcome)
                    assert disposed.wait(3)
                    assert env.disposed and not closing.done()
                    try:
                        waiting.result(timeout=3)
                    except RuntimeError as error:
                        assert 'closed' in str(error)
                    else:
                        raise AssertionError('queued root scan admitted after close')
                    assert pool.snapshot()['reserved_bytes'] == MIB
                    unblock.set(); scanning.result(timeout=5); receipt = closing.result(timeout=5)
                    assert receipt.resource_dispositions == (('tool_environment', ResourceDisposition.RELEASED),)
                    assert pool.snapshot()['reserved_bytes'] == 0
            finally:
                unblock.set(); group.close(OWNER); dispose_tool_env(env)
        assert count_tool_envs() == initial_envs
    print('canonical TaskRuntime principal, two-root actual tools/quota, concurrent roots and four terminal outcomes passed')


if __name__ == '__main__':
    check()
