#!/usr/bin/env python3
"""Per-round provider-request build cost against history length, in process.

Responsibility: measure what building one provider request costs as a task's
history grows, with transport and model removed. Each simulated round runs
the root loop's request path on a real-shaped history: the production round
builder ``build_round_request`` (``build_body`` with context lineage seeded
from composer carriers and chronological injections, the Request Inspector
snapshot, tool/request metadata and context telemetry) and ``prepare_request``
on the Anthropic wire (cache breakpoints, detachment, request-id digest,
translation, recount, sealing with lineage, wire evidence and the raw-archive
capture, prepared but never committed). It then appends a ``run_command`` tool
round (assistant call + tool result) and, every ten rounds, a new user Turn
with a chronological injection, as a long tool-heavy Turn grows. One task
spans every round, as within one Turn.

Usage::

    .venv/bin/python3 benchmarks/request_build_scaling.py [--rounds 400] [--every 50]

Prints, per history checkpoint, the median ms of ``build_round_request``,
the selected slot's adaptation plus ``prepare_request``, their sum, and the
sum for the same history's first request on a fresh task (``cold``) over the last ``--sample`` rounds. Run each
compared tree's copy in a fresh process with ``PYTHONPATH`` set to that tree.
Nothing is persisted: snapshot events of the unregistered task are withheld.

Dependencies: lib.tasks_pkg.orchestrator._round_request_prep,
lib.llm_dispatch._api_stream_state, lib.llm._sse_core.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODEL = 'claude-sonnet-4'
SLOT = SimpleNamespace(
    model=MODEL, logical_model=MODEL, provider_id='bench', routing_provider_id='bench',
    route_offering_id='', route_deployment_id='', protocol='anthropic',
    max_output_tokens=0, thinking_format='', inference_profile=None,
    responses_profile='', context_window=200000)


def _tools():
    from lib.conversation_sync.settled_rows import settle
    return list(settle([
        {'type': 'function', 'function': {
            'name': name, 'description': f'{name} tool. ' * 40,
            'parameters': {'type': 'object', 'properties': {
                'command': {'type': 'string', 'description': 'what to run ' * 5},
                'timeout': {'type': 'integer'}}, 'required': ['command']}}}
        for name in ('run_command', 'read_files', 'write_file', 'edit_file',
                     'search_files', 'find_files', 'web_search', 'fetch_url')]))


def initial_history():
    """System policy carrier + static prompt, first user Turn with a tail carrier."""
    from lib.tasks_pkg.context_composer import append_injection
    messages = [{'role': 'system', 'content': 'You are a coding agent. ' * 1200}]
    append_injection(messages, 'worker_policy', 'Worker policy. ' * 200)
    messages.append({'role': 'user', 'content': 'Please run the benchmark command.'})
    return messages


def append_tool_round(messages, index):
    call_id = f'call_{index:05d}'
    messages.append({'role': 'assistant', 'content': f'Running round {index}.',
                     'reasoning_content': 'I will run the command. ' * 8,
                     'thinking_signature': 'sig' + 'x' * 120,
                     'tool_calls': [{'id': call_id, 'type': 'function', 'function': {
                         'name': 'run_command',
                         'arguments': json.dumps({'command': 'true', 'timeout': 30})}}]})
    messages.append({'role': 'tool', 'tool_call_id': call_id, 'name': 'run_command',
                     'content': f'$ true\n[exit code: 0] round {index}\n' + 'output line\n' * 20})


def append_user_turn(messages, index):
    from lib.tasks_pkg.context_composer import append_injection
    messages.append({'role': 'assistant', 'content': f'Done with step {index}.'})
    messages.append({'role': 'user', 'content': f'Next step {index}, please.'})
    append_injection(messages, 'tool_progress', f'progress note {index}')


class Round:
    """One simulated task: the state it carries across its rounds."""

    def __init__(self):
        self.task = {'id': 'bench-task', 'convId': 'bench-conv', '_userId': 1,
                     '_turnId': 'bench-turn', '_attemptId': 'bench-attempt',
                     'config': {'model': MODEL}}
        self.state = SimpleNamespace(model=MODEL, preset='medium', thinking_enabled=False)
        self.tools = _tools()

    def build(self, messages, round_num):
        """The production round builder: body, Request Inspector snapshot, metadata."""
        from lib.tasks_pkg.orchestrator._round_request_prep import build_round_request
        started = time.thread_time()
        _tools_this_round, body = build_round_request(
            self.task, self.state, messages, self.tools, round_num=round_num, tid='bench',
            thinking_depth=None, temperature=1.0, max_tokens=8192, response_format=None,
            admitted_input_tokens=1000)
        return body, (time.thread_time() - started) * 1e3

    def prepare(self, body):
        """The selected slot's adaptation (reasoning compile, clamp) and ingress."""
        from lib.llm._sse_core import prepare_request
        from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
        from lib.tasks_pkg.compaction._tokens import task_round_text_identities
        started = time.thread_time()
        with task_round_text_identities(self.task):
            adapted = _adapt_stream_body_for_slot(
                SLOT, body, True, tools=None, max_tokens=None, temperature=None,
                thinking_enabled=None, preset=None, effort=None)
            plan = prepare_request(adapted, api_protocol='anthropic', api_key='k',
                                   base_url='https://bench.invalid/v1')
        elapsed = (time.thread_time() - started) * 1e3
        if plan.raw_archive_capture is not None:
            plan.raw_archive_capture.discard()
        return plan, elapsed


def run(rounds: int, every: int, sample: int):
    """Warm rounds of one task, plus at each checkpoint the same history's
    first request on a fresh task (``cold``: the first request of a Turn)."""
    messages = initial_history()
    state = Round()
    rows = []
    window = {'build': [], 'prepare': [], 'total': [], 'cold': []}
    for index in range(rounds):
        body, build_ms = state.build(messages, index)
        _plan, prepare_ms = state.prepare(body)
        if index % every >= every - sample:
            window['build'].append(build_ms)
            window['prepare'].append(prepare_ms)
            window['total'].append(build_ms + prepare_ms)
            cold = Round()
            cold_body, cold_build_ms = cold.build(messages, 0)
            window['cold'].append(cold_build_ms + cold.prepare(cold_body)[1])
        if index % every == every - 1:
            rows.append({'round': index + 1, 'messages': len(messages),
                         **{key: round(statistics.median(values), 2)
                            for key, values in window.items()}})
            window = {key: [] for key in window}
        append_tool_round(messages, index)
        if index % 10 == 9:
            append_user_turn(messages, index)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--rounds', type=int, default=400)
    parser.add_argument('--every', type=int, default=50)
    parser.add_argument('--sample', type=int, default=10)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    rows = run(args.rounds, args.every, args.sample)
    if args.json:
        print(json.dumps(rows))
        return 0
    print(f'{"round":>6} {"msgs":>6} {"build_round":>11} {"prepare":>8} {"total":>8} '
          f'{"cold":>8}  (median ms)')
    for row in rows:
        print(f'{row["round"]:>6} {row["messages"]:>6} {row["build"]:>11} '
              f'{row["prepare"]:>8} {row["total"]:>8} {row["cold"]:>8}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
