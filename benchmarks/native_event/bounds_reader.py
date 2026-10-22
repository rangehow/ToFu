"""One native metadata crossing per locked public replay read; no defaults.

Ephemeral length/first-event metadata cannot survive across producer mutation.
Canonical helpers still own cursor interpretation and terminal semantics.
"""
from types import FunctionType
from native_reader import NativeReplayView
from lib.task_replay.task_replay import (task_event_base_cursor, TASK_REPLAY_TERMINAL_STATUSES,
    _memory_replay_page_from_window)


class BoundsReplayView(NativeReplayView):
    def __init__(self, window):
        super().__init__(window)
        self.length, self.first = window.bounds()

    def __len__(self):
        return self.length

    def __getitem__(self, key):
        if type(key) is int and key == 0 and self.length:
            return self.first
        if (isinstance(key, slice) and key.stop is None
                and (key.step is None or type(key.step) is int and key.step == 1)
                and type(key.start) is int and key.start >= self.length):
            return []
        return super().__getitem__(key)


def bounds_reader(task, cursor):
    with task['events_lock']:
        events = BoundsReplayView(task['events'])
        base = task_event_base_cursor(task, events)
        status = str(task.get('status') or '')
        return _memory_replay_page_from_window(events, cursor, status=status,
            done=status in TASK_REPLAY_TERMINAL_STATUSES, base_cursor=base)


def bind_bounds_reader(execution):
    from public_tail import bind_native_public_reader
    # Reuse the unchanged three public port bindings, with one explicit reader
    # replacement in a private namespace. Global registry/classes stay intact.
    return FunctionType(bind_native_public_reader.__code__,
        dict(bind_native_public_reader.__globals__, native_reader=bounds_reader))(execution)
