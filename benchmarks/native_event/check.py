"""Retention oracle, detached readback and reference-cycle teardown for a native candidate."""
import gc
import os
import importlib.util
from pathlib import Path
import random
import weakref

artifact = Path(os.environ['TOFU_NATIVE_EVENT_LIBRARY'])
spec = importlib.util.spec_from_file_location('tofu_event_prototype', artifact)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def check():
    window = module.ReplayWindow(64, 65536)
    original = [{'seq': index} for index in range(8)]
    for event in original:
        window.append(event, 1)
    snapshot = iter(window)
    window.append({'seq': 8}, 1)
    window.clear()
    observed = list(snapshot)
    assert observed == original and all(left is right for left, right in zip(observed, original))
    assert list(window) == [] and window.retained_bytes == 0
    event = {'owner': window}
    window.append(event, 1)
    snapshot = iter(window); witness = weakref.ref(window)
    del event, window
    gc.collect(); assert witness() is not None, 'snapshot lost owned references'
    del snapshot
    gc.collect(); assert witness() is None, 'snapshot cycle retained native window'
    randomizer = random.Random(20261007)
    operations = 0
    for maximum_count, maximum_bytes in [(1, 1), (64, 256), (4096, 65536)]:
        native = module.ReplayWindow(maximum_count, maximum_bytes)
        events, sizes = [], []
        detached = native.tail(0)
        for sequence in range(10000):
            event = {'seq': sequence, 'content': 'x' * randomizer.randrange(512)}
            size = randomizer.randrange(1, 1024)
            events.append(event)
            sizes.append(size)
            removed = 0
            while len(events) > 1 and (len(events) > maximum_count or sum(sizes) > maximum_bytes):
                del events[0]
                del sizes[0]
                removed += 1
            assert native.append(event, size) == removed
            assert native.retained_bytes == sum(sizes)
            assert len(native) == len(events)
            for offset in (0, len(events)-1, len(events), len(events)+1):
                actual = native.tail(offset)
                assert actual == events[offset:]
                assert all(left is right for left, right in zip(actual, events[offset:]))
            operations += 1
        assert detached == []
    native = module.ReplayWindow(2, 100)
    event = {'owner': native}
    native.append(event, 1)
    witness = weakref.ref(native)
    del event, native
    gc.collect()
    assert witness() is None, 'native storage leaked an event->window reference cycle'
    for action in ('eviction','clear'):
        observations=[]
        native=module.ReplayWindow(1,100)
        weak=weakref.ref(native)
        class Payload(str):
            def __del__(self):
                try:
                    owner=weak()
                    observations.append((len(owner.tail(0)),owner.retained_bytes))
                    owner.append({'text':'reentrant'},1)
                except Exception as exc:
                    observations.append(type(exc).__name__)
        native.append({'text':Payload('first')},5)
        if action=='eviction':
            native.append({'text':'next'},4)
            assert observations==[(1,4)],observations
        else:
            native.clear()
            assert observations==[(0,0)],observations
        assert native.tail(0)==[{'text':'reentrant'}]
    print(f'{operations} oracle operations, cycle teardown and finalizer reentrancy passed')

if __name__ == '__main__':
    check()
