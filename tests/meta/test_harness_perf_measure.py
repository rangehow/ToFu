"""Executable specification of the harness overhead benchmark's pure parts.

benchmarks/harness_perf/: the stub provider's scripted replies on all three
wire faces, per-session metric derivation and profiler stage attribution.
The full suite (two Tofu processes, references) runs via
``python3 benchmarks/harness_perf/run.py --smoke``; see docs/HARNESS_PERFORMANCE.md.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'benchmarks' / 'harness_perf'))
sys.path.insert(0, str(ROOT / 'scripts'))

import pytest  # noqa: E402
import stub_provider  # noqa: E402

pytestmark = pytest.mark.unit

MARK = '[harness-perf scenario={} rounds={} session=s1]'


def _post(base, path, body):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode()


def test_scripted_rounds_vary_arguments_and_finish():
    tools = [{'type': 'function', 'function': {'name': 'run_command'}}]
    body = {'tools': tools, 'messages': [{'role': 'user', 'content': MARK.format('shell', 2)}]}
    first = stub_provider.plan_reply('chat', body)
    assert first['tool'] == ('run_command', {'command': 'echo r0'}) and first['round'] == 0
    body['messages'] += [{'role': 'assistant', 'tool_calls': [{'id': 'a'}]}, {'role': 'tool', 'content': ''}]
    assert stub_provider.plan_reply('chat', body)['tool'] == ('run_command', {'command': 'echo r1'})
    body['messages'] += [{'role': 'assistant', 'tool_calls': [{'id': 'b'}]}, {'role': 'tool', 'content': ''}]
    final = stub_provider.plan_reply('chat', body)
    assert final['tool'] is None and final['round'] == 2
    true = {'tools': tools, 'messages': [{'role': 'user', 'content': MARK.format('true', 1)}]}
    assert stub_provider.plan_reply('chat', true)['tool'] == ('run_command', {'command': 'true'})
    assert stub_provider.plan_reply('chat', {'messages': [{'role': 'user', 'content': 'title?'}]})[
        'scenario'] == 'auxiliary'


def test_responses_and_anthropic_histories_count_rounds():
    responses = {'tools': [{'type': 'function', 'name': 'exec_command'}], 'input': [
        {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': MARK.format('shell', 3)}]},
        {'type': 'function_call', 'call_id': 'c'}, {'type': 'function_call_output', 'call_id': 'c'}]}
    assert stub_provider.plan_reply('responses', responses)['tool'] == ('exec_command', {'cmd': 'echo r1'})
    anthropic = {'tools': [{'name': 'Bash'}], 'messages': [
        {'role': 'user', 'content': [{'type': 'text', 'text': MARK.format('noop', 1)}]},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't'}]}]}
    assert stub_provider.plan_reply('anthropic', anthropic)['tool'] is None


def test_stub_serves_three_faces_and_records_prefix_stability():
    stub = stub_provider.StubProvider().start()
    try:
        message = {'role': 'user', 'content': MARK.format('chat', 0)}
        chat = _post(stub.base_url, '/v1/chat/completions', {'stream': True, 'messages': [message]})
        assert chat.rstrip().endswith('data: [DONE]')
        anthropic = _post(stub.base_url, '/v1/messages', {'stream': True, 'messages': [
            message, {'role': 'assistant', 'content': 'x'}, {'role': 'user', 'content': 'y'}]})
        assert 'message_stop' in anthropic
        responses = json.loads(_post(stub.base_url, '/v1/responses', {'input': MARK.format('chat', 0)}))
        assert responses['status'] == 'completed'
        records = json.loads(urllib.request.urlopen(stub.base_url + '/control/records').read())
        assert [r['face'] for r in records] == ['chat', 'anthropic', 'responses']
        # The second request of session s1 appends to the first one's history.
        assert records[1]['prefix_chars'] < records[1]['prompt_chars']
        assert records[1]['previous_prompt_chars'] == records[0]['prompt_chars']
    finally:
        stub.close()


def test_session_metrics_and_stage_attribution():
    import run
    records = [{'arrived_ns': 1_000_000, 'finished_ns': 2_000_000, 'tool': 'run_command', 'body_bytes': 10,
                'prompt_chars': 8},
               {'arrived_ns': 7_000_000, 'finished_ns': 8_000_000, 'tool': None, 'body_bytes': 20,
                'prompt_chars': 16, 'prefix_chars': 8, 'previous_prompt_chars': 8}]
    metrics = run.session_metrics(records, posted_ns=0, settled_ns=9_000_000)
    assert metrics['gaps_ms'] == [5.0] and metrics['ttfr_ms'] == 1.0 and metrics['settle_ms'] == 1.0
    assert metrics['prefix_ratio'] == [1.0] and metrics['final_round_reached']
    folded = ('run (lib/tasks_pkg/x.py:1);request (lib/storage/v2_client.py:9) 3\n'
              'run (lib/tasks_pkg/x.py:1);body (lib/llm/request.py:4) 1\n')
    profile = run.attribute_profile(folded)
    assert profile['samples'] == 4
    assert profile['stages']['persistence (storage client, turn lifecycle, events)'] == 0.75
    assert profile['top_self_frames'][0] == ['request (lib/storage/v2_client.py)', 0.75]
    assert run.quantiles([1, 2, 3, 4])['median'] == 2.5


def test_api_host_never_shares_the_host_global_heartbeat(tmp_path):
    """The live Tofu's wedge evidence lives in the default /tmp/tofu/heartbeat."""
    import argparse
    import run
    deployment = run.TofuDeployment.__new__(run.TofuDeployment)
    deployment.root = tmp_path
    deployment.port = 9
    deployment.api_connection_file = tmp_path / 'run' / 'storage-connection-api.json'
    deployment.args = argparse.Namespace()
    heartbeat = Path(deployment.api_environment()['TOFU_HEARTBEAT_DIR'])
    assert tmp_path in heartbeat.parents


