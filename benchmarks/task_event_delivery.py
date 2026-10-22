#!/usr/bin/env python3
"""Controlled root-event producer benchmark with an injected durable sink.

Compare the synchronous birth/adopter seam with managed task-wide delivery.
Both use the production event entry, identical event/projection inputs and
an identical injected write delay. This measures main-thread isolation and
full drain separately; it does not certify database throughput or real LLMs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_case(*, asynchronous, events, write_delay, work_delay):
    from lib.agent_core import push
    from lib.agent_core.execution_session import ExecutionPhase
    from lib import turn_lifecycle
    from lib.tasks_pkg import event_delivery
    from lib.tasks_pkg.manager import _events
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    owner = event_delivery.EventDeliveryCoordinator(workers=2, capacity=events + 8,
        byte_capacity=64 * 1024 * 1024, idle_timeout=.01)
    original_owner = event_delivery._COORDINATOR
    original_record, original_push = turn_lifecycle.record_task_event, push.push_event
    task = chat_task_runtime.create(user_id=1, task_id='bench-' + uuid.uuid4().hex)
    task.update(_attemptId='bench-attempt', convId='bench-conversation',
        _turnId='bench-turn', status='running', content='', thinking='',
        config={}, toolRounds=[], model='fixture-model', content_lock=threading.Lock())
    if asynchronous:
        task['_workerStartedAt'] = time.time()
    persisted, observed, durable_sequences = [], [], set()
    transaction_batches = []
    def persist(task, frame, **kwargs):
        if write_delay:
            time.sleep(write_delay)
        source = kwargs.get('projection_source') or task
        preceding = (kwargs.get('task_event') or {}).get('preceding_events', [])
        transaction_batches.append(len(preceding) + 1)
        cumulative = persisted[-1][1] if persisted else ''
        for row in preceding:
            cumulative += row['event']['content']
            persisted.append((row['sequence'], cumulative))
            durable_sequences.add(row['sequence'])
        persisted.append((frame['seq'], source['content']))
        durable_sequences.add(frame['seq'])
        return 'carried'
    def publish(channel, task_id, frame, **kwargs):
        assert frame['seq'] in durable_sequences
        observed.append(frame['seq'])
    event_delivery._COORDINATOR = owner
    turn_lifecycle.record_task_event = persist
    push.push_event = publish
    try:
        started, main_cpu, process_cpu = time.perf_counter(), time.thread_time(), time.process_time()
        for sequence in range(events):
            if work_delay:
                time.sleep(work_delay)
            task['content'] += 'x'
            _events.append_event(task, {'type': 'delta', 'content': 'x'})
        main_ms = (time.perf_counter() - started) * 1000
        main_cpu_ms = (time.thread_time() - main_cpu) * 1000
        event_delivery.flush_task_events(task)
        total_ms = (time.perf_counter() - started) * 1000
        process_cpu_ms = (time.process_time() - process_cpu) * 1000
        expected = [(sequence, 'x' * (sequence + 1)) for sequence in range(events)]
        assert persisted == expected and observed == list(range(events))
        return dict(main_ms=main_ms, total_ms=total_ms, main_cpu_ms=main_cpu_ms,
                    process_cpu_ms=process_cpu_ms, residency=owner.snapshot(),
                    authority_transactions=len(transaction_batches), max_batch_frames=max(transaction_batches),
                    output_sha256=hashlib.sha256(json.dumps(persisted).encode()).hexdigest())
    finally:
        assert owner.close(3)
        event_delivery._COORDINATOR = original_owner
        turn_lifecycle.record_task_event, push.push_event = original_record, original_push
        task['_executionSession'].settle(ExecutionPhase.CANCELLED)
        chat_task_runtime.discard(task['id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=64)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.events <= 10000 or not 1 <= args.samples <= 20:
        parser.error('events must be 1..10000 and samples 1..20')
    with tempfile.TemporaryDirectory(prefix='tofu-event-measure-') as runtime:
        os.environ['TOFU_DATA_DIR'] = runtime
        os.environ['TOFU_LOG_DIR'] = runtime
        results = []
        for delay in (0.0, .002, .02):
            paired = []
            for sample in range(args.samples):
                modes = (False, True) if sample % 2 == 0 else (True, False)
                pair = {}
                for mode in modes:
                    pair['async' if mode else 'sync'] = run_case(
                        asynchronous=mode, events=args.events, write_delay=delay, work_delay=.001)
                assert pair['sync']['output_sha256'] == pair['async']['output_sha256']
                paired.append(pair)
            sync = statistics.median(p['sync']['main_ms'] for p in paired)
            asynchronous = statistics.median(p['async']['main_ms'] for p in paired)
            results.append(dict(write_delay_ms=delay * 1000, samples=paired,
                                main_speedup=sync / asynchronous))
        payload = dict(contract='tofu.task-event-delivery-measure/v1', events=args.events,
            samples=args.samples, compute_delay_ms=1, python=sys.version.split()[0],
            source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (ROOT/'lib/tasks_pkg/event_delivery.py', ROOT/'lib/tasks_pkg/manager/_events.py')},
            scenarios=results)
        if args.output:
            args.output.write_text(json.dumps(payload, indent=2) + '\n')
        print(json.dumps({**{k:v for k,v in payload.items() if k!='scenarios'},
                         'scenarios':[{k:v for k,v in r.items() if k!='samples'} for r in results]}, indent=2))


if __name__ == '__main__':
    main()
