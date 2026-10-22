"""Private public-reader binding for the native experiment only.

The unchanged AgentExecution projection calls the existing experimental native
reader, which delegates cursor interpretation to canonical replay helpers.
No production method/module is patched. Packaging and adoption remain gaps.
"""
import inspect
from types import MethodType

from native_reader import NativeReplayView, native_reader
from tofu_agent.runtime import AgentExecution
from check import module


def bind_native_public_reader(execution):
    task = execution._task
    if type(task.get('events')) is not module.ReplayWindow:
        raise TypeError('native public reader requires the exact experimental window')
    if task.get('events_lock') is None:
        raise ValueError('native public reader requires the canonical task lock')
    import textwrap
    task['_nativeReplayView'] = NativeReplayView(task['events'])
    for name in ('event_page', 'events', 'events_async'):
        method = getattr(AgentExecution, name)
        source = inspect.getsource(method)
        boundary = '        from lib.task_replay.task_replay import task_memory_replay_page'
        assert source.count(boundary) == 1, f'public {name} replay seam changed'
        source = source.replace(boundary, '        task_memory_replay_page = _candidate_reader')
        namespace = dict(method.__globals__, _candidate_reader=native_reader)
        exec(compile(textwrap.dedent(source), '<native-public-reader-experiment>', 'exec'), namespace)
        setattr(execution, name, MethodType(namespace[name], execution))
    return execution


def check():
    import threading
    from tofu_agent.models import AgentRequest
    request = AgentRequest(messages=[{'role': 'user', 'content': 'offline native-tail check'}])
    comparisons = 0
    for count in (1, 64, 4096):
        events = [{'seq': index+17, 'type': 'delta', 'content': f'event {index} 中文',
                   'details': {'detached': [index]}} for index in range(count)]
        window = module.ReplayWindow(count, count*1024)
        for event in events:
            window.append(event, 128)
        task = {'id': 'public-tail-check', 'events': window,
                'events_lock': threading.RLock(), 'status': 'running',
                '_eventBaseSeq': 17, '_eventNextSeq': 17+count}
        def execution(mapping):
            return AgentExecution(None, mapping, request, model='fixture', public_provider_id='fixture')
        candidate = bind_native_public_reader(execution(task))
        view = task['_nativeReplayView']
        for selection in (slice(0, None), slice(1, None, 1), slice(count+10, None),
                          slice(2**130, None), slice(-1, None), slice(None, None),
                          slice(0, None, True), slice(0, count-1), slice(None, None, -1)):
            assert view[selection] == events[selection]
        class Index:
            def __index__(self):
                return 1
        assert view[slice(Index(), None, Index())] == events[1:]
        try:
            view[slice(0, None, 0)]
        except ValueError:
            pass
        else:
            raise AssertionError('zero slice step accepted')
        original_method = AgentExecution.event_page
        for status in ('running', 'done', 'error', 'aborted'):
            task['status'] = status
            reference = execution(dict(task, events=events))
            for cursor in (-1, 0, 16, 17, 18, count+13, count+17, count+27):
                expected = reference.event_page(cursor=cursor)
                actual = candidate.event_page(cursor=cursor)
                assert actual == expected
                if actual['events']:
                    actual['events'][0]['content'] = 'caller mutation'
                    assert candidate.event_page(cursor=cursor) == expected
                comparisons += 1
        assert AgentExecution.event_page is original_method
        # Membership changes remain visible through the canonical task lock.
        with task['events_lock']:
            event = {'seq': count+17, 'type': 'delta', 'content': 'after append'}
            window.append(event, 128)
            events = (events+[event])[-count:]
            task['_eventBaseSeq'] = events[0]['seq']; task['_eventNextSeq'] += 1
        assert candidate.event_page(cursor=0) == execution(dict(task, events=events)).event_page(cursor=0)
        with task['events_lock']:
            window.clear(); task['_eventBaseSeq'] = task['_eventNextSeq']
        assert candidate.event_page(cursor=0) == execution(dict(task, events=[])).event_page(cursor=0)
        task.pop('_nativeReplayView'); assert window.retained_bytes == 0
    print(f'{comparisons} exact public pages, detached mutations, append/eviction/clear; global method unchanged')


if __name__ == '__main__':
    check()
