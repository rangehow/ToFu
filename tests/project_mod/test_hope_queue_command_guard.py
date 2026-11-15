"""run_command forwards the canonical project and guards the hope/mlp PATH."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat

import pytest

from lib.project_mod.run_command import _get_cmd_env, tool_run_command

pytestmark = pytest.mark.unit

M17 = 'root.hldy_training_cluster.your-user.h910c_m17_public'
ALLOWED = 'root.hldy_training_cluster.your-user.h910c_training_data'


def _policy(path, entries=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 1, 'off_limits': list(entries or [])}))
    return path


def _entry():
    return {'id': 'm17', 'queue': M17, 'since': '2026-11-13T11:01+08:00',
            'reason': 'queue admin 杨睿 asked for release; owner decision'}


def _fake_cli(path, log):
    path.write_text(f'#!/bin/sh\necho hope:$@ >> {shlex.quote(str(log))}\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_command_environment_guards_hope_and_mlp(tmp_path, monkeypatch):
    real = tmp_path / 'real'
    real.mkdir()
    log = tmp_path / 'calls.log'
    _fake_cli(real / 'hope', log)
    _fake_cli(real / 'mlp', log)
    policy = _policy(tmp_path / 'policy.json', [_entry()])
    import lib.tools.hope_queue_policy_launcher as launcher
    monkeypatch.setattr(launcher, 'policy_path', lambda owner: str(policy))
    env = _get_cmd_env(str(tmp_path), project_root=str(tmp_path), owner_user_id=1,
                       project_path='/canonical/knowledge')
    assert env['TOFU_HOPE_QUEUE_POLICY'] == str(policy)
    # A project venv also gets the shim last, so its own hope cannot shadow it.
    venv = tmp_path / '.venv' / 'bin'
    venv.mkdir(parents=True)
    (tmp_path / '.venv' / 'pyvenv.cfg').write_text('home = /usr\n')
    (venv / 'python').write_text('#!/bin/sh\n')
    (venv / 'python').chmod(0o755)
    _fake_cli(venv / 'hope', log)
    env = _get_cmd_env(str(tmp_path), project_root=str(tmp_path), owner_user_id=1,
                       project_path='/canonical/knowledge')
    shim = env['PATH'].split(os.pathsep)[0]
    assert shim.startswith('/tmp/tofu-hope-queue-shims-')
    assert shim not in str(real)
    banned = tmp_path / 'banned.hope'
    banned.write_text(f'[resource]\nqueue = {M17}\n')
    allowed = tmp_path / 'allowed.hope'
    allowed.write_text(f'[resource]\nqueue = {ALLOWED}\n')
    output = tool_run_command(str(tmp_path), f'hope run {shlex.quote(str(banned))}',
                              task={'_userId': 1})
    assert 'TOFU_QUEUE_POLICY_REFUSAL' in output
    assert 'm17' in output and str(policy) in output
    assert not log.exists()
    output = tool_run_command(str(tmp_path), f'hope run {shlex.quote(str(allowed))}',
                              task={'_userId': 1})
    assert 'TOFU_QUEUE_POLICY_REFUSAL' not in output
    assert log.exists()
