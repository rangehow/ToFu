"""A worker refused at birth because its budget owner ended is cancelled only on Stop.

``ExecutionBudget.attach`` refuses a child with ``execution_owner_cancelled``
both when the owner was stopped and when it merely settled normally (for
example a follow-up wave's spawning turn that finished before a dependency
released the worker). Only the worker's own Stop signal may turn the refusal
into ``cancelled``; otherwise the run did not happen and stays ``failed``.
"""
import pytest

from lib.agent_core.execution_budget import ModelBudgetExceeded
from lib.swarm.agent import SubAgent
from lib.swarm.protocol import SubTaskSpec

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('stopped', [False, True])
def test_owner_refusal_at_birth_is_cancelled_only_when_stop_was_requested(monkeypatch, stopped):
    from lib.tasks_pkg import child_invocation

    def refuse(*_args, **_kwargs):
        raise ModelBudgetExceeded('execution_owner_cancelled')

    monkeypatch.setattr(child_invocation, 'create_child_invocation', refuse)
    parent = {'id': 'owner', 'convId': 'conversation', 'status': 'done',
              '_userId': 7, 'config': {}}
    worker = SubAgent(SubTaskSpec(id='dependent', role='reviewer', objective='Review'),
                      parent_task=parent, all_tools=[], model='alpha',
                      abort_check=lambda: stopped)
    result = worker.run()
    assert result.status == ('cancelled' if stopped else 'failed')
    assert result.error_message == 'execution_owner_cancelled'
