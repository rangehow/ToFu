"""Reservation races, generation safety, constructor rollback and owned close."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
import tempfile
from unittest.mock import patch

from budget import CacheBudgetPlan
from quota import CacheQuotaExceeded, CacheReservationPool
from index import outline
from lib.agent_core.execution_session import ExecutionSession, ExecutionPhase, bind_tool_environment

OWNER = 900001


def main():
    pool = CacheReservationPool(CacheBudgetPlan(4096, 1024, 4, 'fixture'))
    barrier = Barrier(16)
    release = Event()
    def contend(index):
        barrier.wait(timeout=10)
        try:
            reservation = pool.reserve(OWNER + index % 2, f'execution-{index}')
        except CacheQuotaExceeded:
            return False
        try:
            assert release.wait(10)
            return True
        finally:
            reservation.release(OWNER + index % 2)
    with ThreadPoolExecutor(max_workers=16) as workers:
        futures = [workers.submit(contend, index) for index in range(16)]
        try:
            # All losing contenders terminate while the four holders stay live.
            from concurrent.futures import wait, FIRST_COMPLETED
            wait(futures, timeout=10, return_when=FIRST_COMPLETED)
            state = pool.snapshot()
            assert state['active_scopes'] == 4 and state['reserved_bytes'] == 4096
        finally:
            release.set()
        assert sum(future.result(timeout=10) for future in futures) == 4
    assert pool.snapshot()['reserved_bytes'] == 0
    old = pool.reserve(OWNER, 'reused')
    try:
        old.release(OWNER + 1)
    except PermissionError:
        pass
    else:
        raise AssertionError('foreign reservation release accepted')
    try:
        pool.reserve(OWNER + 1, 'reused')
    except ValueError:
        pass
    else:
        raise AssertionError('duplicate execution reservation accepted')
    old.release(OWNER)
    current = pool.reserve(OWNER, 'reused')
    old.release(OWNER)
    assert pool.snapshot()['reserved_bytes'] == 1024
    current.release(OWNER)
    with tempfile.TemporaryDirectory(prefix='tofu-index-quota-') as folder:
        root = Path(folder); (root/'module.py').write_text('def indexed():\n    return 1\n')
        def failed_factory(*args, **kwargs):
            raise RuntimeError('constructor fault')
        try:
            pool.create_scope(OWNER, 'failed', str(root), epoch_source=lambda: 0, scope_factory=failed_factory)
        except RuntimeError as error:
            assert str(error) == 'constructor fault'
        else:
            raise AssertionError('constructor fault hidden')
        assert pool.snapshot()['reserved_bytes'] == 0
        scope = pool.create_scope(OWNER, 'owned', str(root), epoch_source=lambda: 0)
        entered = Event(); finish = Event(); closing = Event(); closed = Event()
        original = outline._collect_source_tags
        def blocked(relative, text):
            entered.set(); assert finish.wait(10)
            return original(relative, text)
        def close():
            closing.set(); scope.close(OWNER); closed.set()
        with patch.object(outline, '_collect_source_tags', new=blocked), ThreadPoolExecutor(max_workers=2) as workers:
            scanning = workers.submit(scope.scan, OWNER)
            try:
                assert entered.wait(10)
                cleanup = workers.submit(close)
                assert closing.wait(10) and not closed.wait(.05)
                assert pool.snapshot()['reserved_bytes'] == 1024
            finally:
                finish.set()
            scanning.result(timeout=10); cleanup.result(timeout=10)
        assert pool.snapshot()['reserved_bytes'] == 0
        scope.close(OWNER)
        failed_cleanup = pool.create_scope(OWNER, 'cleanup-fault', str(root), epoch_source=lambda: 0)
        failed_cleanup.scan(OWNER)
        with patch.object(failed_cleanup._scope, 'close', side_effect=RuntimeError('cleanup fault')):
            try:
                failed_cleanup.close(OWNER)
            except RuntimeError as error:
                assert str(error) == 'cleanup fault'
            else:
                raise AssertionError('cleanup fault hidden')
            assert pool.snapshot()['reserved_bytes'] == 1024
        failed_cleanup.close(OWNER)
        assert pool.snapshot()['reserved_bytes'] == 0
        from uuid import uuid4
        for outcome in (ExecutionPhase.COMPLETED, ExecutionPhase.FAILED,
                        ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
            execution_id = 'quota-'+uuid4().hex
            execution = ExecutionSession(execution_id=execution_id, kind='task', owner_user_id=OWNER)
            scoped = pool.create_scope(OWNER, execution_id, str(root), epoch_source=lambda: 0)
            try:
                bind_tool_environment(execution, lambda: scoped.close(OWNER))
                execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted()
                execution.mark_dispatch_started()
                scoped.scan(OWNER)
                assert pool.snapshot()['reserved_bytes'] == 1024
                execution.settle(outcome)
                assert pool.snapshot()['reserved_bytes'] == 0
            finally:
                execution.settle(ExecutionPhase.FAILED, cause='benchmark_cleanup')
                scoped.close(OWNER)
        execution_id = 'bind-fault-'+uuid4().hex
        execution = ExecutionSession(execution_id=execution_id, kind='task', owner_user_id=OWNER)
        scoped = pool.create_scope(OWNER, execution_id, str(root), epoch_source=lambda: 0)
        try:
            scoped.scan(OWNER)
            with patch.object(execution, 'hold_resource', side_effect=RuntimeError('registration fault')):
                try:
                    bind_tool_environment(execution, lambda: scoped.close(OWNER))
                except RuntimeError as error:
                    assert str(error) == 'registration fault'
                else:
                    raise AssertionError('registration fault hidden')
            assert execution.is_terminal and pool.snapshot()['reserved_bytes'] == 0
        finally:
            execution.settle(ExecutionPhase.FAILED, cause='benchmark_cleanup')
            scoped.close(OWNER)
    pool.stop_admission()
    try:
        pool.reserve(OWNER, 'late')
    except RuntimeError:
        pass
    else:
        raise AssertionError('stopped reservation admission reopened')
    print('16 concurrent admissions, exact quota, foreign/duplicate denial, stale release, constructor rollback and close-before-release passed')


if __name__ == '__main__':
    main()
