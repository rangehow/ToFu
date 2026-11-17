"""A node's ID is unique per host even when several hosts share one home.

Regression: ``agent_id`` lived only in ``~/.tofu/desktop_agent.json``. Where
containers share a home directory (network home on DolphinFS), every container
read the same ID, so the server saw one node whose registry entry the
containers overwrote poll by poll and commands reached whichever polled first.
"""
import json

import pytest

from lib.desktop_agent import _run, host_identity

pytestmark = pytest.mark.unit


@pytest.fixture
def shared_home(tmp_path, monkeypatch):
    path = tmp_path / 'home' / '.tofu' / 'desktop_agent.json'
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(path))
    return path


def _on_host(monkeypatch, machine_id, hostname):
    monkeypatch.setattr(host_identity, '_machine_id', lambda: machine_id)
    monkeypatch.setattr(host_identity, '_hostname_scoped', lambda: True)
    monkeypatch.setattr(host_identity.socket, 'gethostname', lambda: hostname)


def test_two_containers_sharing_one_config_get_distinct_stable_ids(shared_home, monkeypatch):
    # Containers of one image share /etc/machine-id; their hostnames differ.
    _on_host(monkeypatch, 'image-machine-id', 'container-a')
    first = _run._ensure_agent_id()
    _on_host(monkeypatch, 'image-machine-id', 'container-b')
    second = _run._ensure_agent_id()
    assert first and second and first != second

    # Restarts keep each host's own ID.
    assert _run._ensure_agent_id() == second
    _on_host(monkeypatch, 'image-machine-id', 'container-a')
    assert _run._ensure_agent_id() == first
    saved = json.loads(shared_home.read_text())
    assert sorted(saved['agent_ids'].values()) == sorted([first, second])
    assert 'container-a' not in json.dumps(saved)  # only digests are stored


def test_existing_paired_node_keeps_its_id(shared_home, monkeypatch):
    shared_home.parent.mkdir(parents=True)
    shared_home.write_text(json.dumps({
        'agent_id': 'legacy-node-id',
        'attachments': [{'id': 'a1', 'url': 'https://home.example', 'secret': 's'}]}))
    _on_host(monkeypatch, 'mid-1', 'laptop')
    assert _run._ensure_agent_id() == 'legacy-node-id'
    assert _run._ensure_agent_id() == 'legacy-node-id'
    saved = json.loads(shared_home.read_text())
    assert saved['agent_id'] == 'legacy-node-id'  # older builds still read it
    assert saved['attachments'][0]['id'] == 'a1'

    # A second host that already shared the legacy ID is re-keyed; the first
    # host that claimed it keeps it, so its pairing and device waits survive.
    _on_host(monkeypatch, 'mid-1', 'other-container')
    rekeyed = _run._ensure_agent_id()
    assert rekeyed != 'legacy-node-id'
    _on_host(monkeypatch, 'mid-1', 'laptop')
    assert _run._ensure_agent_id() == 'legacy-node-id'


def test_host_key_combines_machine_identity_and_hostname(monkeypatch):
    _on_host(monkeypatch, 'm', 'a')
    key_a = host_identity.host_key()
    _on_host(monkeypatch, 'm', 'b')
    assert host_identity.host_key() != key_a
    _on_host(monkeypatch, 'n', 'a')
    assert host_identity.host_key() != key_a
    _on_host(monkeypatch, 'm', 'a')
    assert host_identity.host_key() == key_a
    _on_host(monkeypatch, '', 'a')
    assert host_identity.host_key()  # hostname alone still scopes the ID
