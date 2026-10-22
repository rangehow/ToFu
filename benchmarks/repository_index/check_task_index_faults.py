"""Multi-root reservation rollback, constructor reentry and cleanup-fault checks."""
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from budget import CacheBudgetPlan
from quota import CacheReservationPool
from task_indexes import ExecutionRepositoryIndexes, ResizableIndexScope
from lib.agent_core.execution_session import ExecutionSession, bind_tool_environment

OWNER=900001
MIB=1024*1024


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-task-index-faults-') as folder:
        roots=[Path(folder)/name for name in ('left','right')]
        for root in roots:
            root.mkdir(); (root/'module.py').write_text('def fixture():\n    return 1\n')
        pool=CacheReservationPool(CacheBudgetPlan(MIB,MIB,1,'fixture'))
        execution=ExecutionSession(execution_id=uuid4().hex,kind='task',owner_user_id=OWNER)
        group=ExecutionRepositoryIndexes(pool,OWNER,execution.execution_id,roots,epoch_source=lambda root:0)
        group.scan(OWNER,roots[0]);released=[]
        try:
            with patch.object(execution,'hold_resource',side_effect=ValueError('injected bind failure')):
                try:
                    bind_tool_environment(execution,group.environment_release(OWNER,lambda:released.append('environment')))
                except ValueError as error:
                    assert str(error)=='injected bind failure'
                else:
                    raise AssertionError('bind failure swallowed')
            assert released==['environment'] and pool.snapshot()['reserved_bytes']==0
        finally:
            group.close(OWNER)

        captured=[]
        def factory(*args,**kwargs):
            scope=ResizableIndexScope(*args,**kwargs);captured.append(scope);return scope
        group=ExecutionRepositoryIndexes(pool,OWNER,uuid4().hex,roots,epoch_source=lambda root:0,scope_factory=factory)
        try:
            for root in roots:group.scan(OWNER,root)
            original=captured[0].close
            def broken(owner):raise OSError('injected root close failure')
            captured[0].close=broken
            def bad_environment():raise ValueError('original environment failure')
            try:
                group.environment_release(OWNER,bad_environment)()
            except ValueError as error:
                assert str(error)=='original environment failure'
                assert any('OSError' in note for note in error.__notes__)
            else:
                raise AssertionError('cleanup faults swallowed')
            assert captured[1].retained_bytes(OWNER)==0
            assert pool.snapshot()['reserved_bytes']==MIB
            captured[0].close=original
        finally:
            captured[0].close=original;group.close(OWNER)
        assert pool.snapshot()['reserved_bytes']==0

        for action in ('close','recursive_scan'):
            def reentrant(owner,root,maximum_bytes,**options):
                if action=='close':group.close(owner)
                else:group.scan(owner,root)
                raise AssertionError('constructor reentry admitted')
            group=ExecutionRepositoryIndexes(pool,OWNER,uuid4().hex,roots,epoch_source=lambda root:0,scope_factory=reentrant)
            try:
                try:group.scan(OWNER,roots[0])
                except RuntimeError as error:assert 'active scan' in str(error) or 'recursive' in str(error)
                else:raise AssertionError('constructor reentry succeeded')
            finally:group.close(OWNER)
            assert pool.snapshot()['reserved_bytes']==0
    print('actual binding rollback; all-root cleanup attempts, retained quota/retry and primary failure; constructor close/scan reentry refused')


if __name__=='__main__':check()
