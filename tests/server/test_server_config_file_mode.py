"""server_config.json stays owner-only (0600) through every writer.

The file still holds plaintext legacy provider keys. Settings Save used to
republish it through ``lib.json_store`` with the default 0644 mode, so a save
made a 0600 file readable by every local user. Settings Save now creates and
rewrites it 0600, and the shared atomic writer never widens an existing
file's group/other access, so background writers that pass no mode (learned
limits, search settings, ...) keep it owner-only as well.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat

import pytest

pytestmark = pytest.mark.unit


@contextlib.contextmanager
def _permissive_umask():
    """Prove the mode does not depend on the host's restrictive umask."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    import lib.config_dir as config_dir
    import routes.config as config_routes
    path = tmp_path / 'server_config.json'
    monkeypatch.setattr(config_routes, '_SERVER_CONFIG_PATH', str(path))
    monkeypatch.setattr(config_dir, 'CONFIG_DIR', str(tmp_path))
    return path


def _save(flask_client):
    response = flask_client.post('/api/v4/server-config', json={'hidden_models': ['mode-probe-model']})
    assert response.status_code == 200, response.get_data(as_text=True)


def test_settings_save_keeps_an_owner_only_file_owner_only(flask_client, config_path):
    config_path.write_text(json.dumps({'providers': [{'id': 'p', 'api_keys': ['sk-test']}]}))
    os.chmod(config_path, 0o600)
    with _permissive_umask():
        _save(flask_client)
    assert json.loads(config_path.read_text())['hidden_models'] == ['mode-probe-model']
    assert _mode(config_path) == 0o600


def test_settings_save_creates_the_file_owner_only(flask_client, config_path):
    assert not config_path.exists()
    with _permissive_umask():
        _save(flask_client)
    assert _mode(config_path) == 0o600


def test_settings_save_narrows_a_world_readable_file(flask_client, config_path):
    config_path.write_text('{}')
    os.chmod(config_path, 0o644)
    with _permissive_umask():
        _save(flask_client)
    assert _mode(config_path) == 0o600


def test_a_background_writer_without_a_mode_keeps_the_file_owner_only(config_path):
    from lib.model_info import _learn_model_limit
    config_path.write_text('{}')
    os.chmod(config_path, 0o600)
    with _permissive_umask():
        _learn_model_limit('mode-probe-model', 16_384)
    assert json.loads(config_path.read_text())['model_limits']['mode-probe-model'] == 16_384
    assert _mode(config_path) == 0o600
