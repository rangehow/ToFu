"""Owner Hope queue policy: shared schema, MCP binding, and CLI guard.

Before the change, no Tofu-owned code read the Router's off-limits list or
guarded the hope/mlp commands, so every submission spec below reaches the
fake CLI instead of a typed refusal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

from lib.mcp.queue_policy import canonical_project_path, project_meta_for_tool
from lib.mcp.stdio_environment import build_stdio_environment
from lib.tools.hope_queue_policy import (
    CAPABILITY, DEFAULT_ENTRIES, PROJECT_META, Policy, load_policy,
    queues_in_conf, refusal, submission_request,
)
from lib.tools.hope_queue_policy_launcher import (
    apply_hope_queue_policy, policy_path, prepare_shim_directory,
)

M17 = 'root.hldy_training_cluster.your-user.h910c_m17_public'
ALLOWED = 'root.hldy_training_cluster.your-user.h910c_training_data'
PROJECT = '/path/to/your/project'
OTHER = '/path/to/your/data'
ENTRY = {'id': 'm17', 'queue': M17, 'since': '2026-11-13T11:01+08:00',
         'reason': 'queue admin 杨睿 asked for release; owner decision'}


def write_policy(path, entries=None, raw=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else
                    json.dumps({'version': 1, 'off_limits': list(entries or [])}), encoding='utf-8')
    return path


def _assert_refused(out, entry='m17', source='file'):
    assert out['ok'] is False and out['error_code'] == 'queue_off_limits'
    assert out['policy_entry']['id'] == entry and out['policy_source'] == source
    assert entry in out['error'] and out['policy_path'] in out['error']


def test_fail_safe_default_is_exactly_the_router_v1_entry():
    assert list(DEFAULT_ENTRIES) == [ENTRY]


def test_schema_matches_reader_and_default():
    from lib.tools.hope_queue_contract import schema, validate_document
    assert schema()['x-fail-safe-default'] == {'version': 1, 'off_limits': [ENTRY]}
    validate_document({'version': 1, 'off_limits': [ENTRY]})
    validate_document(schema()['x-fail-safe-default'])


@pytest.mark.parametrize('raw', [None, '{broken', '{"version":1,"off_limits":[{"id":"x"}]}'])
def test_missing_or_unparsable_file_keeps_m17_but_not_other_queues(tmp_path, raw):
    path = tmp_path / 'HOPE_QUEUE_POLICY.json'
    if raw is not None:
        path.write_text(raw)
    policy = load_policy(str(path))
    assert policy.source == 'built-in default' and policy.default_reason
    assert list(policy.entries) == [ENTRY]
    _assert_refused(refusal([M17], policy=policy), source='built-in default')
    assert refusal([ALLOWED], policy=policy) is None


def test_valid_empty_list_lifts_bans_and_edits_apply_without_cache(tmp_path):
    path = write_policy(tmp_path / 'HOPE_QUEUE_POLICY.json')
    assert refusal([M17], policy=load_policy(str(path))) is None
    write_policy(path, [ENTRY])
    _assert_refused(refusal([M17], policy=load_policy(str(path))))
    write_policy(path, [])
    assert refusal([M17], policy=load_policy(str(path))) is None


def test_project_scope_and_unknown_callers():
    policy = Policy('/policy', ({**ENTRY, 'projects': [PROJECT]},))
    _assert_refused(refusal([M17], project=PROJECT, policy=policy))
    _assert_refused(refusal([M17], project=None, policy=policy))
    assert refusal([M17], project=OTHER, policy=policy) is None


def test_leaf_name_and_conf_parser():
    policy = Policy('/policy', (ENTRY,))
    _assert_refused(refusal(['h910c_m17_public'], policy=policy))
    assert queues_in_conf('[resource]\nqueue = root.x\nafo.app.queue=root.y\n# queue=z\n') == ['root.x', 'root.y']


def test_stdio_policy_path_is_owner_derived_not_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr('lib.mcp.client._vendor._propagate_pool_proxy_env', lambda env: None)
    environment = build_stdio_environment(
        {'env': {'TOFU_HOPE_QUEUE_POLICY': '/tmp/attacker-policy'}},
        owner_user_id=1, prepare_directories=False)
    assert environment['TOFU_HOPE_QUEUE_POLICY'] == policy_path(1)
    assert environment['TOFU_HOPE_QUEUE_POLICY'].endswith('/router/1/HOPE_QUEUE_POLICY.json')
    unowned = build_stdio_environment({}, prepare_directories=False)
    assert 'TOFU_HOPE_QUEUE_POLICY' not in unowned


def _info(meta=None):
    return {'meta': meta or {}}


def test_project_meta_only_for_declared_submission_tools():
    task = {'config': {'canonicalProjectPath': PROJECT, 'projectPath': '/tmp/worktree'}}
    assert canonical_project_path(task) == PROJECT
    assert project_meta_for_tool(_info({CAPABILITY: {'version': 1}}), task) == {PROJECT_META: PROJECT}
    assert project_meta_for_tool(_info(), task) is None
    assert project_meta_for_tool(_info({CAPABILITY: {'version': 2}}), task) is None
    assert project_meta_for_tool(_info({CAPABILITY: {'version': 1}}), {}) is None


def test_cli_submission_parser_variants(tmp_path):
    conf = tmp_path / 'job.hope'
    conf.write_text(f'[resource]\nqueue = {M17}\n')
    for argv in [
        ['hope', 'run', str(conf)],
        ['hope', 'quickrun', str(conf)],
        ['mlp', 'hope', 'run', str(conf)],
        ['hope', 'run', '-Dqueue=root.other', str(conf)],
        ['mlp', 'job', 'rerun', '--hope-conf', f'[resource]\nqueue={M17}\n'],
        ['mlp', 'job', 'clone', '--hope-conf', str(conf)],
    ]:
        request = submission_request(argv, str(tmp_path))
        assert request is not None, argv
        assert M17 in request[0], argv
    assert submission_request(['hope', 'status', str(conf)], str(tmp_path)) is None
    assert submission_request(['mlp', 'job', 'list'], str(tmp_path)) is None


def _fake_cli(path, name, log):
    body = f'#!/bin/sh\necho {name}:$@ >> {shlex.quote(str(log))}\n'
    path.write_text(body)
    path.chmod(0o755)


def _invoke_shim(directory, name, args, policy, project, cwd):
    launcher = Path(directory) / name
    command = [str(launcher), *args]
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


def test_cli_shim_refuses_banned_and_passes_allowed(tmp_path, monkeypatch):
    real = tmp_path / 'real'
    real.mkdir()
    log = tmp_path / 'calls.log'
    _fake_cli(real / 'hope', 'hope', log)
    _fake_cli(real / 'mlp', 'mlp', log)
    environment = {'PATH': f'{real}:/usr/bin:/bin'}
    policy = write_policy(tmp_path / 'HOPE_QUEUE_POLICY.json', [ENTRY])
    monkeypatch.setattr('lib.tools.hope_queue_policy_launcher.policy_path', lambda owner: str(policy))
    directory = prepare_shim_directory(environment, owner_user_id=1, project_path=PROJECT,
                                      cwd=str(tmp_path))
    assert directory
    banned_conf = tmp_path / 'banned.hope'
    banned_conf.write_text(f'[resource]\nqueue = {M17}\n')
    allowed_conf = tmp_path / 'allowed.hope'
    allowed_conf.write_text(f'[resource]\nqueue = {ALLOWED}\n')
    result = _invoke_shim(directory, 'hope', ['run', str(banned_conf)], policy, PROJECT, tmp_path)
    assert result.returncode == 2
    assert 'TOFU_QUEUE_POLICY_REFUSAL' in result.stderr
    payload = json.loads(next(line.split(' ', 1)[1] for line in result.stderr.splitlines()
                              if line.startswith('TOFU_QUEUE_POLICY_REFUSAL ')))
    _assert_refused(payload)
    assert not log.exists()
    result = _invoke_shim(directory, 'hope', ['run', str(allowed_conf)], policy, PROJECT, tmp_path)
    assert result.returncode == 0, result.stderr
    assert log.read_text().startswith('hope:run')
    result = _invoke_shim(directory, 'mlp', ['job', 'rerun', '--hope-conf',
                                             f'[resource]\nqueue={M17}\n'], policy, PROJECT, tmp_path)
    assert result.returncode == 2
    result = _invoke_shim(directory, 'hope', ['status', str(banned_conf)], policy, PROJECT, tmp_path)
    assert result.returncode == 0