def test_stub_bodies_never_wait_for_a_delayed_ack():
    """A reused connection gets each response in well under the 40 ms delayed-ACK timer."""
    import time
    import urllib3
    stub = stub_provider.StubProvider().start()
    try:
        pool = urllib3.HTTPConnectionPool('127.0.0.1', stub.port, maxsize=1)
        body = json.dumps({'model': 'x', 'stream': True, 'max_tokens': 8, 'messages': [
            {'role': 'user', 'content': MARK.format('noop', 3)}]}).encode()
        elapsed = []
        for _ in range(6):
            started = time.perf_counter()
            response = pool.urlopen('POST', '/v1/messages', body=body, preload_content=False,
                                    headers={'content-type': 'application/json'})
            b''.join(response.stream(65536))
            response.release_conn()
            elapsed.append(time.perf_counter() - started)
        assert sorted(elapsed)[len(elapsed) // 2] < 0.02
    finally:
        stub.close()


def test_round_profile_charges_turn_samples_to_the_first_matching_component():
    import round_profile
    lines = [
        'process 1:"x";thread (2): w;run_task (lib/t.py:1);dispatch;prepare_request (lib/llm/_sse_core.py:5) 6',
        'process 1:"x";thread (2): w;run_task (lib/t.py:1);execute_tools (lib/r.py:9);'
        'checkpoint_invocation (lib/p.py:3);Condition.wait (threading.py:1) 3',
        'process 1:"x";thread (2): w;run_task (lib/t.py:1);somewhere_else (lib/s.py:2) 1',
        'process 1:"x";thread (3): idle;Condition.wait (threading.py:1) 50',
    ]
    result = round_profile.attribute(lines, wall_ms_per_request=60.0)
    rows = {row['component']: row for row in result['rows']}
    assert result['samples'] == 10
    assert rows['request prep: prepare_request (seal, translate, recount, fingerprints)']['ms_per_request'] == 36.0
    assert rows['event barrier: tools_in_flight']['share'] == 0.3
    assert rows['other loop work']['ms_per_request'] == 6.0
