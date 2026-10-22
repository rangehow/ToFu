"""Actual execution-session settlement and acquire/bind rollback experiment.

The fixture cache is one tool-environment component. This does not install a
second environment binding or a cache registry in production task state.
"""
from pathlib import Path
import tempfile
from uuid import uuid4

from owned_scope import OwnedIndexScope
from lib.agent_core.execution_session import (
    ExecutionPhase, ExecutionSession, ResourceDisposition, bind_tool_environment,
)

OWNER = 900001


def session():
    return ExecutionSession(execution_id='repo-index-'+uuid4().hex,
                            kind='task', owner_user_id=OWNER)


def assert_closed(scope):
    assert scope.retained_bytes(OWNER) == 0
    try:
        scope.scan(OWNER)
    except RuntimeError:
        pass
    else:
        raise AssertionError('settled execution retained scan admission')


def main():
    with tempfile.TemporaryDirectory(prefix='tofu-index-session-') as folder:
        root = Path(folder)
        (root/'module.py').write_text('def indexed():\n    return 1\n')
        for outcome in (ExecutionPhase.COMPLETED, ExecutionPhase.FAILED,
                        ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
            execution = session()
            scope = OwnedIndexScope(OWNER, str(root), 1024*1024, epoch_source=lambda: 0)
            releases = []
            def release():
                releases.append(execution.execution_id)
                scope.close(OWNER)
            try:
                bind_tool_environment(execution, release)
                execution.mark_routed(); execution.mark_reserved(); execution.mark_admitted()
                execution.mark_dispatch_started()
                scope.scan(OWNER)
                assert scope.retained_bytes(OWNER) > 0
                receipt = execution.settle(outcome)
                assert ('tool_environment', ResourceDisposition.RELEASED) in receipt.resource_dispositions
                assert execution.settle(outcome) is receipt
                assert len(releases) == 1
                assert_closed(scope)
            finally:
                execution.settle(ExecutionPhase.FAILED, cause='benchmark_cleanup')
                scope.close(OWNER)
        execution = session()
        original = OwnedIndexScope(OWNER, str(root), 1024*1024, epoch_source=lambda: 0)
        acquired = OwnedIndexScope(OWNER, str(root), 1024*1024, epoch_source=lambda: 0)
        try:
            original.scan(OWNER); acquired.scan(OWNER)
            bind_tool_environment(execution, lambda: original.close(OWNER))
            try:
                bind_tool_environment(execution, lambda: acquired.close(OWNER))
            except ValueError:
                pass
            else:
                raise AssertionError('duplicate environment binding accepted')
            assert execution.is_terminal
            assert_closed(original); assert_closed(acquired)
        finally:
            execution.settle(ExecutionPhase.FAILED, cause='benchmark_cleanup')
            original.close(OWNER); acquired.close(OWNER)
    print('actual execution settlement releases cache exactly once for success/failure/cancel/timeout; failed binding rolls back acquired cache')


if __name__ == '__main__':
    main()
