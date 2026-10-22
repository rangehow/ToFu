"""Lightweight experimental native replay view and canonical cursor reader.

No generated TaskRuntime or producer imports. Callers hold the task lock and
canonical helpers own cursor, status and terminal semantics. No product default.
"""
import sys
from lib.task_replay.task_replay import (TaskReplayPage, task_event_base_cursor,
    TASK_REPLAY_TERMINAL_STATUSES, _memory_replay_page_from_window)

class NativeReplayView(list):
    """Only a private experiment adapter; canonical cursor logic owns interpretation."""
    def __init__(self, window):
        self.window=window
    def __len__(self):
        return len(self.window)
    def __getitem__(self, key):
        if isinstance(key,slice):
            # Canonical replay supplies a nonnegative open-ended suffix. Its
            # native tail already clamps oversized offsets; avoid two extra
            # Python/native length calls just to normalize that exact shape.
            if (key.stop is None and (key.step is None or type(key.step) is int and key.step == 1)
                    and type(key.start) is int and 0 <= key.start <= sys.maxsize):
                return self.window.tail(key.start)
            start,stop,step=key.indices(len(self.window))
            if stop==len(self.window) and step==1:
                return self.window.tail(start)
            return self.window.tail(0)[key]
        return self.window[key]
    def __iter__(self):
        return iter(self.window.tail(0))

def native_reader(task, cursor):
    with task['events_lock']:
        events=task['_nativeReplayView']
        base=task_event_base_cursor(task,events)
        status=str(task.get('status') or '')
        return _memory_replay_page_from_window(events,cursor,status=status,
            done=status in TASK_REPLAY_TERMINAL_STATUSES,base_cursor=base)

