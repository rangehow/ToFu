"""Exact demand-layout retention, no oscillation, callback and race checks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from demand_runtime import create, normalized
from chunked_window import ChunkedWindow


def check():
    for count in (64, 1024, 2048):
        reference, rt = create('reference', count)
        candidate, task = create('native', count)
        for index in range(count + 100):
            event = {'type': 'delta', 'index': index}
            reference.append_event(rt['id'], dict(event), deliver_push=False)
            candidate.append_event(task['id'], dict(event), deliver_push=False)
            assert task['_eventRetainedBytes'] == rt['_eventRetainedBytes']
            if index < count or count < 1024:
                assert type(task['events']) is list
        assert (type(task['events']) is ChunkedWindow) == (count >= 1024)
        for cursor in (-1, 0, task['_eventNextSeq'] - 4, task['_eventNextSeq'] + 100):
            assert normalized(candidate.poll(task['id'], cursor)) == normalized(reference.poll(rt['id'], cursor))
        assert type(task['events']) is list
        for index in range(100):
            candidate.append_event(task['id'], {'type': 'delta'}, deliver_push=False)
        assert type(task['events']) is list
        reference.finish(rt['id'], result='checked'); candidate.finish(task['id'], result='checked')
    runtime, task = create('native', 4096)
    runtime.poll(task['id'], 0)  # consumer subscribes before any events
    with ThreadPoolExecutor(max_workers=5) as pool:
        def produce(producer):
            return [runtime.append_event(task['id'], {'type': 'delta', 'producer': producer}, deliver_push=False)
                    for _ in range(1100)]
        futures = [pool.submit(produce, producer) for producer in range(4)]
        sequences = [seq for future in futures for seq in future.result(timeout=20)]
        assert sorted(sequences) == list(range(4400)) and type(task['events']) is list
        def callback(seq):
            pool.submit(runtime.poll, task['id'], seq).result(timeout=5)
        runtime.append_event(task['id'], {'type': 'delta'}, before_push=callback, deliver_push=False)
        assert '_pushWithheldCount' not in task
    runtime.finish(task['id'], result='checked')
    # Reuse the real publication-fault/oversize tests with private create ports.
    path = Path(__file__).with_name('runtime_faults.py'); source = path.read_text()
    boundary = 'from runtime import create, normalized'; assert source.count(boundary) == 1
    namespace = dict(__name__='demand_runtime_fault_checks', create=create, normalized=normalized)
    exec(compile(source.replace(boundary, ''), '<actual-demand-runtime-faults>', 'exec'), namespace)
    namespace['check']()
    print('PASS: actual population/eviction migration, no oscillation, early consumers, producer races and callbacks outside lock')


if __name__ == '__main__':
    check()
