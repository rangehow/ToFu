"""Safety and accounting contract of the agent-loop load test.

The harness drives the real execution host, so its isolation reaches further
than the storage-only harness: the driver, the fake model and the execution
host all run with HOME/TMPDIR/runtime data under the private root, and the
execution host attaches only to the authority the driver started. Storage
latency is the execution host's own ``tofu_storage_rpc_seconds`` interval
delta, read the same way as production's ``/metrics`` export so a load phase
and a production window compare operation by operation.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'agent_loop_load_test.py'


@pytest.fixture(scope='module')
def harness():
    spec = importlib.util.spec_from_file_location('agent_loop_load_test', _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules['agent_loop_load_test'] = module
    spec.loader.exec_module(module)
    return module


def test_every_process_runs_under_the_private_root_and_attaches_only_to_its_authority(harness, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', '/live/tofu/data/native-storage-connection.json')
    monkeypatch.setenv('TOFU_DATA_DIR', '/live/tofu')
    monkeypatch.setenv('HOME', '/home/live')
    engine = tmp_path / 'engine' / 'tofu-db'
    driver = harness._driver_environment(tmp_path, engine, 'a' * 64, 'normal', 'tofudb')

    for key in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'TOFU_DATA_DIR'):
        assert Path(driver[key]).is_relative_to(tmp_path), key
    # The execution host admits only the source tree it runs from.
    assert driver['TOFU_PROJECT_PATH'] == str(harness.PROJECT_ROOT)
    assert 'TOFU_STORAGE_CONNECTION_FILE' not in driver
    assert driver['TOFU_STORAGE_RPC_DEADLINE_S'] == '20'

    connection = tmp_path / 'run' / 'storage-connection.json'
    execution = harness._execution_environment(driver, connection)
    assert execution['TOFU_STORAGE_CONNECTION_FILE'] == str(connection)
    assert execution['TOFU_PROCESS_ROLE'] == 'execution'
    assert not [key for key in execution if key.startswith('TOFU_DB_')]
    assert execution['TOFU_DATA_DIR'] == driver['TOFU_DATA_DIR']


def test_a_root_inside_a_live_checkout_is_refused_before_anything_starts(harness, tmp_path):
    checkout = tmp_path / 'tofu'
    (checkout / 'data').mkdir(parents=True)
    (checkout / 'data' / 'native-storage-connection.json').write_text('{}')
    with pytest.raises(SystemExit, match='inside a live Tofu checkout'):
        harness.main(['--root', str(checkout / 'data' / 'scratch'), '--backend', 'sqlite'])
    assert not (checkout / 'data' / 'scratch' / 'results').exists()


def _series(counts, total, count, *, operation='record.get', kind='query'):
    return {'labels': {'kind': kind, 'operation': operation, 'outcome': 'success'},
            'buckets': [0.001, 0.01, 0.1], 'counts': counts, 'sum': total, 'count': count}


def test_storage_latency_is_the_interval_delta_of_the_agent_process_histogram(harness):
    before = [_series([10, 10, 10], 0.005, 10)]
    after = [_series([10, 60, 110], 5.505, 110), _series([0, 2, 2], 0.01, 2, operation='turn.get')]
    delta = harness.histogram_delta(before, after)

    record = delta[('query', 'record.get', 'success')]
    assert record['counts'] == [0, 50, 100] and record['count'] == 100
    table = harness.operation_table(delta, elapsed=10.0)
    assert table['query:record.get']['per_s'] == 10.0
    assert table['query:record.get']['mean_ms'] == pytest.approx(55.0)
    # Half of the interval's samples lie in (1 ms, 10 ms]: the median is that bucket's top.
    assert table['query:record.get']['p50_ms'] == pytest.approx(10.0)
    assert table['query:record.get']['p99_ms'] == pytest.approx(98.2)
    assert table['query:turn.get']['n'] == 2


def test_a_quantile_beyond_the_last_bucket_is_reported_as_a_bound(harness):
    table = harness.operation_table({('command', 'x', 'success'): _series([0, 0, 1], 3.0, 4)}, elapsed=1.0)
    assert table['command:x']['p50_ms'] == '>100'


def test_production_metrics_parse_to_the_same_series_the_execution_control_returns(harness):
    from lib import observability
    observability.reset_for_tests()
    try:
        for seconds in (0.0004, 0.002, 0.03, 0.03, 7.0):
            observability.record_storage_rpc('query', 'record.get', seconds)
        observability.record_storage_rpc('command', 'turn.event.record', 0.2, 'error')
        snapshot = observability.storage_rpc_snapshot()
        parsed = harness.parse_prometheus_histogram('\n'.join(observability.prometheus_lines()),
                                                    'tofu_storage_rpc_seconds')
    finally:
        observability.reset_for_tests()
    key = lambda row: sorted(row['labels'].items())
    assert sorted(parsed, key=key) == sorted(
        [{**row, 'buckets': [float(b) for b in row['buckets']]} for row in snapshot], key=key)


def test_the_fake_model_plays_one_turn_of_the_requested_rounds_then_answers(harness):
    marker = f'[{harness.MARKER} rounds=3 turn=abc123]'
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': f'go {marker}'}]}]
    assert harness._load_marker(messages) == (3, 'abc123', 0)
    messages += [{'role': 'assistant', 'content': []},
                 {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]},
                 {'role': 'assistant', 'content': []}]
    assert harness._load_marker(messages)[2] == 2
    assert harness._tool_result_ids(messages[:3]) == ['t1']

    tools = {'run_command', 'read_files', 'search_files'}
    first = harness._round_plan('abc123', 0, 3, draining=False, tools=tools)
    assert first == harness._round_plan('abc123', 0, 3, draining=False, tools=tools)
    assert not first['final'] and first['tool']['name'] in tools
    assert harness._round_plan('abc123', 2, 3, draining=False, tools=tools)['final']
    assert harness._round_plan('abc123', 0, 3, draining=True, tools=tools)['final']
    # Only tools the request offers are called.
    assert harness._round_plan('abc123', 0, 3, draining=False, tools=set())['final']


def test_round_latency_pairs_a_tool_call_with_the_request_carrying_its_result(harness):
    state = harness.FakeModelState()
    state.streams['t1'] = (10.0, 12.5)
    state.round_latency(['t1', 'unknown'], 13.0)
    snapshot = state.snapshot(reset=True)
    assert snapshot['samples'] == {'first_token_to_next_request_s': [3.0], 'last_token_to_next_request_s': [0.5]}
    assert state.snapshot(reset=False)['samples'] == {}


def test_the_fake_model_streams_anthropic_messages_events(harness, monkeypatch):
    monkeypatch.setitem(harness.PRODUCTION_PACING, 'ttft_ms', (1, 2))
    monkeypatch.setitem(harness.PRODUCTION_PACING, 'output_tokens_per_round', (20, 40))
    state = harness.FakeModelState()
    server = harness.http.server.ThreadingHTTPServer(('127.0.0.1', 0), harness._fake_model_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = {'stream': True, 'tools': [{'name': 'run_command'}],
                'messages': [{'role': 'user', 'content': f'[{harness.MARKER} rounds=2 turn=feed]'}]}
        connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=30)
        connection.request('POST', '/v1/messages', json.dumps(body), {'Content-Type': 'application/json'})
        raw = connection.getresponse().read().decode()
    finally:
        server.shutdown()
    events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ')]
    kinds = [event['type'] for event in events]
    assert kinds[0] == 'message_start' and kinds[-2:] == ['message_delta', 'message_stop']
    blocks = [event['content_block']['type'] for event in events if event['type'] == 'content_block_start']
    assert blocks[0] == 'thinking' and blocks[-1] == 'tool_use'
    arguments = ''.join(event['delta'].get('partial_json', '') for event in events
                        if event['type'] == 'content_block_delta')
    assert json.loads(arguments)['command'].startswith('head -c ')
    assert events[-2]['delta']['stop_reason'] == 'tool_use'
    assert state.snapshot(reset=False)['counters']['tool:run_command'] == 1


def test_gil_attribution_counts_owners_and_the_busy_fraction(harness):
    folded = '\n'.join([
        'run (lib/durable_worker/service.py:1);x (lib/project_dispatch/pending_wakes.py:9) 30',
        'run (lib/tasks_pkg/x.py:1);encode (lib/llm/request.py:40) 20',
        'run (lib/tasks_pkg/x.py:1);call (lib/storage/v2_client.py:7) 25',
        'loads (json/decoder.py:3) 25',
    ])
    result = harness.gil_attribution(folded, rate_hz=100, seconds=2.0)
    assert result['samples'] == 100 and result['gil_busy_fraction'] == 0.5
    assert result['owners'] == {'project_dispatch': 0.3, 'storage client (lib/storage/)': 0.25,
                                'other': 0.25, 'llm request encoding (lib/llm/request.py)': 0.2}
