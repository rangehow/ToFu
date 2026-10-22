"""Multi-root tiny-quota/concurrent scans and accounting-failure precedence."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from uuid import uuid4

from budget import CacheBudgetPlan
from index import outline
from quota import CacheReservationPool
from task_indexes import ExecutionRepositoryIndexes, ResizableIndexScope

OWNER=900001


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-index-pressure-') as folder:
        roots=[Path(folder)/str(index) for index in range(3)]
        for index,root in enumerate(roots):
            root.mkdir();(root/'module.py').write_text(''.join(
                f'def symbol_{index}_{number}(value):\n    return value+{number}\n' for number in range(12)))
        expected={str(root):outline.scan(str(root)) for root in roots}
        pool=CacheReservationPool(CacheBudgetPlan(1024,1024,1,'tiny-fixture'))
        def observe():
            with group._condition:
                committed=sum(group._limits.values())+sum(
                    used for root,used in group._used.items() if root not in group._limits)
                assert committed<=1024
        class ObservedScope(ResizableIndexScope):
            def scan(self,owner,**options):
                observe();return super().scan(owner,**options)
        group=ExecutionRepositoryIndexes(pool,OWNER,uuid4().hex,roots,
            epoch_source=lambda root:0,scope_factory=ObservedScope)
        try:
            def run(index):
                root=roots[index%len(roots)];result=group.scan(OWNER,root)
                assert result==expected[str(root)];observe()
            with ThreadPoolExecutor(max_workers=8) as workers:
                list(workers.map(run,range(48)))
            assert group.retained_bytes(OWNER)<=1024 and group._active==0
        finally:group.close(OWNER)
        assert pool.snapshot()['reserved_bytes']==0

        class BrokenAccounting(ResizableIndexScope):
            def scan(self,owner,**options):raise ValueError('original scan failure')
            def retained_bytes(self,owner):raise OSError('injected accounting failure')
        group=ExecutionRepositoryIndexes(pool,OWNER,uuid4().hex,roots,
            epoch_source=lambda root:0,scope_factory=BrokenAccounting)
        try:
            try:group.scan(OWNER,roots[0])
            except ValueError as error:
                assert str(error)=='original scan failure'
                assert any('OSError' in note for note in error.__notes__)
            else:raise AssertionError('original failure replaced/swallowed')
            assert group._active==0 and not group._busy and not group._limits
            assert pool.snapshot()['reserved_bytes']==1024
        finally:group.close(OWNER)
        assert pool.snapshot()['reserved_bytes']==0
    print('48 concurrent three-root tiny-quota outputs match; leases stay bounded; original scan failure/accounting notes and release preserved')


if __name__=='__main__':check()
