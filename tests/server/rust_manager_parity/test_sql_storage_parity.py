"""The resident SQL storage authority under both managers (test_sql_storage_service.py).

The real ``serverctl_pkg.native_storage_host`` hosts the selected SQLite
authority: its plan carries the backend and configuration without native
engine admission, it exports the connection only for its current owned
generation, it outlives the API worker across an unchanged restart, it
relaunches its SQL child under the same Web owner, and a stop removes the
connection and releases the storage lease. Only the API worker is a double.
"""
from __future__ import annotations

import json
import os
import shutil
import signal

import pytest

from server_manager import read_storage_lease_status
from tests.server.rust_manager_parity.conftest import REPO, wait_for

pytestmark = pytest.mark.api


@pytest.fixture
def prepare_project():
    def prepare(project):
        (project / 'contracts').mkdir()
        shutil.copy(REPO / 'contracts/native_storage_connection_v2.json',
                    project / 'contracts/native_storage_connection_v2.json')
        (project / '.env').write_text(
            'TOFU_DEPLOYMENT_MODE=personal\nTOFU_PERSONAL_STORAGE_BACKEND=sqlite\n'
            f'TOFU_DATA_DIR={project / "data"}\nTOFU_PROJECT_PATH={project}\n')
    return prepare


def connection(manager) -> dict:
    try:
        return json.loads((manager.project / 'data/native-storage-connection.json').read_text())
    except (OSError, ValueError):
        return {}


def settled(manager) -> dict:
    record = wait_for(lambda: manager.storage_record().get('state') in {'ready', 'failed'} and manager.storage_record(),
                      'the SQL storage host to settle', timeout=120)
    assert record['state'] == 'ready', record.get('error')
    return record


def test_the_sql_authority_is_hosted_exported_and_outlives_the_api_worker(manager):
    first = manager.start_serving()
    host = settled(manager)
    assert host['plan']['backend'] == 'sqlite'
    assert 'binary' not in host['plan'] and len(host['plan']['configurationSha256']) == 64
    document = wait_for(lambda: connection(manager), 'the exported connection')
    assert document['backend'] == 'sqlite'
    worker = json.loads((manager.project / 'env-worker.json').read_text())
    assert worker['TOFU_STORAGE_CONNECTION_FILE'] == str(manager.project / 'data/native-storage-connection.json')
    lease = read_storage_lease_status(manager.project / 'data')
    assert lease['held'] and lease['kind'] == 'storage_authority_sql'

    # Its SQL child dies: the host relaunches it under the same Web owner.
    os.kill(host['nativePid'], signal.SIGKILL)
    relaunched = wait_for(lambda: (current := connection(manager)) and current.get('token') != document['token']
                          and current, 'a relaunched SQL child', timeout=120)
    assert relaunched['backend'] == 'sqlite'
    assert manager.storage_record()['pid'] == host['pid']
    assert manager.wait_serving()['pid'] == first['pid']

    # An unchanged restart replaces the API worker and keeps the authority.
    code, result = manager.control('restart')
    assert code == 200 and result.get('ok') is True, (code, result)
    manager.wait_serving(other_than=first['pid'])
    assert manager.storage_record()['serviceId'] == host['serviceId']

    code, stopped = manager.control('stop')
    assert code == 200 and stopped.get('ok') is True, (code, stopped)
    assert not (manager.project / 'data/native-storage-connection.json').exists()
    assert not read_storage_lease_status(manager.project / 'data')['held']
