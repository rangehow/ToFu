"""Private reuse of actual TaskRuntime retention, clocks and replay helpers."""
from pathlib import Path
import sys
import types
import inspect
import textwrap

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunked_window import ChunkedWindow
from contextlib import nullcontext
from lib.agent_core.task_runtime import TaskRuntime as OriginalTaskRuntime
from lib.task_replay.task_replay import task_memory_replay_page

path = Path(__file__).with_name('runtime.py')
source = path.read_text()
boundary = 'from check import module'
assert source.count(boundary) == 1
source = source.replace(boundary, '')
source = source.replace("'lib.agent_core.task_runtime_native_experiment'",
                        "'lib.agent_core.task_runtime_chunked_experiment'")
namespace = {'__name__': __name__ + '.bound', '__file__': str(path),
             'module': types.SimpleNamespace(ReplayWindow=ChunkedWindow)}
exec(compile(source, '<actual-task-runtime-chunked-window>', 'exec'), namespace)
generated = namespace['generated']
chunked_append = generated.TaskRuntime.append_event


def adaptive_reader(task, cursor):
    # New windows are write-optimized until the first actual consumer. Retire
    # chunk references and use the original list path thereafter, preserving
    # read performance and the canonical legacy reconciliation boundary.
    if type(task.get('events')) is ChunkedWindow:
        lock = task.get('events_lock')
        with lock if lock is not None else nullcontext():
            window = task.get('events')
            if type(window) is ChunkedWindow:
                values = window.tail(0)
                sizes = list(window.size_blocks[0][window.head:]) if window.size_blocks else []
                for block in window.size_blocks[1:]:
                    sizes.extend(block)
                assert len(values) == len(sizes)
                task['events'] = values
                task['_eventRetainedSizes'] = sizes
                task['_eventRetainedBytes'] = window.retained_bytes
                task.pop('_nativeReplayView', None)
    return task_memory_replay_page(task, cursor)


# Choose the layout inside the original event lock, before any append. This
# avoids a reader converting to a list between producer dispatch and mutation,
# and leaves persistence callbacks outside that lock exactly as before.
append_source = textwrap.dedent(inspect.getsource(OriginalTaskRuntime.append_event))
reconcile = '''        retained_sizes, retained_bytes = self._reconcile_event_retention(
            task, events)'''
assert append_source.count(reconcile) == 1
append_source = append_source.replace(reconcile, '''        chunked = type(events) is ChunkedWindow
        if chunked:
            retained_sizes, retained_bytes = [], events.retained_bytes
        else:
            retained_sizes, retained_bytes = self._reconcile_event_retention(task, events)''')
append = '''        events.append(event)
        retained_sizes.append(event_bytes)'''
assert append_source.count(append) == 1
append_source = append_source.replace(append, '''        if chunked:
            trimmed = events.append(event, event_bytes)
        else:
            events.append(event)
            retained_sizes.append(event_bytes)''')
start = append_source.index('        else:\n            trimmed = max(0, len(events) - self.max_events)')
end = append_source.index("        task['_eventRetainedBytes'] = retained_bytes", start)
append_source = append_source[:start] + '''        elif chunked:
            retained_bytes = events.retained_bytes
''' + append_source[start:end].replace('        else:', '        else:', 1) + append_source[end:]
append_namespace = dict(generated.__dict__, ChunkedWindow=ChunkedWindow)
exec(compile(append_source, '<atomic-adaptive-chunked-append>', 'exec'), append_namespace)
generated.TaskRuntime.append_event = append_namespace['append_event']
generated.task_memory_replay_page = adaptive_reader
namespace['native_reader'] = adaptive_reader
create = namespace['create']
normalized = namespace['normalized']
check_runtime = namespace['check']
