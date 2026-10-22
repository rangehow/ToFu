"""Experimental native retention plugged into the real TaskRuntime append/poll methods.

Only isolated workers use the generated subclass. Application code, storage,
publication, event JSON accounting, clock and poll projection remain unchanged.
The experimental reader handles newly created sequenced windows; legacy and
external mutations still need a production admission design.
"""
import inspect
import sys
import types
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check import module
from lib.agent_core import task_runtime
from lib.task_replay.task_replay import (TaskReplayPage, task_event_base_cursor,
    TASK_REPLAY_TERMINAL_STATUSES, _memory_replay_page_from_window,
    task_memory_replay_page)

source = inspect.getsource(task_runtime)
old = '''            events.append(event)
            retained_sizes.append(event_bytes)'''
new = '''            trimmed = events.append(event, event_bytes)'''
assert source.count(old)==1
source=source.replace(old,new)
start=source.index('            else:\n                trimmed = max(0, len(events) - self.max_events)')
end=source.index("            task['_eventRetainedBytes'] = retained_bytes",start)
source=source[:start]+'''            else:
                retained_bytes = events.retained_bytes
'''+source[end:]
old='''            retained_sizes, retained_bytes = self._reconcile_event_retention(
                task, events)'''
assert source.count(old)==1
source=source.replace(old,'            retained_sizes, retained_bytes = [], events.retained_bytes')
generated = types.ModuleType('lib.agent_core.task_runtime_native_experiment')
sys.modules[generated.__name__]=generated
exec(compile(source, '<native-retention-experiment>', 'exec'), generated.__dict__)

from native_reader import NativeReplayView, native_reader

generated.task_memory_replay_page=native_reader

def create(variant, count):
    cls=generated.TaskRuntime if variant=='native' else task_runtime.TaskRuntime
    runtime=cls('benchmark.native-retention',push_channel='',max_events=count,
                max_event_buffer_bytes=count*1024,max_event_bytes=1024*1024)
    label='new' if variant=='native' else 'ref'
    task=runtime.create(task_id=f'fixture-{label}-{count}',user_id=900001)
    if variant=='native':
        task['events']=module.ReplayWindow(runtime.max_events,runtime.max_event_buffer_bytes)
        task['_nativeReplayView']=NativeReplayView(task['events'])
    return runtime,task

def normalized(payload):
    projected=dict(payload)
    for name in ('createdAt','updatedAt','finishedAt','taskId'):
        projected.pop(name,None)
    projected['events']=[{key:value for key,value in event.items()
                         if key not in ('executionAtMs','executionClockId','executionOriginAtMs','requestId','taskId')}
                         for event in payload['events']]
    return projected

def check():
    for events in ([], [{'seq':7}], [{'seq':'invalid'}], [{},{}], [{'seq':-1}]):
        window=module.ReplayWindow(64,65536)
        for event in events:
            window.append(event,1)
        for base in (0,5,'invalid'):
            for cursor in (None,-1,0,7,100,'invalid'):
                task={'events_lock':threading.RLock(),'status':'done',
                      '_eventBaseSeq':base,'_eventNextSeq':15,'events':events}
                expected=task_memory_replay_page(task,cursor)
                task['events']=window
                task['_nativeReplayView']=NativeReplayView(window)
                assert native_reader(task,cursor)==expected
    for count in (1,64):
        reference,rt=create('reference',count)
        native,nt=create('native',count)
        for index in range(200):
            event={'type':'content','text':'sample '+str(index)}
            assert reference.append_event(rt['id'],dict(event),deliver_push=False)==native.append_event(nt['id'],dict(event),deliver_push=False)
            assert rt['_eventRetainedBytes']==nt['_eventRetainedBytes']
            for cursor in (-1,0,index,index+1,index+100,'invalid'):
                actual=normalized(native.poll(nt['id'],cursor))
                expected=normalized(reference.poll(rt['id'],cursor))
                assert actual==expected,(actual,expected)
    print('real TaskRuntime append/poll cursor and byte-accounting parity passed')

if __name__=='__main__':
    check()
