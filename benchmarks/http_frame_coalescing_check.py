"""Exact stream bytes/failures and real owned source closure for coalescing."""
import asyncio
import copy
from pathlib import Path
import sys
from types import FunctionType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.http_frame_coalescing import streams
from tofu_agent import server


class Execution:
    request_id = task_id = 'owned-coalescing-check'
    model = 'fixture-model'
    def __init__(self, events):
        self.events = events
        self.closed = 0
    async def events_async(self, *, cursor):
        try:
            for event in self.events:
                yield event
        finally:
            self.closed += 1


def fixed_clock(function):
    clone = FunctionType(function.__code__, dict(function.__globals__, time=SimpleNamespace(time=lambda: 1700000000)),
                         argdefs=function.__defaults__)
    clone.__kwdefaults__ = function.__kwdefaults__
    return clone


async def collect(function, events):
    execution = Execution(events)
    chunks = []
    error = None
    try:
        async for chunk in fixed_clock(function)(execution, cursor=0):
            chunks.append(chunk)
    except Exception as failure:
        error = (type(failure).__name__, str(failure))
    assert execution.closed == 1
    return ''.join(chunks), error, len(chunks)


async def check():
    original = (server.create_app, server._native_task_stream, server._agent_run_stream)
    candidates = streams()
    cycle = {}; cycle['self'] = cycle
    cases = [[{'seq': 0, 'type': 'delta', 'content': '中文\n<>&'}, {'seq': 1, 'type': 'done'}],
             [{'type': 'delta', 'content': 'no sequence'}],
             [{'seq': 0, 'type': 'error', 'message': 'fixture'}],
             [{'seq': 0, 'type': 'aborted'}],
             [{'seq': 0, 'type': 'delta', 'content': 'x' * 65536}],
             [{'seq': 0, 'type': 'delta', 'extra': object()}],
             [{'seq': 0, 'type': 'delta', 'extra': cycle}],
             [{'seq': 'invalid', 'type': 'delta'}]]
    for name, candidate in candidates.items():
        baseline = getattr(server, name)
        for events in cases:
            expected = await collect(baseline, events)
            actual = await collect(candidate, events)
            assert actual[:2] == expected[:2], (name, expected[:2], actual[:2])
            assert actual[2] <= expected[2]
        execution = Execution(copy.deepcopy(cases[0]))
        generator = candidate(execution, cursor=0)
        await anext(generator)
        await generator.aclose()
        assert execution.closed == 1
    assert original == (server.create_app, server._native_task_stream, server._agent_run_stream)
    print('PASS: exact complete/partial bytes, JSON/cursor failures, terminal markers, close, globals intact')


if __name__ == '__main__':
    asyncio.run(check())
