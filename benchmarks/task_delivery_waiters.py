"""Measure durability-barrier wake amplification with exact owned callbacks.

Entry: CLI. Uses the production coordinator and an explicitly controlled sink;
measures waiter wakeups/CPU, not native storage throughput or provider latency.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def measure(source: Path, lanes: int) -> dict:
    name = '_delivery_waiter_measure'
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    owner = module.EventDeliveryCoordinator(workers=lanes, idle_timeout=.02)
    release = [threading.Event() for _ in range(lanes)]
    entered = [threading.Event() for _ in range(lanes)]
    waiting = [threading.Event() for _ in range(lanes)]
    finished = [threading.Event() for _ in range(lanes)]
    tasks = [module.DeliveryLane({'id': str(i), '_userId': i + 1}) for i in range(lanes)]
    counts = [0] * lanes
    errors = []
    local = threading.local()
    original_wait = threading.Condition.wait

    def wait(condition, timeout=None):
        index = getattr(local, 'index', None)
        if index is not None:
            waiting[index].set()
        result = original_wait(condition, timeout)
        if index is not None:
            counts[index] += 1
        return result

    def barrier(index):
        local.index = index
        try:
            owner.flush(tasks[index], timeout=30)
        except BaseException as error:
            errors.append(type(error).__name__)
        finally:
            finished[index].set()

    threads = []
    try:
        for index, lane in enumerate(tasks):
            def persist(index=index):
                entered[index].set()
                assert release[index].wait(30)
            owner.submit(lane, 1, 128, persist, lambda: None)
        assert all(event.wait(20) for event in entered)
        with patch.object(threading.Condition, 'wait', wait):
            for index in range(lanes):
                thread = threading.Thread(target=barrier, args=(index,))
                thread.start()
                threads.append(thread)
            assert all(event.wait(20) for event in waiting)
            start, cpu = time.perf_counter(), time.process_time()
            for index in range(lanes):
                release[index].set()
                assert finished[index].wait(20)
            for thread in threads:
                thread.join(20)
            elapsed = (time.perf_counter() - start) * 1000
            cpu_ms = (time.process_time() - cpu) * 1000
        assert not errors and all(lane.completed == 1 for lane in tasks)
        return {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'lanes': lanes, 'wait_returns': sum(counts), 'drain_ms': elapsed,
                'process_cpu_ms': cpu_ms, 'completed': lanes,
                'pending_bytes': owner.snapshot()['pending_bytes']}
    finally:
        for event in release:
            event.set()
        for thread in threads:
            thread.join(30)
        assert owner.close(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--lanes', type=int, default=256)
    parser.add_argument('--samples', type=int, default=2)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.lanes <= 1000 or not 1 <= args.samples <= 10:
        parser.error('lanes must be 1..1000; samples must be 1..10')
    candidate = ROOT / 'lib/tasks_pkg/event_delivery.py'
    rows = []
    for sample in range(args.samples):
        order = ('reference', 'candidate') if sample % 2 == 0 else ('candidate', 'reference')
        pair = {}
        for kind in order:
            pair[kind] = measure(args.reference if kind == 'reference' else candidate, args.lanes)
        rows.append(pair)
    result = {'contract': 'tofu.task-delivery-waiters/v1', 'samples': rows,
              'scope': 'controlled sink, exact barriers; no native storage/provider timing'}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
