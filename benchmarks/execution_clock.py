"""Offline producer-clock overhead: capture CPU and serialized event growth.

This microbenchmark excludes storage, network and model work. It never uses
live application state; event reuse matches no production mutation semantics.
"""
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='tofu-clock-benchmark-') as data_root:
        os.environ['TOFU_DATA_DIR'] = data_root
        os.environ['TOFU_CODEX_SHARED_LOGIN'] = '0'
        from lib.agent_core.execution_clock import ExecutionClock, stamp_execution_clock
        task = {'id': 'synthetic-task', '_executionClock': ExecutionClock.start()}
        baseline = {'type': 'delta', 'content': 'synthetic token'}
        samples = []
        repetitions = 100_000
        for _ in range(5):
            started = time.perf_counter_ns()
            for _ in range(repetitions):
                event = dict(baseline)
                stamp_execution_clock(task, event)
            samples.append((time.perf_counter_ns() - started) / repetitions)
        ingress_samples = []
        origin = task['_executionClock']
        for _ in range(5):
            started = time.perf_counter_ns()
            for _ in range(repetitions):
                ExecutionClock.start(origin_at_ms=origin.origin_at_ms,
                                     origin_monotonic_ns=origin.origin_monotonic_ns)
            ingress_samples.append((time.perf_counter_ns() - started) / repetitions)
        from lib.tasks_pkg.handlers.code_exec import _command_progress_timing
        command_round = {'execStartTs': origin.now_ms(), 'deadlineTs': origin.now_ms() + 30000}
        command_samples = []
        for _ in range(5):
            started = time.perf_counter_ns()
            for _ in range(repetitions):
                timing = _command_progress_timing(task, command_round)
            command_samples.append((time.perf_counter_ns() - started) / repetitions)
        compact = lambda value: json.dumps(value, separators=(',', ':')).encode()
        print(json.dumps({
            'eventsPerSample': repetitions, 'samples': len(samples),
            'captureWithEventCopyMedianNs': round(statistics.median(samples), 1),
            'additionalSerializedBytesPerEvent': len(compact(event)) - len(compact(baseline)),
            'capturedOriginClockMedianNs': round(statistics.median(ingress_samples), 1),
            'commandProgressSampleMedianNs': round(statistics.median(command_samples), 1),
            'additionalCommandTimingFieldsBytes': (len(compact({'type': 'tool_progress', **timing}))
                                                    - len(compact({'type': 'tool_progress'}))),
            'clockIdAllocatedOncePerTask': True,
            'includesStorageOrNetwork': False,
        }, sort_keys=True))


if __name__ == '__main__':
    main()
