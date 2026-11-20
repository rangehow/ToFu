"""The Rust MCP client (packages/tofu-mcp) keeps the Python bridge's call path.

Both clients connect to the same recorded MCP session
(tests/fixtures/mcp_sessions) through a ``hope-mcp``-named launcher, and every
case runs on both: the owner Hope queue guard of
test_hope_mcp_queue_policy_boundary.py (refusal before any send, fail closed
on the contract default, audit record), result projection, failure
categories, deadlines and the degraded fast-fail, replay after a dead
transport, cancellation, the catalog, the stdio environment, redaction,
argument coercion, and the execution-host relay (an unchanged Python
``RelayedMCPBridge`` against the Rust relay owner).
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import CancelledError
from types import SimpleNamespace

import pytest

from tests.mcp_protocol.rust_parity_support import (  # noqa: F401 - fixtures
    ROOT, mcp_host_binary, outcome as _outcome, python_bridge, rust_host, scene,
)
__all__ = ['mcp_host_binary', 'rust_host', 'scene']

pytestmark = pytest.mark.unit

COVERS: dict[str, list[str]] = {
    'test_hope_mcp_queue_policy_boundary.py': [
        # test_mcp_submit_to_m17_is_refused_before_the_call
        'test_m17_submission_is_refused_before_the_call_on_both',
        # test_m17_in_hope_body_of_workdir_is_refused (workdir, conf_name),
        # test_m17_override_through_arguments_or_extra_is_refused (arguments, extra),
        # test_rerun_and_clone_with_m17_body_are_refused (rerun, clone),
        # test_codelab_create_on_m17_is_refused_and_other_queues_pass (codelab)
        'test_every_submission_shape_with_m17_is_refused_identically',
        # test_h800_elastic_submission_passes_unchanged, and the passing half of the codelab case
        'test_allowed_queues_reach_the_server_unchanged_on_both',
        # test_refusal_is_audited_with_queue_and_caller
        'test_refusal_is_audited_with_queue_and_caller_on_both',
        # test_missing_or_invalid_policy_fails_closed_on_m17_like_the_shims (3 policies)
        'test_missing_or_invalid_policy_fails_closed_on_m17_on_both',
        # test_policy_file_is_the_single_source
        'test_policy_file_is_the_single_source_on_both',
        # test_submission_with_no_resolvable_queue_is_unresolved
        'test_submission_with_no_resolvable_queue_is_unresolved_on_both',
        # test_server_launched_as_hope_mcp_under_another_name_is_guarded,
        # test_unrelated_server_tool_named_submit_job_is_not_inspected
        'test_hope_mcp_under_another_name_is_guarded_and_other_servers_are_not',
    ],
    'test_hope_queue_policy_bridge.py': [
        'test_meta_reaches_the_server_without_changing_arguments_on_both',
    ],
    'test_mcp_bridge_relay.py': [
        'test_a_shard_sees_exactly_the_owners_catalog_and_grants_on_both',
        'test_get_bridge_in_a_shard_relays_to_either_owner',
        'test_a_relayed_call_runs_once_and_keeps_its_result_and_failure_kind_on_both',
        'test_a_server_killed_mid_call_is_a_transport_failure_never_replayed_on_both',
        'test_a_shard_killed_mid_call_withdraws_the_owners_call_on_both',
        'test_execution_host_restart_with_shards_alive_on_both',
        'test_twenty_sessions_across_shards_reach_one_server_set_on_both',
        'test_an_npm_cache_launcher_holds_the_cache_until_its_handshake_on_both',
        'test_python_relayed_bridge_reaches_the_rust_relay_owner',
    ],
}

M17 = 'root.hldy_training_cluster.your-user.h910c_m17_public'
H800 = 'root.hldy_training_cluster.your-user.h800_elastic'
OTHER_BAN = 'root.hldy_training_cluster.your-user.h910c_training_data'
ENTRY = {'id': 'm17', 'queue': M17, 'since': '2026-11-13T11:01+08:00',
         'reason': 'queue admin asked for release; owner decision'}


def _policy(*entries):
    return json.dumps({'version': 1, 'off_limits': list(entries)})


class Pair:
    """The same server, connected once through each client, with separate replay logs."""

    def __init__(self, scene, monkeypatch, host, *, server='hope', launcher='hope-mcp', timeout=None):
        from lib.mcp.rust_host import RustMCPBridge
        self.scene = scene
        self.server = server
        extra = {} if timeout is None else {'timeout': timeout}
        self.py = python_bridge(monkeypatch)
        self.py.connect_server(server, {'command': scene.launcher(f'py-{server}', launcher), **extra})
        self.host = host
        self.rs = RustMCPBridge(host, 1)
        self.rs.connect_server(server, {'command': scene.launcher(f'rs-{server}', launcher), **extra})

    def calls(self, which, tool=None):
        return [_application_params(params) for params in self.scene.calls(f'{which}-{self.server}')
                if tool is None or params['name'] == tool]

    def both(self, tool, arguments, **options):
        name = f'mcp__{self.server}__{tool}'
        return (_outcome(lambda: self.py.call_tool(name, dict(arguments), **options)),
                _outcome(lambda: self.rs.call_tool(name, dict(arguments), **options)))

    def close(self):
        self.py.disconnect_all()
        self.rs.disconnect_all()


def _application_meta(meta):
    """Request ``_meta`` minus the protocol keys rmcp adds on every 2026-07-28 request."""
    return {key: value for key, value in (meta or {}).items()
            if not key.startswith('io.modelcontextprotocol/') and key != 'progressToken'}


def _application_params(params):
    params = dict(params)
    meta = _application_meta(params.pop('_meta', None))
    if meta:
        params['_meta'] = meta
    return params


@pytest.fixture
def pair(scene, monkeypatch, rust_host):
    pair = Pair(scene, monkeypatch, rust_host)
    yield pair
    pair.close()


def _same(pair, tool, arguments, **options):
    py, rs = pair.both(tool, arguments, **options)
    assert rs == py, f'{tool}: rust {rs!r} != python {py!r}'
    return py


# ── owner Hope queue guard (test_hope_mcp_queue_policy_boundary.py on both clients) ──

def _workdir(tmp_path, queue, name='job'):
    workdir = tmp_path / name
    workdir.mkdir(exist_ok=True)
    (workdir / 'train.hope').write_text(f'[resource]\nqueue = {queue}\n', encoding='utf-8')
    return str(workdir)


def _refusal(outcome):
    kind, text, fields = outcome
    assert kind == 'result' and fields['is_error'] is True
    document = json.loads(text)
    assert document == fields['structured_json']
    assert document['ok'] is False and document['submitted'] is False
    return document


def test_m17_submission_is_refused_before_the_call_on_both(pair):
    pair.scene.write_policy(1, _policy(ENTRY))
    py, rs = pair.both('submit_job', {'workdir': str(pair.scene.tmp), 'usergroup': 'g', 'queue': M17})
    assert rs == py
    out = _refusal(rs)
    assert out['error_code'] == 'queue_off_limits' and out['stage'] == 'queue_policy'
    assert out['policy_entry']['id'] == 'm17' and out['policy_source'] == 'file'
    assert out['queue'] == M17 and 'm17' in out['error'] and out['policy_path'] in out['error']
    assert pair.calls('py') == [] and pair.calls('rs') == []


@pytest.mark.parametrize('variant', ['workdir', 'conf_name', 'arguments', 'extra', 'rerun', 'clone', 'codelab'])
def test_every_submission_shape_with_m17_is_refused_identically(pair, variant):
    tmp = pair.scene.tmp
    pair.scene.write_policy(1, _policy(ENTRY))
    banned, allowed = _workdir(tmp, M17, 'banned'), _workdir(tmp, H800, 'allowed')
    tool, arguments = {
        'workdir': ('submit_job', {'workdir': banned, 'usergroup': 'g'}),
        'conf_name': ('submit_job', {'workdir': banned, 'usergroup': 'g', 'conf_name': 'train.hope'}),
        'arguments': ('submit_job', {'workdir': allowed, 'usergroup': 'g', 'arguments': f'-Dafo.app.queue={M17} --foo'}),
        'extra': ('submit_job', {'workdir': allowed, 'usergroup': 'g', 'extra': {'queue': M17}}),
        'rerun': ('confirm_rerun', {'confirmation_token': 't', 'run_id': 'r', 'spec_job_id': 's',
                                    'job_name': 'j', 'hope_conf': f'queue = {M17}\n'}),
        'clone': ('catalog_write', {'operation': 'job_clone', 'path_params': {'jobId': '1'},
                                    'body': {'hopeConf': f'queue = {M17}\n'}, 'dry_run': False}),
        'codelab': ('catalog_write', {'operation': 'codelab_create', 'body': {'queue': M17}, 'dry_run': False}),
    }[variant]
    out = _refusal(_same(pair, tool, arguments))
    assert out['error_code'] == 'queue_off_limits'
    assert pair.calls('py') == [] and pair.calls('rs') == []


def test_allowed_queues_reach_the_server_unchanged_on_both(pair):
    pair.scene.write_policy(1, _policy(ENTRY))
    allowed = _workdir(pair.scene.tmp, H800)
    for tool, arguments in [
        ('submit_job', {'workdir': allowed, 'usergroup': 'g', 'queue': H800}),
        ('confirm_rerun', {'hope_conf': f'queue = {H800}\n'}),
        ('get_job', {'hope_run_id': 'x'}),
        ('catalog_write', {'operation': 'codelab_create', 'body': {'queue': H800}}),
        ('catalog_write', {'operation': 'codelab_create', 'body': {'name': 'box'}}),
    ]:
        assert _same(pair, tool, arguments)[0] == 'result'
    assert len(pair.calls('py')) == len(pair.calls('rs')) == 5
    assert pair.calls('py') == pair.calls('rs')


def test_refusal_is_audited_with_queue_and_caller_on_both(pair, monkeypatch):
    import lib.mcp.client._bridge as bridge_module
    records = []
    monkeypatch.setattr(bridge_module, 'audit_log', lambda event, **fields: records.append((event, fields)))
    pair.scene.write_policy(1, _policy(ENTRY))
    _same(pair, 'submit_job', {'workdir': str(pair.scene.tmp), 'usergroup': 'g', 'queue': M17},
          meta={'tofu/project': '/projects/knowledge'})
    python = [fields for event, fields in records if event == 'mcp_queue_policy_refusal']
    rust = [fields for event, fields in pair.host.audit if event == 'mcp_queue_policy_refusal']
    assert python == rust and len(rust) == 1
    assert rust[0]['queue'] == M17 and rust[0]['owner_user_id'] == 1
    assert rust[0]['project'] == '/projects/knowledge' and rust[0]['tool'] == 'submit_job'


@pytest.mark.parametrize('policy_text', [None, '{broken', '{"version":1,"off_limits":[{"id":"x"}]}'])
def test_missing_or_invalid_policy_fails_closed_on_m17_on_both(pair, policy_text):
    pair.scene.write_policy(1, policy_text)
    py, rs = pair.both('submit_job', {'workdir': str(pair.scene.tmp), 'usergroup': 'g', 'queue': M17})
    out_py, out_rs = _refusal(py), _refusal(rs)
    for out in (out_py, out_rs):
        assert out['error_code'] == 'queue_off_limits'
        assert out['policy_source'] == 'built-in default' and out['policy_entry']['id'] == 'm17'
    if policy_text == '{broken':
        # CPython's and serde's JSON syntax messages differ; everything else is identical.
        for out in (out_py, out_rs):
            out['error'] = out['error'].split(' is unparsable: ')[0]
    assert out_py == out_rs
    assert _same(pair, 'submit_job', {'workdir': str(pair.scene.tmp), 'usergroup': 'g', 'queue': H800})[0] == 'result'
    assert len(pair.calls('py')) == len(pair.calls('rs')) == 1


def test_policy_file_is_the_single_source_on_both(pair):
    pair.scene.write_policy(1, _policy({**ENTRY, 'id': 'td', 'queue': OTHER_BAN}))
    _refusal(_same(pair, 'submit_job', {'workdir': str(pair.scene.tmp), 'usergroup': 'g', 'queue': OTHER_BAN}))
    assert _same(pair, 'submit_job', {'workdir': str(pair.scene.tmp), 'usergroup': 'g', 'queue': M17})[0] == 'result'


def test_submission_with_no_resolvable_queue_is_unresolved_on_both(pair):
    pair.scene.write_policy(1, _policy(ENTRY))
    empty = pair.scene.tmp / 'empty'
    empty.mkdir()
    out = _refusal(_same(pair, 'submit_job', {'workdir': str(empty), 'usergroup': 'g'}))
    assert out['error_code'] == 'queue_policy_unresolved'
    assert pair.calls('py') == [] and pair.calls('rs') == []


def test_hope_mcp_under_another_name_is_guarded_and_other_servers_are_not(scene, monkeypatch, rust_host):
    scene.write_policy(1, _policy(ENTRY))
    jobs = Pair(scene, monkeypatch, rust_host, server='jobs')
    try:
        _refusal(_same(jobs, 'submit_job', {'workdir': str(scene.tmp), 'usergroup': 'g', 'queue': M17}))
    finally:
        jobs.close()
    other = Pair(scene, monkeypatch, rust_host, server='other', launcher='other-mcp')
    try:
        assert _same(other, 'submit_job', {'queue': M17})[0] == 'result'
        assert len(other.calls('py')) == len(other.calls('rs')) == 1
    finally:
        other.close()


# ── results, failures, deadlines, replay, cancellation ──────────────────────

@pytest.mark.parametrize('tool', ['get_job', 'structured', 'duplicate_json', 'media', 'tool_error',
                                  'large', 'catalog_write', 'sheet.get_range_data'])
def test_recorded_results_project_identically(pair, tool):
    arguments = {'operation': 'noop'} if tool == 'catalog_write' else {}
    name = 'sheet_get_range_data' if tool == 'sheet.get_range_data' else tool
    py = _same(pair, name, arguments)
    assert py[0] == 'result'


def test_peer_errors_keep_their_category(pair):
    py, rs = pair.both('peer_error', {})
    assert py[:2] == rs[:2] == ('raised', 'error')


def test_coercion_and_trusted_meta_reach_the_server_identically(pair):
    py, rs = pair.both('echo', {'n': ' 5 ', 'f': '2', 'flag': 'yes', 'tags': 'x', 'free': '1'},
                       meta={'tofu/project': '/canonical'})
    received = [json.loads(outcome[2]['structured_json']) for outcome in (py, rs)]
    for document in received:
        assert document['arguments'] == {'n': 5, 'f': 2.0, 'flag': True, 'tags': ['x'], 'free': '1'}
        document['meta'] = _application_meta(document['meta'])
    assert received[0] == received[1]
    assert received[1]['meta'] == {'tofu/project': '/canonical'}


def test_deadline_timeouts_then_degraded_fast_fail(pair):
    for _ in range(3):
        py, rs = pair.both('hang', {}, timeout_override=1)
        assert py[:2] == rs[:2] == ('raised', 'timeout')
    py, rs = pair.both('hang', {}, timeout_override=1)
    assert rs == py and py[0] == 'result'
    assert py[1].startswith("MCP Error: server 'hope' is degraded — 3 consecutive call timeouts "
                            "(configured call budget: 1s)")


def test_dead_transport_replays_only_reads(pair):
    py, rs = pair.both('crash', {})
    assert rs == py and py[1] == 'after restart'
    py, rs = pair.both('crash_write', {})
    assert py[:2] == rs[:2] == ('raised', 'transport')
    assert len(pair.calls('py', 'crash_write')) == len(pair.calls('rs', 'crash_write')) == 1
    # The dead transport's catalog was refreshed; the server answers again.
    assert _same(pair, 'get_job', {})[0] == 'result'


def test_cancellation_stops_a_waiting_call_on_both(pair):
    for bridge in (pair.py, pair.rs):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(CancelledError):
            bridge.call_tool('mcp__hope__hang', {}, cancel=cancel)
        assert time.monotonic() - started < 30


def test_disabled_tools_are_refused_without_a_call(pair):
    for bridge in (pair.py, pair.rs):
        bridge.set_disabled_tools('hope', ['get_job'])
    py, rs = pair.both('get_job', {})
    assert py == rs == ('raised', 'invalid', 'ValueError')
    assert pair.calls('py') == pair.calls('rs') == []


def test_catalog_rows_and_definitions_are_identical(pair):
    python_rows = pair.py.tool_index_rows()
    rust_rows = pair.rs.tool_index_rows()
    assert [row[0] for row in rust_rows] == [row[0] for row in python_rows]
    for (name, py_info, py_disabled), (_, rs_info, rs_disabled) in zip(python_rows, rust_rows):
        assert dict(rs_info) == json.loads(json.dumps(dict(py_info))), name
        assert rs_disabled == py_disabled
    assert pair.rs.get_openai_tool_defs() == pair.py.get_openai_tool_defs()
    [py_server] = pair.py.list_servers()
    [rs_server] = pair.rs.list_servers()
    for key in ('name', 'tools_count', 'tool_names', 'server_version', 'server_impl_name', 'catalog_version'):
        assert rs_server[key] == py_server[key], key


# ── the execution-host relay: a Python shard against the Rust owner ─────────

def test_python_relayed_bridge_reaches_the_rust_relay_owner(pair):
    from lib.mcp.bridge_relay import RelayClient, RelayedMCPBridge, record_path
    project = pair.scene.tmp / 'project'
    pair.host.call('relay_start', {'project': str(project)}, timeout=30)
    try:
        shard = RelayedMCPBridge(RelayClient(record_path(project)), 1)
        assert shard.connected and shard.server_count == 1
        assert shard.get_tool_info('mcp__hope__get_job') == pair.rs.get_tool_info('mcp__hope__get_job')
        direct = _outcome(lambda: pair.py.call_tool('mcp__hope__structured', {}))
        assert _outcome(lambda: shard.call_tool('mcp__hope__structured', {})) == direct
        assert _outcome(lambda: shard.call_tool('mcp__hope__peer_error', {}))[:2] == ('raised', 'error')
        pair.scene.write_policy(1, _policy(ENTRY))
        refused = _outcome(lambda: shard.call_tool('mcp__hope__submit_job', {'workdir': '/x', 'queue': M17}))
        assert _refusal(refused)['error_code'] == 'queue_off_limits'
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        with pytest.raises(CancelledError):
            shard.call_tool('mcp__hope__hang', {}, cancel=cancel)
    finally:
        pair.host.call('relay_stop', {}, timeout=30)
    assert not record_path(project).exists()


# ── pure owners: projection, environment, redaction, coercion, policy ───────

def _python_projection(result):
    from lib.mcp.client import MCPBridge
    import lib.mcp.client as mc

    class Session:
        async def call_tool(self, name, arguments, **kwargs):
            return result

    handle = mc._MCPServerHandle('s', {})
    handle.sdk_generation = 2
    handle.session = Session()
    return asyncio.run(MCPBridge()._async_call_tool(handle, 't', {}, None))


def _nested(depth):
    value = 1
    for _ in range(depth):
        value = [value]
    return value


_BIG = 'x' * 200_100
PROJECTIONS = [
    {'content': [{'type': 'text', 'text': 'plain'}]},
    {'content': [], 'structuredContent': {'b': [1, 2.0, 1e16, 1e-05], 'a': '三'}},
    {'content': [{'type': 'text', 'text': '{"a": 1}'}], 'structuredContent': {'a': 1}, 'isError': True},
    {'content': [{'type': 'text', 'text': 'x'}], 'structuredContent': {}},
    {'content': [{'type': 'text', 'text': _BIG}]},
    {'content': [{'type': 'text', 'text': 'y' * 300_000}]},
    {'content': [{'type': 'text', 'text': 'z'}], 'structuredContent': {'deep': _nested(33)}},
    {'content': [{'type': 'image', 'data': 'AAAA', 'mimeType': 'image/JPG', 'caption': 'c'}] * 22},
    {'content': [{'type': 'resource', 'resource': {'uri': 'u://1', 'text': 't'}}, {'type': 'mystery'}]},
    {'content': [{'type': 'text', 'text': str(i)} for i in range(1030)]},
]


@pytest.mark.parametrize('index', range(len(PROJECTIONS)))
def test_result_projection_matches_python(rust_host, index):
    raw = PROJECTIONS[index]
    sdk_like = SimpleNamespace(content=raw['content'], structuredContent=raw.get('structuredContent'),
                               isError=raw.get('isError', False))
    python = _python_projection(sdk_like)
    rust = rust_host.call('project_result', {'result': raw}, timeout=60)
    assert rust['text'] == str(python)
    fields = rust['tool_result']
    assert fields['structured_json'] == python.structured_json
    assert fields['document_text'] == python.document_text
    assert fields['document_error'] == python.document_error
    assert fields['retention_error'] == python.retention_error
    assert fields['is_error'] == python.is_error
    assert fields['content_blocks'] == json.loads(json.dumps(list(python.content_blocks)))
    assert fields['image_contents'] == json.loads(json.dumps(list(python.image_contents)))


def test_stdio_environment_matches_python(rust_host, scene, monkeypatch):
    from lib.mcp.stdio_environment import build_stdio_environment
    import lib.proxy
    monkeypatch.setattr(lib.proxy, 'first_reachable_global_proxy_url', lambda: None, raising=False)
    monkeypatch.setenv('GITHUB_TOKEN', 'ambient-secret')
    monkeypatch.setenv('TOFU_STORAGE_TOKEN', 'live-storage')
    monkeypatch.setenv('TOFU_HTTPS_PROXY', 'http://proxy.invalid:3128')
    monkeypatch.delenv('HTTPS_PROXY', raising=False)
    monkeypatch.delenv('https_proxy', raising=False)
    cases = [
        ({'command': 'x', 'env': {'A': '1', 'PATH': '/only/here'}}, 1),
        ({'command': 'x', 'env': {'TOFU_DATA_DIR': '/explicit'}}, None),
        ({'command': 'x', 'env': {'XDG_DATA_HOME': '/mine', 'UV_PYTHON': '3.11'}}, 7),
        ({'command': 'x'}, 3),
    ]
    for config, owner in cases:
        python = build_stdio_environment(json.loads(json.dumps(config)), owner_user_id=owner)
        rust = rust_host.call('stdio_environment', {
            **scene.host_settings(), 'config': config, 'owner_user_id': owner,
            'process_env': dict(os.environ)}, timeout=30)
        assert rust == python, config
        assert 'GITHUB_TOKEN' not in rust and 'TOFU_STORAGE_TOKEN' not in rust
    from lib.mcp.rust_host import RustHostError
    for config, owner in [({'env': {'TOFU_MCP_OWNER_ID': 'tofu:9'}}, None), ({'env': {'K': 'vault:missing_entry'}}, 1),
                          ({'env': 'oops'}, None)]:
        with pytest.raises(ValueError) as python_error:
            build_stdio_environment(config, owner_user_id=owner)
        with pytest.raises(RustHostError) as rust_error:
            rust_host.call('stdio_environment', {**scene.host_settings(), 'config': config, 'owner_user_id': owner,
                                                 'process_env': dict(os.environ)}, timeout=30)
        if 'vault' not in json.dumps(config):
            assert str(rust_error.value) == str(python_error.value)
        else:
            assert 'missing_entry' in str(rust_error.value) and 'not configured' in str(rust_error.value)


def test_redaction_matches_python(rust_host):
    from lib.mcp.transport import header_env_keys, redact_config, scrub_text
    configs = [
        {'command': 'npx', 'args': ['-y', 'x'], 'env': {'KEY': 's3cret'}, 'timeout': 30, 'surprise': {'a': 1}},
        {'transport': 'http', 'url': 'https://mcp.amap.com/mcp?key=${AMAP}&v=2&flag', 'headers': {
            'Authorization': 'Bearer ${TOKEN}', 'X-Static': 'literal-secret-value', 'X-Short': 'a b'}},
        {'url': 'https://h/p?', 'headers': {}},
    ]
    for config in configs:
        result = rust_host.call('redact_config', {'config': config}, timeout=30)
        assert result['config'] == redact_config(config)
        assert rust_host.call('header_env_keys', {'config': config}, timeout=30) == header_env_keys(config)
    for text in ["httpx error for url 'https://mcp.amap.com/mcp?key=REAL&x=${Y}' (401)",
                 'see http://a/b?c=d and https://e/f', 'no url here', '']:
        assert rust_host.call('scrub_text', {'text': text}, timeout=30) == scrub_text(text)


def test_coercion_matches_python(rust_host):
    from lib.mcp.client._coerce import _coerce_args_to_schema
    schema = {'properties': {
        'i': {'type': 'integer'}, 'n': {'type': ['number', 'null']}, 'b': {'type': 'boolean'},
        'a': {'type': 'array', 'items': {'type': 'integer'}}, 'ao': {'type': 'array'},
        'u': {'anyOf': [{'type': 'integer'}, {'type': 'boolean'}]},
        'o': {'type': 'object', 'properties': {'k': {'type': 'integer'}}},
        's': {'type': 'string'}}}
    for arguments in [
        {'i': '12', 'n': '1.5', 'b': 'no', 'a': ['1', 'x'], 'ao': 'one', 'u': 'true', 'o': {'k': '3', 'z': '4'}, 's': 5},
        {'i': '-3', 'n': 'abc', 'b': 'maybe', 'a': '7', 'u': '9', 'o': 'flat'},
        {'i': '1.0', 'n': '  2 ', 'extra': [1]},
    ]:
        assert rust_host.call('coerce', {'arguments': arguments, 'schema': schema}, timeout=30) == \
            _coerce_args_to_schema(arguments, schema)


INVALID_POLICIES = [
    '[]', '{"version": 2, "off_limits": []}', '{"version": true, "off_limits": []}',
    '{"version": 1, "off_limits": {}}', '{"version": 1, "off_limits": [{"id": "a", "queue": "q", "since": "s"}]}',
    '{"version": 1, "off_limits": [{"id": "-bad", "queue": "q", "since": "s", "reason": "r"}]}',
    '{"version": 1, "off_limits": [{"id": "a", "queue": "q", "since": "s", "reason": "r"},'
    ' {"id": "a", "queue": "q", "since": "s", "reason": "r"}]}',
    '{"version": 1, "off_limits": [{"id": "a", "queue": "q", "since": " ", "reason": "r"}]}',
    '{"version": 1, "off_limits": [{"id": "a", "queue": "q", "since": "s", "reason": "r", "projects": ["rel"]}]}',
    '{"version": 1, "off_limits": [{"id": "a", "queue": "q", "since": "s", "reason": "r", "x": 1}]}',
    '{"version": 1, "version": 1, "off_limits": []}',
    '{"version": 1, "off_limits": [{"id": "a", "queue": "q", "since": "s", "reason": "r", "projects": ["/p"]}]}',
]


@pytest.mark.parametrize('text', INVALID_POLICIES)
def test_policy_validation_matches_the_python_loader(rust_host, tmp_path, text):
    from lib.tools.hope_queue_policy import load_policy
    path = tmp_path / 'HOPE_QUEUE_POLICY.json'
    path.write_text(text, encoding='utf-8')
    python = load_policy(str(path))
    rust = rust_host.call('load_policy', {'path': str(path)}, timeout=30)
    assert rust['default_reason'] == python.default_reason
    assert rust['entries'] == [dict(entry) for entry in python.entries]
    assert rust['described'] == python.described


def test_policy_file_kinds_match_the_python_loader(rust_host, tmp_path):
    from lib.tools.hope_queue_policy import load_policy
    link = tmp_path / 'link.json'
    link.symlink_to(tmp_path / 'target.json')
    (tmp_path / 'target.json').write_text('{"version": 1, "off_limits": []}', encoding='utf-8')
    big = tmp_path / 'big.json'
    big.write_text(' ' * 70000, encoding='utf-8')
    for path in ['', str(tmp_path / 'absent.json'), str(link), str(tmp_path), str(big), '/dev/null']:
        rust = rust_host.call('load_policy', {'path': path}, timeout=30)
        assert rust['default_reason'] == load_policy(path).default_reason, path


def test_cli_submission_parsing_matches_python(rust_host, tmp_path):
    from lib.tools.hope_queue_policy import submission_request
    (tmp_path / 'a.hope').write_text('queue = root.a\n', encoding='utf-8')
    (tmp_path / 'b.hope').write_text('afo.app.queue: "root.b"\n', encoding='utf-8')
    xml = tmp_path / 'conf.xml'
    xml.write_text('<configuration><property><name>queue</name><value>root.x</value></property>'
                   '</configuration>', encoding='utf-8')
    for argv in [
        ['hope', 'run'], ['hope', 'run', 'a.hope', '-Dqueue=root.d'], ['hope', 'quickrun', '--queue', 'root.q'],
        ['hope', 'workbench', '--init', '--xml', str(xml)], ['hope', 'list'], ['mlp', 'hope', 'run', '--queue=root.z'],
        ['mlp', 'job', 'rerun', '--hope-conf', 'queue = root.r'], ['mlp', 'job', 'clone', '--hope-conf', 'a.hope'],
        ['mlp', 'job', 'clone'], ['other'],
    ]:
        python = submission_request(argv, str(tmp_path))
        rust = rust_host.call('cli_submission_request', {'argv': argv, 'cwd': str(tmp_path)}, timeout=30)
        assert (None if rust is None else (rust[0], rust[1])) == python, argv


def test_namespaced_names_and_schema_hashes_match_python(rust_host):
    from lib.mcp.types import namespaced_tool_names
    raw = ['a.b', 'a_b', 'hope_login', 'x' * 90, 'ok']
    assert rust_host.call('namespaced_names', {'server': 'hope', 'tools': raw}, timeout=30) == \
        namespaced_tool_names('hope', raw)
    from lib.mcp.client._bridge import _schema_hash
    tool = {'name': 't', 'description': 'd', 'inputSchema': {'type': 'object'},
            'annotations': {'readOnlyHint': True, 'title': 'T'}}
    sdk_tool = SimpleNamespace(name='t', description='d', input_schema={'type': 'object'}, meta=None,
                               annotations=SimpleNamespace(model_dump=lambda by_alias: {
                                   'title': 'T', 'readOnlyHint': True, 'destructiveHint': None,
                                   'idempotentHint': None, 'openWorldHint': None}))
    assert rust_host.call('schema_hash', {'tool': tool}, timeout=30) == _schema_hash(sdk_tool)


# ── trusted request meta (test_hope_queue_policy_bridge.py on both clients) ──

def test_meta_reaches_the_server_without_changing_arguments_on_both(pair):
    py, rs = pair.both('echo', {'workdir': '/tmp'}, meta={'tofu/project': '/canonical'})
    for result in (py, rs):
        received = json.loads(result[2]['structured_json'])
        assert received['arguments'] == {'workdir': '/tmp'}
        assert _application_meta(received['meta']) == {'tofu/project': '/canonical'}
    assert pair.calls('py', 'echo') == pair.calls('rs', 'echo')
    assert pair.calls('rs', 'echo')[0]['name'] == 'echo'


# ── the execution-plane relay (test_mcp_bridge_relay.py, a Python and a Rust owner) ──

CHAOS = ROOT / 'tests' / 'chaos' / 'mcp_server.py'
# A test bound for one wait on a real process, not a product timer.
DEADLINE_S = 60.0


def _chaos_row(state, command=sys.executable):
    state.mkdir(parents=True, exist_ok=True)
    (state / 'schema').write_text('v1')
    return {'transport': 'stdio', 'command': command, 'args': [str(CHAOS), str(state)], 'enabled': True}


def _chaos_log(state, name='calls.log'):
    path = state / name
    return path.read_text().splitlines() if path.exists() else []


def _wait(predicate, what):
    deadline = time.monotonic() + DEADLINE_S
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def _relay_environment():
    env = {k: v for k, v in os.environ.items() if k not in ('TOFU_PROCESS_ROLE', 'npm_config_cache')}
    env['PYTHONPATH'] = str(ROOT)
    return env


class Owner:
    """One execution host: its bridge, its chaos servers ``hope``/``github`` and its published relay."""

    def __init__(self, kind, tmp_path, monkeypatch, host):
        from lib.mcp import bridge_relay
        self.kind = kind
        self.project = tmp_path / f'{kind}-project'
        self.states = {name: tmp_path / kind / name for name in ('hope', 'github')}
        if kind == 'python':
            import lib.mcp.client as client
            from lib.mcp.client._bridge import MCPBridge
            self.bridge = MCPBridge(owner_user_id=1)
            monkeypatch.setitem(client._bridges, 1, self.bridge)
        else:
            from lib.mcp.rust_host import RustMCPBridge
            self.host = host
            self.bridge = RustMCPBridge(host, 1)
        for name, state in self.states.items():
            self.bridge.connect_server(name, _chaos_row(state))
        if kind == 'python':
            self.server = bridge_relay.RelayServer(bridge_relay.record_path(self.project))
            self.server.start()
        else:
            host.call('relay_start', {'project': str(self.project)}, timeout=30)

    def shard(self):
        from lib.mcp import bridge_relay
        return bridge_relay.RelayedMCPBridge(bridge_relay.RelayClient(bridge_relay.record_path(self.project)), 1)

    def active_calls(self):
        if self.kind == 'python':
            return len(self.server._calls)
        return self.host.call('relay_active_calls', {}, timeout=30)

    def close(self):
        if self.kind == 'python':
            self.server.close(timeout=DEADLINE_S)
        else:
            self.host.call('relay_stop', {}, timeout=30)
        self.bridge.disconnect_all()


@pytest.fixture
def owners(tmp_path, scene, monkeypatch, rust_host):
    made = [Owner('python', tmp_path, monkeypatch, None), Owner('rust', tmp_path, monkeypatch, rust_host)]
    yield made
    for owner in made:
        owner.close()


def _catalog_view(shard):
    return {
        'defs': shard.get_openai_tool_defs(), 'safety': shard.get_tool_safety(),
        'snapshot': shard.get_tool_catalog_snapshot(), 'projection': shard.get_tool_catalog_projection()[0],
        'info': shard.get_tool_info('mcp__hope__chaos_record'), 'rows': shard.tool_index_rows(),
        'servers': sorted(row['name'] for row in shard.list_servers()),
    }


def test_a_shard_sees_exactly_the_owners_catalog_and_grants_on_both(owners):
    python, rust = owners
    py_shard, rs_shard = python.shard(), rust.shard()
    for shard in (py_shard, rs_shard):
        assert shard.connected and shard.server_count == 2
    assert _catalog_view(rs_shard) == _catalog_view(py_shard)
    assert rs_shard.get_openai_tool_defs() == python.bridge.get_openai_tool_defs()
    assert rs_shard.get_tool_catalog_snapshot() == python.bridge.get_tool_catalog_snapshot()
    assert _catalog_view(rs_shard)['servers'] == ['github', 'hope']
    generations = [shard.tool_catalog_generation() for shard in (py_shard, rs_shard)]
    assert [shard.tool_catalog_generation() for shard in (py_shard, rs_shard)] == generations

    for owner in owners:
        owner.bridge.set_disabled_tools('hope', ['chaos_record'])
    for shard, generation in zip((py_shard, rs_shard), generations):
        assert shard.tool_catalog_generation() != generation
        assert 'mcp__hope__chaos_record' not in shard.get_tool_safety()
        assert ('mcp__hope__chaos_record', True) in [(ns, disabled) for ns, _i, disabled in shard.tool_index_rows()]
        with pytest.raises(ValueError, match='disabled by user'):
            shard.call_tool('mcp__hope__chaos_record', {'note': 'forbidden'})
    assert _catalog_view(rs_shard) == _catalog_view(py_shard)
    assert rs_shard.get_openai_tool_defs() == python.bridge.get_openai_tool_defs()


def test_get_bridge_in_a_shard_relays_to_either_owner(owners):
    from lib.mcp import bridge_relay, get_bridge
    for owner in owners:
        try:
            assert bridge_relay.configure_mcp_owner(
                process_role='execution_shard', environ={'TOFU_PROJECT_PATH': str(owner.project)}) == 'relay'
            relayed = get_bridge(1)
            assert isinstance(relayed, bridge_relay.RelayedMCPBridge)
            assert relayed.is_mcp_tool('mcp__github__chaos_record')
            with pytest.raises(RuntimeError, match='owned by the execution host'):
                relayed.connect_server('other', {})
        finally:
            bridge_relay.stop_mcp_owner()
        assert bridge_relay.relayed_bridge(1) is None


def test_a_relayed_call_runs_once_and_keeps_its_result_and_failure_kind_on_both(owners):
    outcomes = []
    for owner in owners:
        shard = owner.shard()
        recorded = _outcome(lambda: shard.call_tool('mcp__hope__chaos_record', {'note': 'one'}))
        assert recorded[1] == 'recorded one'
        assert _chaos_log(owner.states['hope']).count('record one') == 1
        missing = _outcome(lambda: shard.call_tool('mcp__nowhere__tool', {}))
        assert missing[:2] == ('raised', 'invalid')
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(CancelledError):
            shard.call_tool('mcp__hope__chaos_record', {'note': 'never'}, cancel=cancelled)
        assert 'record never' not in _chaos_log(owner.states['hope'])
        outcomes.append((recorded, missing))
    assert outcomes[1] == outcomes[0]


def test_a_server_killed_mid_call_is_a_transport_failure_never_replayed_on_both(owners):
    from lib.mcp.client._errors import _is_transport_dead_error
    for owner in owners:
        shard = owner.shard()
        with pytest.raises(Exception) as failure:
            shard.call_tool('mcp__github__chaos_crash', {})
        assert _is_transport_dead_error(failure.value), (owner.kind, repr(failure.value))
        assert _chaos_log(owner.states['github']).count('crash') == 1, f'{owner.kind}: a crashing call was replayed'
        assert shard.call_tool('mcp__github__chaos_record', {'note': 'after'}) == 'recorded after'
        assert len(_chaos_log(owner.states['github'], 'launches.log')) == 2, owner.kind


_SHARD_CALL = """
import json, sys
from lib.mcp import bridge_relay
from lib.mcp.client import get_bridge
bridge_relay.configure_mcp_owner(process_role='execution_shard', environ={'TOFU_PROJECT_PATH': sys.argv[1]})
print(json.dumps(str(get_bridge(1).call_tool(sys.argv[2], json.loads(sys.argv[3])))), flush=True)
"""


def test_a_shard_killed_mid_call_withdraws_the_owners_call_on_both(owners):
    for owner in owners:
        hold = owner.states['hope'] / 'hold-call'
        hold.touch()
        shard = subprocess.Popen(
            [sys.executable, '-c', _SHARD_CALL, str(owner.project), 'mcp__hope__chaos_wait', '{"note": "k"}'],
            env=_relay_environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            _wait(lambda: 'wait k' in _chaos_log(owner.states['hope']), f'{owner.kind}: the shard call never arrived')
            assert owner.active_calls() == 1
            shard.kill()
            shard.wait()
            _wait(lambda: owner.active_calls() == 0, f"{owner.kind}: the owner kept the dead shard's call")
        finally:
            hold.unlink(missing_ok=True)
            if shard.poll() is None:
                shard.kill()
                shard.wait()
        assert _chaos_log(owner.states['hope']).count('wait k') == 1, f'{owner.kind}: the call was replayed'


_PYTHON_OWNER = """
import json, sys
import lib.mcp.client as client
from lib.mcp.client._bridge import MCPBridge
from lib.mcp import bridge_relay
bridge = MCPBridge(owner_user_id=1)
client._bridges[1] = bridge
for name, row in json.loads(sys.argv[2]).items():
    bridge.connect_server(name, row)
