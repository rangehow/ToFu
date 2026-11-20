"""Tofu's MCP boundary enforces the owner Hope queue policy before any call.

hope-mcp itself has no policy reader, so before this boundary an MCP
``submit_job`` (or rerun/clone) to the banned m17 queue reached the server.
The bridge now reads ``HOPE_QUEUE_POLICY.json`` with the same loader as the
hope/mlp CLI shims and refuses a banned queue without starting the call.
"""
from __future__ import annotations

import json

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge

pytestmark = pytest.mark.unit

M17 = 'root.hldy_training_cluster.your-user.h910c_m17_public'
H800 = 'root.hldy_training_cluster.your-user.h800_elastic'
OTHER_BAN = 'root.hldy_training_cluster.your-user.h910c_training_data'
ENTRY = {'id': 'm17', 'queue': M17, 'since': '2026-11-13T11:01+08:00',
         'reason': 'queue admin asked for release; owner decision'}


def _bridge(monkeypatch, tmp_path, policy_text, *, server='hope', command='hope-mcp'):
    policy = tmp_path / 'router' / 'HOPE_QUEUE_POLICY.json'
    if policy_text is not None:
        policy.parent.mkdir(parents=True)
        policy.write_text(policy_text, encoding='utf-8')
    import lib.tools.hope_queue_policy_launcher as launcher
    monkeypatch.setattr(launcher, 'policy_path', lambda owner: str(policy))
    bridge = MCPBridge(owner_user_id=1)
    handle = mc._MCPServerHandle(server, {'command': command, 'timeout': 60})
    handle.session = object()
    bridge._servers[server] = handle
    bridge._configs[server] = {'command': command}
    for tool in ('submit_job', 'confirm_rerun', 'catalog_write', 'get_job'):
        name = f'mcp__{server}__{tool}'
        bridge._tool_index[name] = {
            'server_name': server, 'tool_name': tool, 'namespaced_name': name,
            'description': '', 'input_schema': {}, 'openai_def': {}, 'read_only_hint': False}
    sent = []

    def _run_async(coro, timeout=None, **_):
        coro.close()
        sent.append(True)
        return 'SENT'

    monkeypatch.setattr(bridge, '_run_async', _run_async)
    return bridge, sent


def _policy(*entries):
    return json.dumps({'version': 1, 'off_limits': list(entries)})


def _refusal(result):
    assert getattr(result, 'is_error', False) is True
    document = json.loads(str(result))
    assert document == result.structured_json
    assert document['ok'] is False and document['submitted'] is False
    return document


def _workdir(tmp_path, queue):
    workdir = tmp_path / 'job'
    workdir.mkdir()
    (workdir / 'train.hope').write_text(f'[resource]\nqueue = {queue}\n', encoding='utf-8')
    return str(workdir)


def test_mcp_submit_to_m17_is_refused_before_the_call(monkeypatch, tmp_path):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    out = _refusal(bridge.call_tool('mcp__hope__submit_job',
                                    {'workdir': str(tmp_path), 'usergroup': 'g', 'queue': M17}))
    assert out['error_code'] == 'queue_off_limits' and out['stage'] == 'queue_policy'
    assert out['policy_entry']['id'] == 'm17' and out['policy_source'] == 'file'
    assert out['queue'] == M17 and 'm17' in out['error'] and out['policy_path'] in out['error']
    assert sent == []


@pytest.mark.parametrize('arguments', [
    lambda w: {'workdir': w, 'usergroup': 'g'},
    lambda w: {'workdir': w, 'usergroup': 'g', 'conf_name': 'train.hope'},
])
def test_m17_in_hope_body_of_workdir_is_refused(monkeypatch, tmp_path, arguments):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    out = _refusal(bridge.call_tool('mcp__hope__submit_job', arguments(_workdir(tmp_path, M17))))
    assert out['error_code'] == 'queue_off_limits' and sent == []


@pytest.mark.parametrize('arguments', [
    {'arguments': f'-Dafo.app.queue={M17} --foo'},
    {'extra': {'queue': M17}},
])
def test_m17_override_through_arguments_or_extra_is_refused(monkeypatch, tmp_path, arguments):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    out = _refusal(bridge.call_tool('mcp__hope__submit_job', {
        'workdir': _workdir(tmp_path, H800), 'usergroup': 'g', **arguments}))
    assert out['error_code'] == 'queue_off_limits' and sent == []


def test_rerun_and_clone_with_m17_body_are_refused(monkeypatch, tmp_path):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    body = f'queue = {M17}\n'
    _refusal(bridge.call_tool('mcp__hope__confirm_rerun', {
        'confirmation_token': 't', 'run_id': 'r', 'spec_job_id': 's',
        'job_name': 'j', 'hope_conf': body}))
    _refusal(bridge.call_tool('mcp__hope__catalog_write', {
        'operation': 'job_clone', 'path_params': {'jobId': '1'},
        'body': {'hopeConf': body}, 'dry_run': False}))
    assert sent == []


