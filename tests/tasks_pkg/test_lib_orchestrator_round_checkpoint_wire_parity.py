"""Slice 20 wire-parity: _round_checkpoint.py extraction from _run.py."""

import inspect

import pytest

import lib.tasks_pkg.orchestrator._round_checkpoint as _round_checkpoint
pytestmark = pytest.mark.unit


class TestRoundCheckpointWireParity:
    def test_module_exists(self):
        assert _round_checkpoint is not None

    def test_helper_callable(self):
        assert callable(_round_checkpoint.run_round_checkpoint_and_close)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(
            _round_checkpoint.run_round_checkpoint_and_close)
        params = set(sig.parameters.keys())
        assert {'task', 'round_num', 'tid'} <= params

    def test_every_round_end_takes_a_sampled_checkpoint_without_a_durability_fence(self, monkeypatch):
        calls = []
        monkeypatch.setattr(_round_checkpoint, 'checkpoint_task_partial',
            lambda task, **kw: calls.append(('checkpoint', kw)))
        monkeypatch.setattr(_round_checkpoint, 'append_event',
            lambda task, event: calls.append(('event', event)))
        for round_num in (2, 3):
            calls.clear()
            _round_checkpoint.run_round_checkpoint_and_close({}, round_num=round_num, tid='test')
            assert calls[0] == ('checkpoint', {'force': True, 'sampled': True})
            assert calls[1][1]['type'] == 'round_end'

    def test_checkpoint_failure_is_non_fatal(self):
        src = inspect.getsource(
            _round_checkpoint.run_round_checkpoint_and_close)
        assert "except Exception" in src
        assert "non-fatal" in src

    def test_body_emits_round_end_reason_tools(self):
        src = inspect.getsource(
            _round_checkpoint.run_round_checkpoint_and_close)
        assert "reason='tools'" in src
        assert "EventType.ROUND_END" in src

    def test_step_ordering_checkpoint_before_round_end(self):
        src = inspect.getsource(
            _round_checkpoint.run_round_checkpoint_and_close)
        i_checkpoint = src.index("checkpoint_task_partial")
        i_round_end = src.index("EventType.ROUND_END")
        assert i_checkpoint < i_round_end

class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        import lib.tasks_pkg.orchestrator._root_agent_loop as _root_agent_loop
        src = inspect.getsource(_root_agent_loop)
        assert "run_round_checkpoint_and_close(" in src

    def test_run_task_no_longer_carries_block_inline(self):
        import lib.tasks_pkg.orchestrator._run as _run
        src = inspect.getsource(_run.run_task)
        assert "checkpoint_task_partial(task, force=True)" not in src
        assert "roundNum=round_num, reason='tools')" not in src
