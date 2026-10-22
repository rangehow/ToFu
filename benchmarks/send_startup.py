#!/usr/bin/env python3
"""Offline paired startup microbenchmark; no provider, storage, or credentials.

Entry: CLI or measure(). Dependencies: production cache/snapshot authorities.
History is synthetic. Snapshot time/allocation and optional policy holds are
local overhead only, never measured click-to-provider or upstream TTFT.
"""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.llm_dispatch import cache_settle as cache
from lib.tasks_pkg.observer_snapshot import observer_snapshot
from lib.tasks_pkg.context_composer._provider_snapshot import context_provider_snapshot


def _snapshot_measure(fn, task, repeats):
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        snapshot = fn(task)
        samples.append((time.perf_counter() - started) * 1000)
        del snapshot
    gc.collect()
    tracemalloc.start()
    try:
        snapshot = fn(task)
        _, peak = tracemalloc.get_traced_memory()
        del snapshot
    finally:
        tracemalloc.stop()
    return {'median_ms': round(statistics.median(samples), 3), 'peak_bytes': peak}


def measure(history_messages=5000, events=20000, repeats=7):
    messages = [{'role': 'tool', 'content': 'synthetic result',
                 'tool_call_id': f'call_{i}'} for i in range(history_messages)]
    task = {'id': 'synthetic', '_userId': 1,
            'config': {'projectPaths': ['/synthetic'], 'model': 'test-model'},
            'messages': messages, '_turn_input_messages': list(messages),
            'events': [{'type': 'delta', 'seq': i, 'data': {'text': 'synthetic'}}
                       for i in range(events)],
            'toolRounds': [{'calls': [{'id': f'call_{i}', 'result': 'synthetic'}]}
                           for i in range(history_messages)]}
    snapshots = {
        'prior_full_task_copy': _snapshot_measure(observer_snapshot, task, repeats),
        'metadata_only': _snapshot_measure(context_provider_snapshot, task, repeats),
    }
    holds = {}
    cache._reset_settle_for_tests()
    try:
        with patch.dict('os.environ', {'TOFU_CACHE_SETTLE': '1',
                'TOFU_CACHE_SETTLE_MS': '1500',
                'TOFU_CACHE_SETTLE_MAX_MS': '4000',
                'TOFU_CACHE_SETTLE_CODEX_VISIBILITY_MS': '5000',
                'TOFU_CACHE_SETTLE_CODEX_MAX_MS': '6000',
                'TOFU_CACHE_SETTLE_CODEX_SEND_INTERVAL_MS': '4200'}):
            for profile in ('generic', 'codex'):
                conv = 'synthetic-' + profile
                cache.record_stream_end(conv, now=1000, cache_profile=profile,
                                        pending_write=True)
                with patch('lib.llm._transport.abortable_sleep'):
                    prior = cache.settle_before_send(conv, 200000, now=1000.1,
                                                    cache_profile=profile)
                with patch.dict('os.environ', {'TOFU_CACHE_SETTLE': '0'}):
                    current = cache.settle_before_send(conv, 200000, now=1000.1,
                                                      cache_profile=profile)
                holds[profile] = {'prior_policy_seconds': round(prior, 3),
                                  'default_policy_seconds': current}
    finally:
        cache._reset_settle_for_tests()
    return {'scope': 'offline local preparation; no end-to-end latency claim',
            'history_messages': history_messages, 'events': events,
            'repeats': repeats, 'snapshots': snapshots, 'cache_hold': holds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--messages', type=int, default=5000)
    parser.add_argument('--events', type=int, default=20000)
    parser.add_argument('--repeats', type=int, default=7)
    args = parser.parse_args()
    if not (0 <= args.messages <= 100000 and 0 <= args.events <= 200000
            and 1 <= args.repeats <= 30):
        parser.error('bounded inputs: messages 0..100000, events 0..200000, repeats 1..30')
    print(json.dumps(measure(args.messages, args.events, args.repeats), indent=2))


if __name__ == '__main__':
    main()
