"""Node-side identity on shared home directories (lib/desktop_agent/config.py).

Hosts that share one ``desktop_agent.json`` (containers on a network home)
must never share a node ID *or* a credential; a host whose identity key
changed must keep its node when it is provably the same device (same
container renamed) and otherwise name its predecessor when it pairs again.
"""
import json
import sys
import threading
import types

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


def _on_platform(monkeypatch, platform, *, machine, hostname='fixture-host', container=''):
    """Run as a fixture host of ``platform``; never touches the real OS readers.

    Only the identity seams are substituted: ``_hostname_scoped`` follows the
    patched ``sys.platform``, so Windows/macOS hosts scope by machine id alone.
    """
    monkeypatch.setattr(host_identity.sys, 'platform', platform)
    monkeypatch.setattr(host_identity, '_machine_id', lambda: machine)
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


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_rename_keeps_the_node_and_its_attachments_on_windows_and_macos(
        shared_home, monkeypatch, platform):
    # These platforms scope the key by the platform machine id alone, so a
    # renamed computer is still the same node with the same joined servers.
    _on_platform(monkeypatch, platform, machine='fixture-machine-guid', hostname='old-name')
    node = config.node_identity()['node_id']
    key = host_identity.host_key()
    _attach('https://home.example', 'kept-secret')
    _on_platform(monkeypatch, platform, machine='fixture-machine-guid', hostname='new-name')
    assert host_identity.host_key() == key
    assert config.node_identity() == {'node_id': node, 'predecessor': None}
    assert [a['secret'] for a in config.attachments()] == ['kept-secret']
    # Restarting again, under either name, changes nothing.
    assert config.node_identity()['node_id'] == node
    _on_platform(monkeypatch, platform, machine='fixture-machine-guid', hostname='old-name')
    assert config.node_identity()['node_id'] == node
    assert [a['secret'] for a in config.attachments()] == ['kept-secret']
    # Only digests of host facts reach the shared config file.
    assert 'fixture-machine-guid' not in shared_home.read_text()


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_distinct_machines_do_not_inherit_secrets_on_windows_and_macos(
        shared_home, monkeypatch, platform):
    _on_platform(monkeypatch, platform, machine='machine-a', hostname='shared-host')
    first = config.node_identity()['node_id']
    _attach('https://home.example', 'secret-a')
    # Same hostname, different platform machine id: a different machine.
    _on_platform(monkeypatch, platform, machine='machine-b', hostname='shared-host')
    second = config.node_identity()['node_id']
    assert second != first
    assert config.attachments() == []
    _attach('https://home.example', 'secret-b')
    assert [a['secret'] for a in config.attachments()] == ['secret-b']
    _on_platform(monkeypatch, platform, machine='machine-a', hostname='shared-host')
    assert config.node_identity()['node_id'] == first
    assert [a['secret'] for a in config.attachments()] == ['secret-a']


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_a_missing_machine_identifier_falls_back_to_the_hostname(
        shared_home, monkeypatch, platform):
    # An unreadable platform id must not collapse every host to one node.
    _on_platform(monkeypatch, platform, machine='', hostname='laptop')
    facts = host_identity.host_facts()
    assert facts['machine'] == '' and facts['hostname']
    key = host_identity.host_key()
    node = config.node_identity()['node_id']
    _attach('https://home.example', 'fallback-secret')
    # A restart with the same hostname keeps the node.
    assert config.node_identity()['node_id'] == node
    # Another hostname is another host: a distinct key, node, and credential list.
    _on_platform(monkeypatch, platform, machine='', hostname='other-laptop')
    assert host_identity.host_key() != key
    assert config.node_identity()['node_id'] != node
    assert config.attachments() == []
    _on_platform(monkeypatch, platform, machine='', hostname='laptop')
    assert config.node_identity()['node_id'] == node
    assert [a['secret'] for a in config.attachments()] == ['fallback-secret']


def test_windows_machine_guid_is_read_through_the_registry_seam(monkeypatch):
    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(), OpenKey=lambda *a, **k: _Key(),
        QueryValueEx=lambda key, name: ('WIN-GUID-FIXTURE', 1))
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    monkeypatch.setattr(host_identity.sys, 'platform', 'win32')
    assert host_identity._machine_id() == 'WIN-GUID-FIXTURE'
    # An unreadable value is a missing id, never an exception.
    def _unreadable(key, name):
        raise FileNotFoundError(name)

    fake.QueryValueEx = _unreadable
    assert host_identity._machine_id() == ''


def test_macos_platform_uuid_is_read_through_the_ioreg_seam(monkeypatch):
    class _Out:
        stdout = '    "IOPlatformUUID" = "MAC-UUID-FIXTURE"\n'

    monkeypatch.setattr(host_identity.sys, 'platform', 'darwin')
    monkeypatch.setattr(host_identity.subprocess, 'run', lambda *a, **k: _Out())
    assert host_identity._machine_id() == 'MAC-UUID-FIXTURE'

    def _missing(*a, **k):
        raise FileNotFoundError('ioreg')

    monkeypatch.setattr(host_identity.subprocess, 'run', _missing)
    assert host_identity._machine_id() == ''


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
