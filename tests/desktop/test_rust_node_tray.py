"""``tofu node --tray`` on Linux: the dbusmenu permission toggle reaches the Home and the config.

Real processes: a private D-Bus session bus (``dbus-daemon``), the Python Home
(``live_server``) and the Rust node in tray mode. The test drives the tray the
way a StatusNotifier host does: it reads the menu with
``com.canonical.dbusmenu.GetLayout`` and clicks "Allow run commands / open
apps" with ``com.canonical.dbusmenu.Event``. The Home must then see the node
declare ``exec`` (effective once its own ceiling allows it), and
``~/.tofu/desktop_agent.json`` must carry the switch in the launcher's
``computer_control`` keys.

Tools: ``dbus-daemon`` (or ``TOFU_TEST_DBUS_DAEMON``) and ``gdbus`` (GLib; or
``TOFU_TEST_GDBUS``) on PATH; the test skips without them. To provision them
without root, extract Ubuntu 22.04 packages outside every repository, e.g.::

    mkdir -p ~/.cache/tofu-test-tools/dbus && cd ~/.cache/tofu-test-tools/dbus
    for p in main/d/dbus/dbus_1.12.20-2ubuntu4_amd64.deb \\
             main/g/glib2.0/libglib2.0-bin_2.72.1-1_amd64.deb; do
      curl -LO http://archive.ubuntu.com/ubuntu/pool/$p && dpkg-deb -x "$(basename $p)" .; done
    export PATH=$PWD/usr/bin:$PATH LD_LIBRARY_PATH=$PWD/usr/lib/x86_64-linux-gnu
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from tests.desktop.test_rust_node_interop import _api, _nodes, _stop, _wait
from tests.support.tofu_node import node_command

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling,
              pytest.mark.skipif(not sys.platform.startswith('linux'), reason='StatusNotifierItem is Linux')]

EXEC_LABEL = 'Allow run commands / open apps'  # desktop/_tk_theme.py desktop.tray.permExec (en)


def _tool(env_name: str, program: str) -> str:
    path = os.environ.get(env_name) or shutil.which(program) or ''
    if not path:
        pytest.skip(f'{program} not found (set {env_name} or put it on PATH; see module docstring)')
    return path


@pytest.fixture
def session_bus(tmp_path):
    daemon = _tool('TOFU_TEST_DBUS_DAEMON', 'dbus-daemon')
    socket = tmp_path / 'bus'
    proc = subprocess.Popen([daemon, '--session', f'--address=unix:path={socket}', '--nofork',
                             '--print-address'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    try:
        address = proc.stdout.readline().strip()
        assert address.startswith('unix:'), address
        yield address
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _gdbus(address: str, *args: str) -> str:
    done = subprocess.run([_tool('TOFU_TEST_GDBUS', 'gdbus'), 'call', '--session', *args],
                          env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _tray_name(address: str) -> str:
    names = _gdbus(address, '--dest', 'org.freedesktop.DBus', '--object-path', '/org/freedesktop/DBus',
                   '--method', 'org.freedesktop.DBus.ListNames')
    found = re.findall(r"'(org\.kde\.StatusNotifierItem-[^']+)'", names)
    return found[0] if found else ''


def _layout(address: str, name: str) -> str:
    return _gdbus(address, '--dest', name, '--object-path', '/MenuBar',
                  '--method', 'com.canonical.dbusmenu.GetLayout', '--', '0', '-1', '[]')


def _item(layout: str, label: str) -> tuple[int, str]:
    """(id, properties text) of the menu item labelled ``label``."""
    for match in re.finditer(r"\((\d+), \{([^{}]*)\}", layout):
        if f"'label': <'{label}'>" in match.group(2):
            return int(match.group(1)), match.group(2)
    raise AssertionError(f'{label!r} not in the menu: {layout[:2000]}')


def test_a_tray_permission_toggle_reaches_the_home_and_the_config(live_server, tmp_path, session_bus):
    from lib.desktop_wire.node_protocol import build_pairing_link, parse_pairing_link

    config = tmp_path / 'home' / 'desktop_agent.json'
    config.parent.mkdir()
    log = tmp_path / 'node.log'
    host = ('tray-machine', 'tray-host', '7' * 64)
    env = dict(os.environ, TOFU_DESKTOP_CONFIG=str(config), HOME=str(config.parent), NO_PROXY='*',
               no_proxy='*', DBUS_SESSION_BUS_ADDRESS=session_bus, TOFU_LANG='en')
    proc = node_id = None
    try:
        status, minted = _api(live_server, 'POST', f'/api/v4/desktop/pair-code?base={live_server}/', {})
        assert status == 201, minted
        link = build_pairing_link(live_server, parse_pairing_link(minted['link']).secret)
        with log.open('ab') as handle:
            proc = subprocess.Popen(node_command('--simulate-host', *host, '--tray', '--join', link,
                                                 '--poll-interval', '0.2'),
                                    env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        name = _wait(lambda: proc.poll() is None and _tray_name(session_bus), 'tray on the bus')
        assert proc.poll() is None, log.read_text()[-3000:]
        key = subprocess.run(node_command('--simulate-host', *host, '--host-key'), env=env,
                             capture_output=True, text=True, check=True).stdout.strip()
        node_id = json.loads(config.read_text())['agent_ids'][key]
        # Joining from the tray turned Computer Control on and persisted it, read-only.
        saved = json.loads(config.read_text())['computer_control']
        assert saved['enabled'] is True and saved['perms']['allow_exec'] is False

        status, body = _api(live_server, 'POST', f'/api/v4/desktop/nodes/{node_id}/confirm', {})
        assert status == 200, body
        status, snapshot = _api(live_server, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(live_server, 'PUT', f'/api/v4/devices/{node_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'exec': True}})
        assert status == 200, body
        _wait(lambda: (n := _nodes(live_server).get(node_id)) and n['online'], 'online')
        assert _nodes(live_server)[node_id]['tiers']['exec'] is False  # the device withholds exec

        # The server row shows the live link verdict (re-rendered on every status change).
        _wait(lambda: f"'label': <'{live_server} — connected'>" in _layout(session_bus, name), 'row connected')
        layout = _layout(session_bus, name)
        item, props = _item(layout, EXEC_LABEL)
        assert "'toggle-state': <0>" in props
        _gdbus(session_bus, '--dest', name, '--object-path', '/MenuBar',
               '--method', 'com.canonical.dbusmenu.Event', str(item), 'clicked', '<int32 0>', '0')

        _wait(lambda: _nodes(live_server)[node_id]['tiers']['exec'] is True, 'exec declared to the Home')
        saved = _wait(lambda: (cc := json.loads(config.read_text())['computer_control'])['perms']['allow_exec']
                      and cc, 'exec persisted')
        assert saved['enabled'] is True and saved['perms']['allow_write'] is False
        _wait(lambda: "'toggle-state': <1>" in _item(_layout(session_bus, name), EXEC_LABEL)[1],
              'menu shows exec on')
    finally:
        _stop(proc)
        if node_id:
            _api(live_server, 'DELETE', f'/api/v4/desktop/nodes/{node_id}')
        Path(log).exists() and print(log.read_text()[-2000:])
