"""Node-side identity on shared home directories (lib/desktop_agent/config.py).

Hosts that share one ``desktop_agent.json`` (containers on a network home)
must never share a node ID *or* a credential; a host whose identity key
changed must keep its node when it is provably the same device (same
container renamed) and otherwise name its predecessor when it pairs again.
"""
import json
import threading

import pytest

from lib.desktop_agent import config, host_identity

pytestmark = pytest.mark.unit


@pytest.fixture
def shared_home(tmp_path, monkeypatch):
    path = tmp_path / 'home' / '.tofu' / 'desktop_agent.json'
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(path))
    return path


def _on(monkeypatch, *, machine='image-mid', hostname='host', container=''):
    monkeypatch.setattr(host_identity, '_machine_id', lambda: machine)
    monkeypatch.setattr(host_identity, '_hostname_scoped', lambda: True)
    monkeypatch.setattr(host_identity.socket, 'gethostname', lambda: hostname)
    monkeypatch.setattr(host_identity, '_container_id', lambda: container)


def _attach(url, secret):
    from lib.desktop_wire.node_protocol import attachment_id
    return config.save_attachment({'id': attachment_id(url), 'url': url, 'secret': secret,
                                   'state': 'active'})


def test_twins_on_one_shared_config_hold_separate_credentials(shared_home, monkeypatch):
    # Host networking: both containers report the host's hostname.
    _on(monkeypatch, container='c' * 64)
    first = config.node_identity()['node_id']
    _attach('https://home.example', 'secret-of-twin-1')
    _on(monkeypatch, container='d' * 64)
    second = config.node_identity()['node_id']
    assert second != first
    assert config.attachments() == []  # twin 2 does not inherit twin 1's credential
    _attach('https://home.example', 'secret-of-twin-2')
    assert [a['secret'] for a in config.attachments()] == ['secret-of-twin-2']
    _on(monkeypatch, container='c' * 64)
    assert [a['secret'] for a in config.attachments()] == ['secret-of-twin-1']
    # Removing a server on one twin leaves the other's link alone.
    assert config.remove_attachment(config.attachments()[0]['id'])
    _on(monkeypatch, container='d' * 64)
    assert [a['secret'] for a in config.attachments()] == ['secret-of-twin-2']
    assert 'c' * 64 not in shared_home.read_text()  # only digests are stored


def test_legacy_attachments_stay_with_the_host_owning_the_legacy_id(shared_home, monkeypatch):
    shared_home.parent.mkdir(parents=True)
    shared_home.write_text(json.dumps({
        'agent_id': 'legacy-node',
        'attachments': [{'id': 'a1', 'url': 'https://home.example', 'secret': 's'}]}))
    _on(monkeypatch, hostname='laptop')
    assert config.node_identity()['node_id'] == 'legacy-node'
    assert [a['id'] for a in config.attachments()] == ['a1']
    _on(monkeypatch, hostname='other')
    assert config.node_identity()['node_id'] != 'legacy-node'
    assert config.attachments() == []
    # Older builds still read the top-level fields of the legacy owner.
    saved = json.loads(shared_home.read_text())
    assert saved['agent_id'] == 'legacy-node' and saved['attachments'][0]['id'] == 'a1'


def test_the_same_container_under_a_new_hostname_keeps_its_node(shared_home, monkeypatch):
    _on(monkeypatch, hostname='before', container='e' * 64)
    node = config.node_identity()['node_id']
    _attach('https://home.example', 'kept')
    _on(monkeypatch, hostname='after', container='e' * 64)
    assert config.node_identity() == {'node_id': node, 'predecessor': None}
    assert [a['secret'] for a in config.attachments()] == ['kept']


def test_a_recreated_container_names_its_predecessor_without_its_credential(
        shared_home, monkeypatch):
    _on(monkeypatch, hostname='worker-0', container='f' * 64)
    old = config.node_identity()['node_id']
    _attach('https://home.example', 'old-secret')
    _on(monkeypatch, hostname='worker-0', container='0' * 64)
    identity = config.node_identity()
    assert identity['node_id'] != old and identity['predecessor'] == old
    assert config.attachments() == []
    config.clear_predecessor()
    assert config.node_identity()['predecessor'] is None


def test_a_renamed_bare_linux_host_names_its_predecessor(shared_home, monkeypatch):
    # Without a container ID a rename and a cloned VM look alike: re-pair, owner decides.
    _on(monkeypatch, machine='vm-mid', hostname='old-name')
    old = config.node_identity()['node_id']
    _on(monkeypatch, machine='vm-mid', hostname='new-name')
    assert config.node_identity()['predecessor'] == old


def test_a_phase0_keyed_container_entry_is_claimed_once(shared_home, monkeypatch):
    # A phase-0 build keyed a container by machine id + hostname only and
    # recorded no host facts.
    _on(monkeypatch, hostname='shared-host', container='')
    phase0 = 'phase0-node'
    shared_home.parent.mkdir(parents=True)
    shared_home.write_text(json.dumps({
        'agent_id': phase0, 'agent_ids': {host_identity.phase0_host_key(): phase0},
        'attachments': [{'id': 'a1', 'url': 'https://home.example', 'secret': 'phase0-secret'}]}))
    _on(monkeypatch, hostname='shared-host', container='1' * 64)
    assert config.node_identity()['node_id'] == phase0
    assert [a['secret'] for a in config.attachments()] == ['phase0-secret']
    _on(monkeypatch, hostname='shared-host', container='2' * 64)
    twin = config.node_identity()
    assert twin['node_id'] != phase0 and twin['predecessor'] == phase0


def test_concurrent_first_starts_on_one_file_get_distinct_ids(shared_home, monkeypatch):
    ids, errors = {}, []
    real_container = host_identity._container_id
    local = threading.local()
    monkeypatch.setattr(host_identity, '_machine_id', lambda: 'm')
    monkeypatch.setattr(host_identity, '_container_id', lambda: getattr(local, 'c', ''))

    def start(index):
        local.c = '%064x' % (index + 1)
        try:
            ids[index] = config.node_identity()['node_id']
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=start, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors and len(set(ids.values())) == 5
    assert callable(real_container)
