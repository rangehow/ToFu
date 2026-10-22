"""Exact rolling membership/bytes, GC and actual runtime fault checks."""
import gc
from pathlib import Path
import random
import sys
import weakref
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from chunked_window import ChunkedWindow
from chunked_runtime import create, normalized, check_runtime


def check():
    randomizer = random.Random(20261008)
    for capacity in (1, 63, 64, 65, 129):
        window = ChunkedWindow(capacity, 256)
        expected = []
        for index in range(1200):
            event = {'seq': index, 'payload': [index]}
            size = randomizer.randrange(0, 320)
            expected.append((event, size))
            trimmed = 0
            while len(expected) > capacity or (sum(s for _, s in expected) > 256 and len(expected) > 1):
                expected.pop(0)
                trimmed += 1
            assert window.append(event, size) == trimmed
            assert len(window) == len(expected) and window.retained_bytes == sum(s for _, s in expected)
            values = [e for e, _ in expected]
            for start in (0, 1, 4, len(values), len(values) + 1):
                assert window.tail(start) == values[start:]
            if values:
                assert window[0] is values[0] and window[-1] is values[-1]
    class Payload(dict):
        pass
    window = ChunkedWindow(1, 100)
    event = Payload(seq=1); reference = weakref.ref(event)
    window.append(event, 1); snapshot = window.tail(0)
    window.append({'seq': 2}, 1)
    del event
    gc.collect(); assert reference() is not None
    del snapshot
    gc.collect(); assert reference() is None
    check_runtime()
    path = Path(__file__).with_name('runtime_faults.py')
    source = path.read_text(); boundary = 'from runtime import create, normalized'
    assert source.count(boundary) == 1
    namespace = {'__name__': 'chunked_fault_checks', 'create': create, 'normalized': normalized}
    exec(compile(source.replace(boundary, ''), '<actual-chunked-runtime-fault-checks>', 'exec'), namespace)
    namespace['check']()
    reference_runtime, reference_task = create('reference', 64)
    runtime, task = create('native', 64)
    ordinary_oversize = {'type': 'delta', 'delta': 'x' * 100000}
    for selected, owned in ((reference_runtime, reference_task), (runtime, task)):
        selected.append_event(owned['id'], dict(ordinary_oversize), deliver_push=False)
        assert len(owned['events']) == 1 and owned['_eventRetainedBytes'] > selected.max_event_buffer_bytes
    assert normalized(runtime.poll(task['id'], 0)) == normalized(reference_runtime.poll(reference_task['id'], 0))
    assert type(task['events']) is list
    reference_runtime.finish(reference_task['id'], result='checked')
    runtime.finish(task['id'], result='checked')
    with ThreadPoolExecutor(max_workers=5) as pool:
        def produce(producer):
            return [runtime.append_event(task['id'], {'type': 'delta', 'producer': producer, 'offset': index},
                                         deliver_push=False) for index in range(300)]
        def consume():
            for _ in range(300):
                runtime.poll(task['id'], 0)
        # Start a fresh chunked window so reads race with the first transition.
        runtime, task = create('native', 64)
        futures = [pool.submit(produce, producer) for producer in range(4)]
        reader = pool.submit(consume)
        sequences = [value for future in futures for value in future.result(timeout=15)]
        reader.result(timeout=15)
        assert sorted(sequences) == list(range(1200)) and type(task['events']) is list
        def persist(seq):
            # The canonical durability callback must remain outside events_lock.
            pool.submit(runtime.poll, task['id'], seq).result(timeout=5)
        runtime.append_event(task['id'], {'type': 'delta', 'delta': 'callback'},
                             before_push=persist, deliver_push=False)
        assert '_pushWithheldCount' not in task
        runtime.finish(task['id'], result='checked')
    print('PASS: chunk boundaries/bytes, immediate eviction, owned snapshot references, runtime races and publication faults')


if __name__ == '__main__':
    check()