bridge_relay.configure_mcp_owner(process_role='execution', environ={'TOFU_PROJECT_PATH': sys.argv[1]})
print('ready', flush=True)
sys.stdin.read()
"""


class _OwnerProcess:
    """An execution host process (Python's owner, or a ``tofu-mcp-host``) that can be SIGKILLed."""

    def __init__(self, kind, project, rows, binary, settings):
        self.kind = kind
        if kind == 'python':
            self.process = subprocess.Popen(
                [sys.executable, '-c', _PYTHON_OWNER, str(project), json.dumps(rows)], env=_relay_environment(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            assert self.process.stdout.readline().strip() == b'ready'
            return
        from lib.mcp.rust_host import RustMCPBridge, RustMCPHost
        self.host = RustMCPHost(binary, env=dict(os.environ))
        self.host.call('configure', settings, timeout=30)
        bridge = RustMCPBridge(self.host, 1)
        for name, row in rows.items():
            bridge.connect_server(name, row)
        self.host.call('relay_start', {'project': str(project)}, timeout=30)
        self.process = self.host._proc

    def kill(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()
        if self.kind == 'rust':
            self.host.close()


@pytest.mark.parametrize('kind', ['python', 'rust'])
def test_execution_host_restart_with_shards_alive_on_both(tmp_path, scene, mcp_host_binary, kind):
    from lib.mcp import bridge_relay
    state = tmp_path / 'hope'
    rows = {'hope': _chaos_row(state)}
    project = tmp_path / 'project'
    start = lambda: _OwnerProcess(kind, project, rows, mcp_host_binary, scene.host_settings())  # noqa: E731
    owner = start()
    shard = bridge_relay.RelayedMCPBridge(bridge_relay.RelayClient(bridge_relay.record_path(project)), 1)
    try:
        assert shard.call_tool('mcp__hope__chaos_record', {'note': 'before'}) == 'recorded before'
        before = shard.tool_catalog_generation()
        (state / 'hold-call').touch()
        result = {}

        def in_flight():
            try:
                result['result'] = shard.call_tool('mcp__hope__chaos_wait', {'note': 'lost'})
            except Exception as exc:  # noqa: BLE001 - the failure is the assertion
                result['error'] = exc

        caller = threading.Thread(target=in_flight)
        caller.start()
        _wait(lambda: 'wait lost' in _chaos_log(state), 'the in-flight call never reached the server')
        owner.kill()
        caller.join(DEADLINE_S)
        assert isinstance(result.get('error'), ConnectionError), result
        assert 'outcome is unknown' in str(result['error'])
        assert not shard.connected and shard.get_openai_tool_defs() == []
        (state / 'hold-call').unlink()

        owner = start()
        assert shard.connected
        assert shard.tool_catalog_generation() != before
        assert shard.call_tool('mcp__hope__chaos_record', {'note': 'after'}) == 'recorded after'
    finally:
        (state / 'hold-call').unlink(missing_ok=True)
        owner.kill()
    calls = _chaos_log(state)
    assert calls.count('wait lost') == 1, 'the lost call was replayed'
    assert calls.count('record before') == 1 and calls.count('record after') == 1


_SHARD_SESSIONS = """
import json, sys, threading
from lib.mcp import bridge_relay
from lib.mcp.client import get_bridge
bridge_relay.configure_mcp_owner(process_role='execution_shard', environ={'TOFU_PROJECT_PATH': sys.argv[1]})
shard, sessions = sys.argv[2], int(sys.argv[3])
results, servers = {}, set()

def session(index):
    bridge = get_bridge(1)
    servers.update(row['server_name'] for row in bridge.get_tool_catalog_snapshot())
    for server in ('hope', 'github'):
        note = f'{shard}-{index}-{server}'
        results[note] = str(bridge.call_tool(f'mcp__{server}__chaos_record', {'note': note}))

threads = [threading.Thread(target=session, args=(index,)) for index in range(sessions)]
for thread in threads: thread.start()
for thread in threads: thread.join()
print(json.dumps({'results': results, 'servers': sorted(servers)}), flush=True)
"""


def test_twenty_sessions_across_shards_reach_one_server_set_on_both(owners):
    for owner in owners:
        shards = [subprocess.Popen([sys.executable, '-c', _SHARD_SESSIONS, str(owner.project), f's{index}', '5'],
                                   env=_relay_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                  for index in range(4)]
        notes = {}
        for process in shards:
            out, err = process.communicate(timeout=DEADLINE_S)
            assert process.returncode == 0, err.decode()[-2000:]
            report = json.loads(out.decode().strip().splitlines()[-1])
            assert report['servers'] == ['github', 'hope'], f'{owner.kind}: a shard is missing a server'
            notes.update(report['results'])
        assert len(notes) == 40
        assert all(result == f'recorded {note}' for note, result in notes.items())
        for name, state in owner.states.items():
            expected = sorted(f'record {note}' for note in notes if note.endswith(name))
            assert sorted(line for line in _chaos_log(state) if line.startswith('record ')) == expected, \
                f'{owner.kind}: a call was lost or ran twice'
            assert len(_chaos_log(state, 'launches.log')) == 1, f'{owner.kind}: a shard launched its own server'


_TRY_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('held')
else:
    print('free')
"""


@pytest.mark.parametrize('launcher, held', [('npx', True), ('python', False)])
def test_an_npm_cache_launcher_holds_the_cache_until_its_handshake_on_both(
        tmp_path, scene, monkeypatch, mcp_host_binary, launcher, held):
    from lib.mcp.client._bridge import MCPBridge
    from lib.mcp.rust_host import RustMCPBridge, RustMCPHost
    # Both owners derive the cache from TOFU_MCP_CACHE_DIR, never an inherited npm_config_cache.
    monkeypatch.delenv('npm_config_cache', raising=False)
    rust_host = RustMCPHost(mcp_host_binary, env=dict(os.environ))
    rust_host.call('configure', scene.host_settings(), timeout=30)
    lock = scene.cache / 'npm' / '.tofu-launch.lock'
    state_of = lambda: subprocess.run([sys.executable, '-c', _TRY_LOCK, str(lock)], capture_output=True,  # noqa: E731
                                      text=True, check=True).stdout.strip()
    command = sys.executable
    if launcher == 'npx':
        command = str(tmp_path / 'bin' / 'npx')
        Path(command).parent.mkdir()
        Path(command).write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
        os.chmod(command, 0o755)
    for kind, bridge in (('python', MCPBridge(owner_user_id=1)), ('rust', RustMCPBridge(rust_host, 1))):
        state = tmp_path / f'{kind}-server'
        row = _chaos_row(state, command)
        (state / 'hold-initialize').touch()
        connect = threading.Thread(target=bridge.connect_server, args=('srv', row))
        connect.start()
        try:
            _wait(lambda: _chaos_log(state, 'launches.log'), f'{kind}: the server never launched')
            assert state_of() == ('held' if held else 'free'), kind
            (state / 'hold-initialize').unlink()
            connect.join(DEADLINE_S)
            assert bridge.is_mcp_tool('mcp__srv__chaos_record'), kind
            assert state_of() == 'free', kind
        finally:
            (state / 'hold-initialize').unlink(missing_ok=True)
            connect.join(DEADLINE_S)
            bridge.disconnect_all()
    rust_host.close()
