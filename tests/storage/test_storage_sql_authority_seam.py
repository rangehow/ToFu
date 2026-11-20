"""Replacement SQL authorities require explicit, test-root-scoped injection."""

from __future__ import annotations

import json

import pytest

from lib.storage import StorageSupervisor
from lib.storage_authority_sql.cli import main

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('backend', ['sqlite', 'postgres', None])
def test_supervisor_ignores_inherited_test_binary(tmp_path, monkeypatch, backend):
    monkeypatch.setenv('TOFU_TEST_SQL_AUTHORITY_BIN', str(tmp_path / 'not-an-authority'))
    assert StorageSupervisor(
        project_root=tmp_path, backend=backend,
    )._sql_authority_command is None
    assert StorageSupervisor(connection_file='')._sql_authority_command is None


def test_maintenance_ignores_inherited_test_binary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('TOFU_TEST_SQL_AUTHORITY_BIN', str(tmp_path / 'not-an-authority'))
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', '')

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path), 'status',
    ]) == 0
    assert json.loads(capsys.readouterr().out)['backend'] == 'sqlite'


def test_explicit_replacement_requires_a_sqlite_test_root(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_TEST_SQL_AUTHORITY_BIN', str(tmp_path / 'not-an-authority'))
    with pytest.raises(ValueError, match='explicit test project root'):
        StorageSupervisor(sql_authority_command=('/opt/x', 'serve'), connection_file='')
    for backend in ('postgres', 'tofudb', None):
        with pytest.raises(ValueError, match='sqlite backend'):
            StorageSupervisor(
                project_root=tmp_path, backend=backend,
                sql_authority_command=('/opt/x', 'serve'))
    command = ['/opt/x', 'serve']
    selected = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', sql_authority_command=command)
    command[0] = '/opt/changed'
    assert selected._sql_authority_command == ('/opt/x', 'serve')
