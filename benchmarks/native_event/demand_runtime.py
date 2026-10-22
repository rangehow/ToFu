"""Private measured-crossover layout policy on the actual locked producer.

Small/growing or already-observed windows remain original lists. Unobserved
windows switch only when eviction is imminent with >=1024 retained records.
First consumers retire chunks once. No application/storage default changes.
"""
import inspect
from pathlib import Path
import sys
import textwrap
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.agent_core import task_runtime
from lib.task_replay.task_replay import task_memory_replay_page
from chunked_window import ChunkedWindow
from native_reader import NativeReplayView
from chunked_runtime import adaptive_reader, normalized

MINIMUM_MIGRATION_EVENTS = 1024  # measured crossover, not a resource limit


def migrate(events, sizes, max_events, max_bytes):
    window = ChunkedWindow(max_events, max_bytes)
    if len(events) != len(sizes):
        raise ValueError('event byte ledger is not reconciled')
    width = window.block_size
    window.blocks = [events[index:index + width] for index in range(0, len(events), width)]
    window.size_blocks = [sizes[index:index + width] for index in range(0, len(sizes), width)]
    window.count = len(events)
    window.retained_bytes = sum(sizes)
    return window


def demand_reader(task, cursor):
    if not task.get('_demandConsumerSeen'):
        with task['events_lock']:
            task['_demandConsumerSeen'] = True
    # Publication and replay helpers acquire the same non-reentrant task lock.
    # Marking a consumer first prevents any later migration; perform the
    # established locked conversion only after releasing this marker lock.
    if type(task.get('events')) is ChunkedWindow:
        return adaptive_reader(task, cursor)
    return task_memory_replay_page(task, cursor)


source = inspect.getsource(task_runtime)
generated = types.ModuleType('lib.agent_core.task_runtime_demand_layout_experiment')
sys.modules[generated.__name__] = generated
exec(compile(source, '<actual-demand-layout-runtime>', 'exec'), generated.__dict__)
append_source = textwrap.dedent(inspect.getsource(task_runtime.TaskRuntime.append_event))
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
append_source = append_source.replace(append, '''        if (not chunked and not task.get('_demandConsumerSeen')
                and len(events) >= MINIMUM_MIGRATION_EVENTS
                and (len(events) >= self.max_events
                     or retained_bytes + event_bytes > self.max_event_buffer_bytes)):
            window = migrate(events, retained_sizes, self.max_events, self.max_event_buffer_bytes)
            events = window
            task['events'] = window
            task['_eventRetainedSizes'] = []
            task['_nativeReplayView'] = NativeReplayView(window)
            chunked = True
        if chunked:
            trimmed = events.append(event, event_bytes)
        else:
            events.append(event)
            retained_sizes.append(event_bytes)''')
start = append_source.index('        else:\n            trimmed = max(0, len(events) - self.max_events)')
end = append_source.index("        task['_eventRetainedBytes'] = retained_bytes", start)
append_source = append_source[:start] + '''        elif chunked:
            retained_bytes = events.retained_bytes
''' + append_source[start:end] + append_source[end:]
namespace = dict(generated.__dict__, ChunkedWindow=ChunkedWindow,
                 MINIMUM_MIGRATION_EVENTS=MINIMUM_MIGRATION_EVENTS, migrate=migrate, NativeReplayView=NativeReplayView)
exec(compile(append_source, '<locked-demand-layout-append>', 'exec'), namespace)
generated.TaskRuntime.append_event = namespace['append_event']
generated.task_memory_replay_page = demand_reader


def create(variant, count):
    # Below the observed crossover, reuse the original class and methods
    # directly rather than adding even layout-dispatch overhead.
    cls = generated.TaskRuntime if variant == 'native' and count >= MINIMUM_MIGRATION_EVENTS else task_runtime.TaskRuntime
    runtime = cls('benchmark.native-retention', push_channel='', max_events=count,
                  max_event_buffer_bytes=count * 1024, max_event_bytes=1024 * 1024)
    task = runtime.create(task_id=f"demand-{'new' if variant == 'native' else 'ref'}-{count}", user_id=900001)
    return runtime, task
