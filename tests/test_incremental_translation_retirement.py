"""An idle-retired translation queue must reject work so callers can recover."""

import queue

import pytest

from lib.translate._operation_buffer import IncrementalOperationBuffer

pytestmark = pytest.mark.unit


def test_idle_retirement_rejects_terminal_handoff_and_preview():
    buffer = IncrementalOperationBuffer(2)
    with pytest.raises(queue.Empty):
        buffer.get(0)
    # The consumer has committed to exit; accepting this loses the final
    # translation while telling the coordinator no fallback is necessary.
    assert buffer.put_terminal(('finalize', 'final response'), replace=True) == -1
    assert buffer.put_segment('text:1', 'preview') == -1


def test_terminal_consumption_rejects_duplicate_handoffs():
    buffer = IncrementalOperationBuffer(2)
    assert buffer.put_terminal(('finalize', 'final response'), replace=True) == 0
    assert buffer.get(0) == ('finalize', 'final response')
    assert buffer.put_terminal(('finalize', 'duplicate'), replace=True) == -1


def test_queued_terminal_can_be_replaced_before_consumption():
    buffer = IncrementalOperationBuffer(2)
    buffer.put_terminal(('finalize', 'first'), replace=True)
    assert buffer.put_terminal(('stamp',), replace=True) == 0
    assert buffer.get(0) == ('stamp',)


def test_retired_accumulator_handoff_declines_for_whole_turn_fallback(monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        'id': 'retiring-task', 'convId': 'conversation-retiring',
        '_turnId': 'turn-retiring', '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    accumulator = incremental._Accumulator(task)
    # Reproduce the gap after the consumer timed out but before its finally
    # block removes the registry entry. No sleeps or real worker are needed.
    with pytest.raises(queue.Empty):
        accumulator._queue.get(0)
    monkeypatch.setattr(incremental, '_accumulators', {task['id']: accumulator})
    assert incremental.finalize_incremental(task, 'Complete response') is False
    assert incremental.finalize_incremental_stamp_only(task) is False
