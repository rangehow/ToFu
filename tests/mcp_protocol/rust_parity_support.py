"""Shared fixtures for the Rust MCP client parity suites (packages/tofu-mcp).

The host binary comes from ``TOFU_MCP_HOST_BIN``, else Cargo builds it into a
target directory under pytest's temporary root. Every server is the recorded
session replay (tests/fixtures/mcp_sessions/replay_server.py) launched through
a ``hope-mcp``-named shim, so the Hope queue guard applies to it, and both
clients share the tool cache, runtime base and Hope policy location of the test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CRATE = ROOT / 'packages' / 'tofu-mcp'
SESSIONS = ROOT / 'tests' / 'fixtures' / 'mcp_sessions'
REPLAY = SESSIONS / 'replay_server.py'


def _cargo() -> str | None:
    found = shutil.which('cargo')
    if found:
        return found
    for home in (os.environ.get('CARGO_HOME'), str(Path.home() / '.cargo')):
        if home and (Path(home) / 'bin' / 'cargo').is_file():
            return str(Path(home) / 'bin' / 'cargo')
    return None


@pytest.fixture(scope='session')
def mcp_host_binary(tmp_path_factory) -> str:
    from lib.mcp.rust_host import BINARY_ENV
    given = os.environ.get(BINARY_ENV)
    if given:
        return given
    cargo = _cargo()
    if cargo is None:
        pytest.skip(f'{BINARY_ENV} is unset and cargo is unavailable')
    target = tmp_path_factory.getbasetemp().parent / 'tofu-mcp-target'
    subprocess.run([cargo, 'build', '--bin', 'tofu-mcp-host'], cwd=CRATE, check=True,
                   env={**os.environ, 'CARGO_TARGET_DIR': str(target)})
    return str(target / 'debug' / 'tofu-mcp-host')


class Scene:
    """One test's servers, policy file, logs and both clients' environments."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.cache = tmp_path / 'mcp-cache'
        self.base = tmp_path / 'runtime'
        self.policy_dir = tmp_path / 'router'
        monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(self.cache))
        # An inherited npm cache would send both clients' launch lock to the live data root.
        monkeypatch.delenv('npm_config_cache', raising=False)
        import lib.tools.hope_queue_policy_launcher as launcher
        monkeypatch.setattr(launcher, 'policy_path', lambda owner: self.policy_path(owner))
        import runtime_path_policy
        monkeypatch.setattr(runtime_path_policy, 'get_runtime_base', lambda: str(self.base))
        import lib.tools.process_environment as process_environment
        monkeypatch.setattr(process_environment, 'get_runtime_base', lambda: str(self.base))

    def policy_path(self, owner: int) -> str:
        return str(self.policy_dir / f'u{owner}' / 'HOPE_QUEUE_POLICY.json')

    def write_policy(self, owner: int, text: str | None) -> None:
        path = Path(self.policy_path(owner))
        if text is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def launcher(self, tag: str, name: str = 'hope-mcp', session: str = 'hope_like.json') -> str:
        """A ``name`` launcher whose replay logs to ``<tag>.log`` and keeps ``<tag>.state``."""
        directory = self.tmp / 'bin' / tag
        directory.mkdir(parents=True, exist_ok=True)
        shim = directory / name
        shim.write_text(f'#!/bin/sh\nexec {sys.executable} {REPLAY} {SESSIONS / session} '
                        f'{self.tmp / (tag + ".log")} {self.tmp / (tag + ".state")}\n', encoding='utf-8')
        shim.chmod(0o755)
        return str(shim)

    def calls(self, tag: str) -> list[dict]:
        """Every ``tools/call`` the ``tag`` launcher's server received."""
        log = self.tmp / f'{tag}.log'
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        return [row['params'] for row in rows if row['method'] == 'tools/call']

    def host_settings(self) -> dict:
        return {
            'runtime_base': str(self.base),
            'repo_root': str(ROOT),
            'python_minor': f'{sys.version_info.major}.{sys.version_info.minor}',
            'policy_path_template': str(self.policy_dir / 'u{owner}' / 'HOPE_QUEUE_POLICY.json'),
        }


@pytest.fixture
def scene(tmp_path, monkeypatch) -> Scene:
    return Scene(tmp_path, monkeypatch)


@pytest.fixture
def rust_host(mcp_host_binary, scene):
    from lib.mcp.rust_host import RustMCPHost
    host = RustMCPHost(mcp_host_binary, env=dict(os.environ))
    host.call('configure', scene.host_settings(), timeout=30)
    yield host
    host.close()


def python_bridge(monkeypatch, owner: int | None = 1):
    from lib.mcp.client import MCPBridge
    bridge = MCPBridge(owner_user_id=owner)
    monkeypatch.setattr(bridge, '_stored_server_config', lambda name, refresh_browser=False: (None, None))
    return bridge


def outcome(call):
    """A call's comparable result: the text plus every runtime payload, or its failure category."""
    from lib.mcp.bridge_relay import _error_kind
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 - the category is the comparison
        return ('raised', _error_kind(exc), type(exc).__name__)
    fields = {}
    for key in ('image_contents', 'structured_json', 'document_text', 'document_error',
                'is_error', 'content_blocks', 'retention_error'):
        if hasattr(result, key):
            value = getattr(result, key)
            fields[key] = list(value) if isinstance(value, tuple) else value
    return ('result', str(result), fields)
