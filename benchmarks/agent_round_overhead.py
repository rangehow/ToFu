"""Per-round harness overhead of the agent loop against a scripted provider.

Responsibility: drive one real storage-free ``tofu_agent`` session (the same
round loop, tool assembly, gateway dispatch, result settlement and event
fan-out the application uses) against a loopback OpenAI-compatible provider
that answers instantly. The provider stamps when each request arrives and when
its response was flushed, so the gap between one response and the next request
is pure harness time: stream parsing, argument normalization, tool dispatch,
result persistence, event fan-out and next-request assembly. The provider
costs nothing measurable, so these gaps are the overhead a real model would
add on top of its own latency.

Each session runs ``--rounds`` no-op tool rounds and a final text answer.
The round's tool is selected by ``--tool`` (default ``read_files`` on a
distinct one-line file per round): a near no-op that still crosses argument normalization,
dispatch, approval and result settlement.

Also reports the request prefix stability evidence used by
``tests/tasks_pkg/test_request_prefix_stability.py``: whether every request's
``tools`` list and system message are byte-identical to the first one.

Entry points:
  python benchmarks/agent_round_overhead.py --rounds 30 --json out.json
  python benchmarks/agent_round_overhead.py --rounds 30 --profile out.prof
Run arms in fresh alternating processes with ``PYTHONPATH`` set to the tree
under test; the script prints ``lib.__file__`` so the arm is checkable.
Depends on: ``tofu_agent.AgentRuntime``, ``tests.support.standalone_model_routing``.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OWNER = 7


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {'tool_calls': [{'index': 0, 'id': call_id, 'type': 'function',
                            'function': {'name': name,
                                         'arguments': json.dumps(arguments)}}]}


class _Provider:
    def __init__(self, rounds: int, tool: str, arguments: dict):
        self.rounds = rounds
        self.tool = tool
        self.arguments = arguments
        self.requests: list[dict] = []
        self.received: list[float] = []
        self.flushed: list[float] = []
        self.lock = threading.Lock()

    def handler(self):
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                arrived = time.perf_counter()
                request = json.loads(body)
                with provider.lock:
                    provider.requests.append(request)
                    provider.received.append(arrived)
                    index = len(provider.requests) - 1
                if index < provider.rounds:
                    delta = _tool_call(f'call-{index}', provider.tool,
                                       provider.arguments(index))
                    finish = 'tool_calls'
                else:
                    delta, finish = {'content': 'done.'}, 'stop'
                frames = [
                    {'choices': [{'index': 0, 'delta': delta}]},
                    {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                     'usage': {'prompt_tokens': 100, 'completion_tokens': 10}},
                ]
                wire = ''.join('data: ' + json.dumps(f) + '\n\n' for f in frames)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write((wire + 'data: [DONE]\n\n').encode())
                self.wfile.flush()
                with provider.lock:
                    provider.flushed.append(time.perf_counter())
                self.close_connection = True

        return Handler


def run_session(rounds: int, tool: str, workspace: Path) -> dict:
    from tests.support.standalone_model_routing import (
        standalone_model_routing_envelope)
    from tofu_agent import AgentRuntime, ModelRoutingConfig

    os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
    for index in range(rounds):
        (workspace / f'note-{index}.txt').write_text(f'{index}\n', encoding='utf-8')

    def arguments(index: int) -> dict:
        # Distinct arguments per round: the loop breaker stops identical rounds.
        return {'path': f'note-{index}.txt'} if tool == 'read_files' else {}

    provider = _Provider(rounds, tool, arguments)
    server = ThreadingHTTPServer(('127.0.0.1', 0), provider.handler())
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret='test-only',
        model_id='round-overhead')
    for row in (*envelope['model_routing']['models'],
                *envelope['model_routing']['offerings']):
        row['capabilities'].append('tools')
    started = time.perf_counter()
    try:
        with AgentRuntime.local(
                model_routing=ModelRoutingConfig.from_mapping(envelope),
                owner_user_id=OWNER, max_inflight=1) as runtime:
            result = runtime.run(
                [{'role': 'user', 'content': 'Read every note-N.txt file.'}],
                config={'tools': '*', 'projectPath': str(workspace)},
                timeout_s=600)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
    total = time.perf_counter() - started
    gaps = [(provider.received[i + 1] - provider.flushed[i]) * 1000.0
            for i in range(len(provider.flushed) - 1)]
    return {'status': result.status, 'error': result.error,
            'requests': provider.requests, 'gaps_ms': gaps,
            'first_request_ms': (provider.received[0] - started) * 1000.0
            if provider.received else None,
            'total_s': total}


def _prefix_stability(requests: list[dict]) -> dict:
    def system(request):
        return json.dumps([m for m in request.get('messages', [])
                           if m.get('role') == 'system'], sort_keys=False)

    def tools(request):
        return json.dumps(request.get('tools') or [], sort_keys=False)

    first = requests[0] if requests else {}
    return {
        'tools_stable': all(tools(r) == tools(first) for r in requests),
        'system_stable': all(system(r) == system(first) for r in requests),
        'tool_names': [str((t.get('function') or {}).get('name'))
                       for t in first.get('tools') or ()],
    }


def _summary(gaps: list[float]) -> dict:
    ordered = sorted(gaps)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {'n': len(ordered), 'median_ms': statistics.median(ordered),
            'p95_ms': p95, 'mean_ms': statistics.fmean(ordered),
            'min_ms': ordered[0], 'max_ms': ordered[-1]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--tool', default='read_files')
    parser.add_argument('--json', dest='json_path')
    parser.add_argument('--profile', dest='profile_path')
    parser.add_argument('--dump-requests', dest='requests_path')
    args = parser.parse_args(argv)
    import lib
    print(f'lib: {lib.__file__}', file=sys.stderr)
    with tempfile.TemporaryDirectory() as scratch:
        workspace = Path(scratch)
        if args.profile_path:
            import cProfile
            profiler = cProfile.Profile()
            profiler.enable()
            outcome = run_session(args.rounds, args.tool, workspace)
            profiler.disable()
            profiler.dump_stats(args.profile_path)
        else:
            outcome = run_session(args.rounds, args.tool, workspace)
    if outcome['status'] != 'done':
        print(f"session failed: {outcome['status']} {outcome['error']}",
              file=sys.stderr)
        return 1
    if args.requests_path:
        Path(args.requests_path).write_text(
            json.dumps(outcome['requests'], ensure_ascii=False), encoding='utf-8')
    report = {
        'lib': lib.__file__, 'rounds': args.rounds, 'tool': args.tool,
        'requests': len(outcome['requests']),
        'first_request_ms': outcome['first_request_ms'],
        'total_s': outcome['total_s'],
        'round_gap': _summary(outcome['gaps_ms']),
        'gaps_ms': outcome['gaps_ms'],
        'prefix': _prefix_stability(outcome['requests']),
    }
    text = json.dumps(report, indent=2)
    if args.json_path:
        Path(args.json_path).write_text(text, encoding='utf-8')
    print(json.dumps({k: report[k] for k in
                      ('requests', 'first_request_ms', 'total_s', 'round_gap')},
                     indent=2))
    print(json.dumps({k: v for k, v in report['prefix'].items()
                      if k != 'tool_names'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
