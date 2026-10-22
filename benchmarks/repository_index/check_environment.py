"""Compose experimental cache release with actual custom-tool environment cleanup.

One execution resource stops client handoffs before joining cache scans. Tests
exercise real mint/dispose/waiter owners; no production registry is installed.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import tempfile
from unittest.mock import patch
from uuid import uuid4

from budget import CacheBudgetPlan
from quota import CacheReservationPool
from index import outline
from lib.agent_core.execution_session import (
    ExecutionSession, ExecutionPhase, ResourceDisposition, bind_tool_environment,
)
from lib.tools.tool_env import (
    mint_tool_env, dispose_tool_env, count_tool_envs,
    request_client_tool_result, resolve_client_tool_result,
)

OWNER = 900001


def environment():
    return mint_tool_env(owner=f'owner:{OWNER}', tools=[{
        'type': 'function', 'function': {
            'name': 'custom__fixture', 'description': 'offline lifecycle fixture',
            'parameters': {'type': 'object', 'properties': {}},
        }, 'execution': {'mode': 'client'},
    }])


def compose_release(env, scope):
    def release():
        # Stop environment waiters before a cache join that can block. A
        # failure in either component still attempts the other component.
        primary = None
        try:
            dispose_tool_env(env)
        except Exception as exc:
            primary = exc
        try:
            scope.close(OWNER)
        except Exception as exc:
            if primary is None:
                raise
            primary.add_note('cache release also failed: '+type(exc).__name__)
        if primary is not None:
            raise primary
    return release


def main():
    initial_envs = count_tool_envs()
    with tempfile.TemporaryDirectory(prefix='tofu-index-environment-') as folder:
        root = Path(folder)
        (root/'module.py').write_text('def fixture():\n    return 1\n')
        for outcome in (ExecutionPhase.COMPLETED, ExecutionPhase.FAILED,
                        ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
            pool = CacheReservationPool(CacheBudgetPlan(2*1024*1024, 1024*1024, 2, 'fixture'))
            execution = ExecutionSession(execution_id=uuid4().hex, kind='task', owner_user_id=OWNER)
            env = environment(); neighbor = environment()
            scope = pool.create_scope(OWNER, execution.execution_id, str(root), epoch_source=lambda: 0)
            release = compose_release(env, scope)
            task = {'id': execution.execution_id, '_userId': OWNER, '_tool_env': env}
            other = {'id': uuid4().hex, '_userId': OWNER, '_tool_env': neighbor}
            entered = Event(); unblock = Event(); registered = Event(); other_registered = Event()
            original = outline._collect_source_tags

            def blocked(relative, text):
                entered.set()
                assert unblock.wait(10)
                return original(relative, text)

            try:
                bind_tool_environment(execution, release)
                execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted()
                execution.mark_dispatch_started()
                with patch.object(outline, '_collect_source_tags', new=blocked), ThreadPoolExecutor(max_workers=4) as workers:
                    try:
                        scanning = workers.submit(scope.scan, OWNER)
                        assert entered.wait(10)
                        waiting = workers.submit(request_client_tool_result, 'same-call', task=task,
                                                 timeout=10, on_registered=registered.set)
                        adjacent = workers.submit(request_client_tool_result, 'same-call', task=other,
                                                  timeout=10, on_registered=other_registered.set)
                        assert registered.wait(10) and other_registered.wait(10)
                        settling = workers.submit(execution.settle, outcome)
                        message, failed = waiting.result(timeout=3)
                        assert failed and 'disposed' in message
                        assert env.disposed and not neighbor.disposed
                        assert not settling.done() and not adjacent.done()
                        assert pool.snapshot()['reserved_bytes'] == 1024*1024
                        assert resolve_client_tool_result('same-call', 'neighbor survived',
                                                          task_id=other['id'], user_id=OWNER)
                        assert adjacent.result(timeout=3) == ('neighbor survived', False)
                    finally:
                        unblock.set(); dispose_tool_env(env); dispose_tool_env(neighbor)
                    scanning.result(timeout=10)
                    receipt = settling.result(timeout=10)
                    assert receipt.resource_dispositions == (('tool_environment', ResourceDisposition.RELEASED),)
                    assert execution.settle(outcome) is receipt
                    assert pool.snapshot()['reserved_bytes'] == 0
            finally:
                unblock.set(); release(); dispose_tool_env(neighbor); pool.stop_admission()

        # The existing acquire/bind rollback must release both composed owners.
        execution = ExecutionSession(execution_id=uuid4().hex, kind='task', owner_user_id=OWNER)
        pool = CacheReservationPool(CacheBudgetPlan(1024*1024, 1024*1024, 1, 'fixture'))
        env = environment()
        scope = pool.create_scope(OWNER, execution.execution_id, str(root), epoch_source=lambda: 0)
        release = compose_release(env, scope)
        try:
            with patch.object(execution, 'hold_resource', side_effect=ValueError('injected registration failure')):
                try:
                    bind_tool_environment(execution, release)
                except ValueError as exc:
                    assert str(exc) == 'injected registration failure'
                else:
                    raise AssertionError('registration failure swallowed')
            assert env.disposed and pool.snapshot()['reserved_bytes'] == 0
        finally:
            release(); pool.stop_admission()

        for env_fault, cache_fault in ((True, False), (False, True), (True, True)):
            pool = CacheReservationPool(CacheBudgetPlan(1024*1024, 1024*1024, 1, 'fixture'))
            env = environment()
            scope = pool.create_scope(OWNER, uuid4().hex, str(root), epoch_source=lambda: 0)
            release = compose_release(env, scope)
            dispose = dispose_tool_env; close = scope.close

            def faulty_dispose(current):
                dispose(current)
                if env_fault:
                    raise ValueError('injected environment cleanup failure')

            def faulty_close(owner):
                if cache_fault:
                    raise RuntimeError('injected cache cleanup failure')
                close(owner)

            try:
                with patch(__name__+'.dispose_tool_env', new=faulty_dispose), patch.object(scope, 'close', new=faulty_close):
                    try:
                        release()
                    except (ValueError, RuntimeError) as exc:
                        assert str(exc) == ('injected environment cleanup failure' if env_fault
                                            else 'injected cache cleanup failure')
                        if env_fault and cache_fault:
                            assert exc.__notes__ == ['cache release also failed: RuntimeError']
                    else:
                        raise AssertionError('component cleanup failure swallowed')
                assert env.disposed
                assert pool.snapshot()['reserved_bytes'] == (1024*1024 if cache_fault else 0)
            finally:
                release(); pool.stop_admission()
            assert pool.snapshot()['reserved_bytes'] == 0
    assert count_tool_envs() == initial_envs
    print('four terminal outcomes: real client waiters unblock before cache join; neighbor isolated; quota retained until quiescence; composed bind rollback and single/dual cleanup failure retry pass')


if __name__ == '__main__':
    main()
