"""Scratch SQL launches isolate their authority and environment."""
import json
import os
from pathlib import Path
import sys

import pytest

from lib.storage import StorageSupervisor
from lib.storage_authority_sql.cli import main

pytestmark = pytest.mark.api


def test_scratch_sqlite_pins_volume_without_changing_application_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'required')
    external = tmp_path / 'must-not-attach-to-this.json'
    external.write_text('not a sidecar connection')
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', str(external))
    supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite',
                                   sqlite_fastpath_mode='off', connection_file='', startup_timeout=30)
    try:
        client = supervisor.start()
        assert os.environ['TOFU_STORAGE_FASTPATH'] == 'required'
        assert os.environ['TOFU_STORAGE_CONNECTION_FILE'] == str(external)
        fastpath = client.metrics(deadline=5)['fastpath']
        assert not fastpath['active']
        assert 'disabled' in fastpath['reason']
        assert client.health()['ready']
    finally:
        supervisor.stop()


@pytest.fixture
def inherited_sql_candidate(tmp_path, monkeypatch):
    # A bogus executable must not prevent a normal Python authority launch.
    monkeypatch.setenv('TOFU_TEST_SQL_AUTHORITY_BIN', str(tmp_path / 'missing-candidate'))
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', '')
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')


def test_supervisor_ignores_inherited_sql_candidate(tmp_path, inherited_sql_candidate):
    with StorageSupervisor(project_root=tmp_path, backend='sqlite',
                           connection_file='', startup_timeout=30) as supervisor:
        assert supervisor.client.health()['backend'] == 'sqlite'
        if sys.platform == 'linux':
            pid = supervisor.status()['pid']
            assert pid is not None
            proc = Path('/proc') / str(pid)
            assert (proc / 'exe').resolve(strict=True) == Path(sys.executable).resolve(strict=True)
            assert (proc / 'cmdline').read_bytes().split(b'\0')[:-1] == [
                os.fsencode(sys.executable), b'-m', b'lib.storage_authority_sql',
            ]


def test_maintenance_ignores_inherited_sql_candidate(tmp_path, inherited_sql_candidate, capsys):
    assert main(['--backend', 'sqlite', '--project-root', str(tmp_path), 'status']) == 0
    assert json.loads(capsys.readouterr().out)['backend'] == 'sqlite'