def test_codelab_create_on_m17_is_refused_and_other_queues_pass(monkeypatch, tmp_path):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    call = lambda body: bridge.call_tool('mcp__hope__catalog_write', {
        'operation': 'codelab_create', 'body': body, 'dry_run': False})
    assert _refusal(call({'queue': M17}))['error_code'] == 'queue_off_limits'
    assert call({'queue': H800}) == 'SENT' and call({'name': 'box'}) == 'SENT'
    assert len(sent) == 2


def test_refusal_is_audited_with_queue_and_caller(monkeypatch, tmp_path):
    import lib.mcp.client._bridge as bridge_module
    records = []
    monkeypatch.setattr(bridge_module, 'audit_log',
                        lambda event, **fields: records.append((event, fields)))
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    _refusal(bridge.call_tool('mcp__hope__submit_job',
                              {'workdir': str(tmp_path), 'usergroup': 'g', 'queue': M17},
                              meta={'tofu/project': '/projects/knowledge'}))
    [(event, fields)] = [r for r in records if r[0] == 'mcp_queue_policy_refusal']
    assert fields['queue'] == M17 and fields['owner_user_id'] == 1
    assert fields['project'] == '/projects/knowledge' and fields['tool'] == 'submit_job'


def test_h800_elastic_submission_passes_unchanged(monkeypatch, tmp_path):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    arguments = {'workdir': _workdir(tmp_path, H800), 'usergroup': 'g', 'queue': H800}
    assert bridge.call_tool('mcp__hope__submit_job', dict(arguments)) == 'SENT'
    assert bridge.call_tool('mcp__hope__confirm_rerun', {
        'confirmation_token': 't', 'run_id': 'r', 'spec_job_id': 's',
        'job_name': 'j', 'hope_conf': f'queue = {H800}\n'}) == 'SENT'
    assert bridge.call_tool('mcp__hope__get_job', {'hope_run_id': 'x'}) == 'SENT'
    assert len(sent) == 3


@pytest.mark.parametrize('policy_text', [None, '{broken', '{"version":1,"off_limits":[{"id":"x"}]}'])
def test_missing_or_invalid_policy_fails_closed_on_m17_like_the_shims(
        monkeypatch, tmp_path, policy_text):
    bridge, sent = _bridge(monkeypatch, tmp_path, policy_text)
    out = _refusal(bridge.call_tool('mcp__hope__submit_job',
                                    {'workdir': str(tmp_path), 'usergroup': 'g', 'queue': M17}))
    assert out['error_code'] == 'queue_off_limits'
    assert out['policy_source'] == 'built-in default' and out['policy_entry']['id'] == 'm17'
    assert bridge.call_tool('mcp__hope__submit_job',
                            {'workdir': str(tmp_path), 'usergroup': 'g', 'queue': H800}) == 'SENT'
    assert sent == [True]


def test_policy_file_is_the_single_source(monkeypatch, tmp_path):
    other = {**ENTRY, 'id': 'td', 'queue': OTHER_BAN}
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(other))
    _refusal(bridge.call_tool('mcp__hope__submit_job',
                              {'workdir': str(tmp_path), 'usergroup': 'g', 'queue': OTHER_BAN}))
    # A valid published file without m17 lifts the ban, as for the CLI shims.
    assert bridge.call_tool('mcp__hope__submit_job',
                            {'workdir': str(tmp_path), 'usergroup': 'g', 'queue': M17}) == 'SENT'


def test_submission_with_no_resolvable_queue_is_unresolved(monkeypatch, tmp_path):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY))
    empty = tmp_path / 'empty'
    empty.mkdir()
    out = _refusal(bridge.call_tool('mcp__hope__submit_job',
                                    {'workdir': str(empty), 'usergroup': 'g'}))
    assert out['error_code'] == 'queue_policy_unresolved' and sent == []


def test_server_launched_as_hope_mcp_under_another_name_is_guarded(monkeypatch, tmp_path):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY), server='jobs',
                           command='/opt/bin/hope-mcp')
    _refusal(bridge.call_tool('mcp__jobs__submit_job',
                              {'workdir': str(tmp_path), 'usergroup': 'g', 'queue': M17}))
    assert sent == []


def test_unrelated_server_tool_named_submit_job_is_not_inspected(monkeypatch, tmp_path):
    bridge, sent = _bridge(monkeypatch, tmp_path, _policy(ENTRY), server='other',
                           command='other-mcp')
    assert bridge.call_tool('mcp__other__submit_job', {'queue': M17}) == 'SENT'
