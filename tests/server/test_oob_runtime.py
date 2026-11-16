"""Out-of-band repair runtime adapter (serverctl_pkg/oob_runtime.py, SELF-REPAIR O4).

Specification of designs/headless-recovery-agent/DESIGN.md, piece O4: the
repair session is Tofu's own storage-free headless runtime, `tofu-agent run`,
started from a pinned installed copy of the last gate-passed commit, not from
the live checkout. Its model route is the project's gateway through the
watcher's Unix-socket relay with a placeholder secret; opencode is the
fallback when the pinned runtime fails `tofu-agent doctor`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from serverctl_pkg import oob_relay, oob_runtime
from tests.support.production_api_guard import production_api_guard  # noqa: F401 - autouse guard

pytestmark = pytest.mark.unit


def test_envelope_matches_the_tb_harness_shape_with_a_placeholder_secret():
    envelope = oob_runtime.routing_envelope(model='kimi-k3.1-pro-m17', key_hint='199c')
    assert envelope['model'] == {'creator_id': 'moonshot', 'model_id': 'kimi-k3.1-pro-m17'}
    routing = envelope['model_routing']
    assert routing['contract_version'] == 'tofu.model-routing/v2'
    [connection] = routing['connections']
    # The sandbox's loopback bridge to the relay: no Tofu transport speaks http+unix.
    assert connection['base_url'] == oob_relay.SANDBOX_BASE_URL
    assert connection['protocol'] == 'openai'
    [credential] = routing['credentials']
    assert credential['kind'] == 'api_key' and credential['key_hint'] == '199c'
    assert credential['secret_reference'] in envelope['credential_secrets']
    assert all('relay' in secret for secret in envelope['credential_secrets'].values())
    [deployment] = routing['deployments']
    assert deployment['wire_model_id'] == 'kimi-k3.1-pro-m17'
    assert routing['providers'][0]['provider_id'] == 'example-corp'
    assert 'thinking' in routing['models'][0]['capabilities']


def test_envelope_serializes_without_the_real_key():
    envelope = oob_runtime.routing_envelope(model='glm-5.3', key_hint='abcd')
    text = json.dumps(envelope)
    assert 'sk-' not in text and '199c' not in text
    assert 'relay-held' in text


def test_pinned_runtime_is_never_the_live_checkout(tmp_path):
    runtime = oob_runtime.PinnedRuntime(root=tmp_path / 'runtime', commit='abc123')
    argv = runtime.argv(prompt=Path('/tmp/prompt.md'), output=Path('/tmp/result.json'), workspace=tmp_path,
                        routing=Path('/tmp/routing.json'))
    assert argv[0].endswith('/bin/python3')
    assert argv[1:4] == ['-I', '-m', 'tofu_agent']
    assert 'run' in argv
    assert '--cwd' in argv and argv[argv.index('--cwd') + 1] == str(tmp_path)
    assert '--task-file' in argv and argv[argv.index('--task-file') + 1] == '/tmp/prompt.md'
    assert '--output' in argv and argv[argv.index('--output') + 1] == '/tmp/result.json'
    assert runtime.python.exists() is False or runtime.python.is_symlink() or runtime.python.is_file()


def test_tools_are_minimal_and_no_deadline_is_imposed(tmp_path):
    runtime = oob_runtime.PinnedRuntime(root=tmp_path / 'runtime', commit='abc123')
    argv = runtime.argv(prompt=Path('/tmp/p'), output=Path('/tmp/o'), workspace=tmp_path, routing=Path('/tmp/r'))
    # The empty selection resolves every optional capability off
    # (tests/tofu_agent/test_tool_selection_least_privilege.py proves it end to end).
    assert argv[argv.index('--tools') + 1] == ''
    # Omitted --timeout-s reaches the runtime as null: no deadline.
    assert '--timeout-s' not in argv
    assert not hasattr(oob_runtime, 'REPAIR_BUDGET_S')


def test_the_session_reads_its_route_from_its_routing_file_and_no_env_file(tmp_path):
    runtime = oob_runtime.PinnedRuntime(root=tmp_path / 'runtime', commit='abc123')
    argv = runtime.argv(prompt=Path('/tmp/p'), output=Path('/tmp/o'), workspace=tmp_path,
                        routing=Path('/tmp/routing.json'))
    assert argv[argv.index('--model-routing-file') + 1] == '/tmp/routing.json'
    assert argv[argv.index('--env-file') + 1] == '/dev/null'  # DESIGN.md "Runtime": never a .env credential


def test_doctor_failure_selects_opencode(tmp_path):
    runtime = oob_runtime.PinnedRuntime(root=tmp_path / 'runtime', commit='abc123')
    runtime.doctor = lambda: (False, 'bad archive')
    fallback = oob_runtime.OpenCodeRuntime(binary=Path('/usr/bin/opencode'))
    assert oob_runtime.select_runtime(runtime, fallback) is fallback


def test_doctor_success_selects_tofu_agent(tmp_path):
    runtime = oob_runtime.PinnedRuntime(root=tmp_path / 'runtime', commit='abc123')
    runtime.doctor = lambda: (True, 'ok')
    fallback = oob_runtime.OpenCodeRuntime(binary=Path('/usr/bin/opencode'))
    assert oob_runtime.select_runtime(runtime, fallback) is runtime


def test_opencode_command_uses_a_fixed_recovery_agent_and_local_state(tmp_path):
    runtime = oob_runtime.OpenCodeRuntime(binary=Path('/usr/bin/opencode'))
    argv = runtime.argv(prompt=Path('/tmp/p'), output=Path('/tmp/o'), workspace=tmp_path)
    assert argv[0] == '/usr/bin/opencode'
    assert 'run' in argv and '--standalone' in argv and '--archived' in argv
    assert '--agent' in argv and argv[argv.index('--agent') + 1] == 'tofu-recovery'
    assert '--format' in argv and argv[argv.index('--format') + 1] == 'json'
