"""The Rust node's GUI commands on the Python Home, against a real X server.

``desktop_screenshot``, ``desktop_gui_action`` and ``desktop_clipboard`` run in
the Rust node (packages/tofu-node, ``src/commands/gui*``) on a private Xvfb
display; the Home sends them through ``lib.desktop.bridge`` after confirming
the node and raising its ceiling to ``gui``. What the node did is checked from
a second, independent X client (a minimal X11 protocol client in this module,
no Xlib needed): the pointer position, the key and button events delivered to
the root window, and the CLIPBOARD selection the node owns and serves. A node
started without ``DISPLAY`` answers a typed ``gui_unavailable`` error.

Xvfb is found through ``TOFU_TEST_XVFB`` (path of the binary) or ``Xvfb`` on
PATH; the tests skip without it. It is started with its own directory as the
working directory and ``<root>/usr/lib/x86_64-linux-gnu`` (when present) on
LD_LIBRARY_PATH, so an unpacked copy works without root. Provisioning on an
Ubuntu 22.04 host without root::

    D=~/.cache/tofu-test-tools/xvfb; mkdir -p $D/debs && cd $D/debs
    B=http://archive.ubuntu.com/ubuntu/pool
    curl -O $B/universe/x/xorg-server/xvfb_21.1.4-2ubuntu1.7~22.04.16_amd64.deb
    curl -O $B/main/x/x11-xkb-utils/x11-xkb-utils_7.7+5build4_amd64.deb
    curl -O $B/main/libx/libxfont/libxfont2_2.0.5-1build1_amd64.deb
    for f in *.deb; do dpkg-deb -x $f $D/root; done
    # Xvfb runs /usr/bin/xkbcomp; point it at the xkbcomp beside it instead:
    python3 -c "import sys; p=sys.argv[1]; b=open(p,'rb').read(); \
    o=b'-ar2\\0/usr/bin\\0'; assert b.count(o)==1; \
    open(p,'wb').write(b.replace(o, b'-ar2\\0.' + bytes(8)))" $D/root/usr/bin/Xvfb
    export TOFU_TEST_XVFB=$D/root/usr/bin/Xvfb

(xkb-data must be installed, as it is on stock Ubuntu.)
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from tests.support.tofu_node import node_command

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling]

OWNER = '1'
SCREEN = (1920, 1080)


# ── a minimal, independent X11 client ────────────────────────────────────────

class _XClient:
    """Just enough of the X11 core protocol to observe what the node did."""

    KEY_PRESS, BUTTON_PRESS, SELECTION_NOTIFY = 2, 4, 31

    def __init__(self, display: str):
        number = display.split(':', 1)[1].split('.', 1)[0]
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(f'/tmp/.X11-unix/X{number}')
        self.sock.sendall(struct.pack('<BxHHHHxx', 0x6C, 11, 0, 0, 0))
        head = self._read(8)
        if head[0] != 1:
            raise RuntimeError('X connection refused')
        body = self._read(struct.unpack_from('<H', head, 6)[0] * 4)
        self.id_base, self.id_mask = struct.unpack_from('<II', body, 4)
        vendor_len, = struct.unpack_from('<H', body, 16)
        formats, = struct.unpack_from('<B', body, 21)
        self.min_keycode, self.max_keycode = struct.unpack_from('<BB', body, 26)
        screen = 32 + (vendor_len + 3) // 4 * 4 + formats * 8
        self.root, = struct.unpack_from('<I', body, screen)
        self.next_id = 0
        self.seq = 0
        self.events: list[bytes] = []

    def _read(self, n: int) -> bytes:
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError('X connection closed')
            data += chunk
        return data

    def _packet(self) -> bytes:
        packet = self._read(32)
        if packet[0] == 1:
            packet += self._read(struct.unpack_from('<I', packet, 4)[0] * 4)
        return packet

    def request(self, opcode: int, data: int, body: bytes = b'', reply: bool = False) -> bytes | None:
        body += b'\0' * (-len(body) % 4)
        self.sock.sendall(struct.pack('<BBH', opcode, data, 1 + len(body) // 4) + body)
        self.seq = (self.seq + 1) & 0xFFFF
        if not reply:
            return None
        while True:
            packet = self._packet()
            if packet[0] == 0:
                raise RuntimeError(f'X error {packet[1]} for opcode {opcode}')
            if packet[0] == 1 and struct.unpack_from('<H', packet, 2)[0] == self.seq:
                return packet
            self.events.append(packet)

    def new_id(self) -> int:
        self.next_id += 1
        return self.id_base | (self.next_id & self.id_mask)

    def pointer(self) -> tuple[int, int]:
        reply = self.request(38, 0, struct.pack('<I', self.root), reply=True)
        return struct.unpack_from('<hh', reply, 16)

    def intern(self, name: str) -> int:
        raw = name.encode()
        reply = self.request(16, 0, struct.pack('<HH', len(raw), 0) + raw, reply=True)
        return struct.unpack_from('<I', reply, 8)[0]

    def keymap(self) -> tuple[int, list[int]]:
        count = self.max_keycode - self.min_keycode + 1
        reply = self.request(101, 0, struct.pack('<BBxx', self.min_keycode, count), reply=True)
        per = reply[1]
        return per, list(struct.unpack_from(f'<{count * per}I', reply, 32))

    def watch_root(self) -> None:
        """Receive key and button presses delivered to the root window."""
        key_press, button_press = 1 << 0, 1 << 2
        self.request(2, 0, struct.pack('<III', self.root, 1 << 11, key_press | button_press))
        self.request(43, 0, b'', reply=True)  # GetInputFocus: a round trip

    def wait_events(self, code: int, count: int, timeout: float = 10.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while sum(1 for e in self.events if e[0] & 0x7F == code) < count:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.sock], [], [], left)[0]:
                break
            packet = self._packet()
            if packet[0] == 0:
                raise RuntimeError(f'X error {packet[1]}')
            self.events.append(packet)
        return [e for e in self.events if e[0] & 0x7F == code]

    def clipboard(self) -> tuple[int, str | None]:
        """(owner window, text) of CLIPBOARD, converted to UTF8_STRING by its owner."""
        selection, target, prop = self.intern('CLIPBOARD'), self.intern('UTF8_STRING'), self.intern('TOFU_TEST')
        owner = struct.unpack_from('<I', self.request(23, 0, struct.pack('<I', selection), reply=True), 8)[0]
        if not owner:
            return 0, None
        self.events = [e for e in self.events if e[0] & 0x7F != self.SELECTION_NOTIFY]
        window = self.new_id()
        self.request(1, 0, struct.pack('<IIhhHHHHII', window, self.root, 0, 0, 1, 1, 0, 2, 0, 0))
        self.request(24, 0, struct.pack('<IIIII', window, selection, target, prop, 0))
        notify = self.wait_events(self.SELECTION_NOTIFY, 1)
        assert notify, 'the selection owner never answered'
        if struct.unpack_from('<I', notify[-1], 20)[0] == 0:
            return owner, None
        reply = self.request(20, 1, struct.pack('<IIIII', window, prop, 0, 0, 1 << 20), reply=True)
        length = struct.unpack_from('<I', reply, 16)[0]
        return owner, reply[32:32 + length].decode()

    def close(self) -> None:
        self.sock.close()


def _typed_text(client: _XClient, presses: list[bytes]) -> str:
    per, syms = client.keymap()
    text = ''
    for event in presses:
        keycode, state = event[1], struct.unpack_from('<H', event, 28)[0]
        row = syms[(keycode - client.min_keycode) * per:][:per]
        keysym = row[1] if state & 1 and per > 1 and row[1] else row[0]
        if 0x20 <= keysym <= 0x7E:
            text += chr(keysym)
    return text


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def xvfb_display():
    binary = os.environ.get('TOFU_TEST_XVFB') or shutil.which('Xvfb')
    if not binary or not os.access(binary, os.X_OK):
        pytest.skip('Xvfb not found: set TOFU_TEST_XVFB or put Xvfb on PATH (see module docstring)')
    bindir = Path(binary).resolve().parent
    env = dict(os.environ)
    libdir = bindir.parent / 'lib' / 'x86_64-linux-gnu'
    if libdir.is_dir():
        env['LD_LIBRARY_PATH'] = os.pathsep.join(p for p in (str(libdir), env.get('LD_LIBRARY_PATH')) if p)
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [binary, '-displayfd', str(write_fd), '-screen', '0', f'{SCREEN[0]}x{SCREEN[1]}x24',
         '-nolisten', 'tcp', '-ac'],
        cwd=str(bindir), env=env, pass_fds=(write_fd,), stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, start_new_session=True)
    os.close(write_fd)
    number = b''
    try:
        while not number.endswith(b'\n'):
            if not select.select([read_fd], [], [], 30)[0]:
                break
            chunk = os.read(read_fd, 16)
            if not chunk:
                break
            number += chunk
        if not number.strip():
            proc.kill()
            pytest.fail('Xvfb did not start: ' + proc.communicate()[1].decode(errors='replace')[-2000:])
        yield f':{number.decode().strip()}'
    finally:
        os.close(read_fd)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


# ── the Home side ────────────────────────────────────────────────────────────

def _api(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b'{}')
            return response.status, payload.get('data', payload) if isinstance(payload, dict) else payload
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors='replace')


def _nodes(base: str) -> dict:
    status, body = _api(base, 'GET', '/api/v4/desktop/nodes')
    assert status == 200, body
    return {n['node_id']: n for n in body['nodes'] if n['node_id']}


def _wait(predicate, what: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f'never {what}')


def _join_gui_node(base: str, tmp_path: Path, name: str, display: str | None):
    """A confirmed Rust node with ``--allow-gui`` and a Home ceiling raised to gui."""
    config = tmp_path / name / 'desktop_agent.json'
    config.parent.mkdir()
    log = tmp_path / f'{name}.log'
    host = (f'machine-{name}', f'host-{name}', name[0] * 64)
    status, minted = _api(base, 'POST', f'/api/v4/desktop/pair-code?base={base}/', {})
    assert status == 201, minted
    # Keep only the test Home's own route: the minted link's LAN ``via`` route names the
    # inherited PORT (the production Tofu), which a test must never reach.
    from lib.desktop_wire.node_protocol import build_pairing_link, parse_pairing_link
    link = build_pairing_link(base, parse_pairing_link(minted['link']).secret)
    env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY')}
    env.update(TOFU_DESKTOP_CONFIG=str(config), HOME=str(config.parent), NO_PROXY='*', no_proxy='*')
    if display:
        env['DISPLAY'] = display
    with log.open('ab') as handle:
        proc = subprocess.Popen(
            node_command('--simulate-host', *host, '--join', link, '--allow-gui', '--poll-interval', '0.2'),
            env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        _wait(lambda: 'fingerprint' in log.read_text() or proc.poll() is not None, 'joined')
        assert proc.poll() is None, log.read_text()[-2000:]
        key = subprocess.run(node_command('--simulate-host', *host, '--host-key'), capture_output=True,
                             text=True, check=True).stdout.strip()
        node_id = json.loads(config.read_text())['agent_ids'][key]
        _wait(lambda: _nodes(base).get(node_id), 'listed')
        status, body = _api(base, 'POST', f'/api/v4/desktop/nodes/{node_id}/confirm', {})
        assert status == 200, body
        status, snapshot = _api(base, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(base, 'PUT', f'/api/v4/devices/{node_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'gui': True}})
        assert status == 200, body
        _wait(lambda: _nodes(base)[node_id]['online'] and _nodes(base)[node_id]['tiers']['gui'], 'gui effective')
    except BaseException:
        _stop(proc)
        raise
    return proc, node_id


def _stop(proc) -> None:
    if proc is not None and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _cleanup(base: str) -> None:
    from lib.desktop import bridge as db
    for node_id in _nodes(base):
        _api(base, 'DELETE', f'/api/v4/desktop/nodes/{node_id}')
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()


def _send(node_id: str, cmd_type: str, params: dict) -> dict:
    """The node's result object; a failed command as {'error': ...} like the node reports it."""
    from lib.desktop.bridge import send_desktop_command
    result, error = send_desktop_command(cmd_type, params, timeout=60, target_agent_id=node_id, user_id=OWNER)
    return {'error': error} if error is not None else result


# ── tests ────────────────────────────────────────────────────────────────────

def test_rust_node_drives_a_real_x_display(live_server, tmp_path, xvfb_display):
    from PIL import Image

    proc = None
    observer = _XClient(xvfb_display)
    try:
        proc, node_id = _join_gui_node(live_server, tmp_path, 'gui', xvfb_display)

        shot = _send(node_id, 'desktop_screenshot', {})
        assert 'error' not in shot, shot
        assert (shot['real_width'], shot['real_height']) == SCREEN
        assert (shot['width'], shot['height']) == (1024, 576)
        assert shot['scale'] == 0.533333 and shot['format'] == 'jpeg'
        jpeg = base64.b64decode(shot['base64'])
        assert shot['size_bytes'] == len(jpeg)
        with Image.open(io.BytesIO(jpeg)) as image:
            assert image.format == 'JPEG' and image.size == (1024, 576)
        region = _send(node_id, 'desktop_screenshot', {'region': [100, 100, 300, 200]})
        assert (region['width'], region['height'], region['scale']) == (300, 200, 1.0)

        # Model coordinates on the 1024x576 image land on real pixels.
        moved = _send(node_id, 'desktop_gui_action', {'action': 'moveto', 'x': 512, 'y': 288, 'duration': 0})
        assert moved == {'action': 'moveto', 'x': 960, 'y': 540, 'success': True}
        assert observer.pointer() == (960, 540)
        tweened = _send(node_id, 'desktop_gui_action', {'action': 'moveto', 'x': 300, 'y': 200, 'scale': 1})
        assert tweened['success'] is True and observer.pointer() == (300, 200)

        observer.watch_root()
        clicked = _send(node_id, 'desktop_gui_action', {'action': 'click', 'x': 100, 'y': 50, 'button': 'right'})
        assert clicked == {'action': 'click', 'x': 188, 'y': 94, 'success': True}
        assert observer.pointer() == (188, 94)
        presses = observer.wait_events(_XClient.BUTTON_PRESS, 1)
        assert [(p[1], struct.unpack_from('<hh', p, 20)) for p in presses] == [(3, (188, 94))]

        typed = _send(node_id, 'desktop_gui_action', {'action': 'type', 'text': 'Hi, Tofu!', 'interval': 0})
        assert typed == {'action': 'type', 'chars': 9, 'success': True}
        keys = observer.wait_events(_XClient.KEY_PRESS, 12)  # 9 characters and Shift for H, T, !
        assert _typed_text(observer, keys) == 'Hi, Tofu!'
        per, before = observer.keymap()
        unicode = _send(node_id, 'desktop_gui_action', {'action': 'type', 'text': 'é中✓'})
        assert unicode == {'action': 'type', 'chars': 3, 'success': True}
        assert observer.keymap() == (per, before)  # spare keycodes restored
        hotkey = _send(node_id, 'desktop_gui_action', {'action': 'hotkey', 'keys': ['ctrl', 'shift', 'esc']})
        assert hotkey == {'action': 'hotkey', 'keys': ['ctrl', 'shift', 'esc'], 'success': True}
        unknown = _send(node_id, 'desktop_gui_action', {'action': 'hotkey', 'keys': ['hyperdrive']})
        assert unknown['error'] == 'Unknown key: hyperdrive'

        # The node owns CLIPBOARD and keeps serving it to other clients.
        text = 'tofu clipboard ✓ 剪贴板'
        written = _send(node_id, 'desktop_clipboard', {'action': 'write', 'content': text})
        assert written == {'success': True, 'written': len(text)}
        assert _send(node_id, 'desktop_clipboard', {'action': 'read'}) == {'content': text}
        owner, served = observer.clipboard()
        assert owner and served == text
        time.sleep(1)
        assert observer.clipboard()[1] == text
        replaced = _send(node_id, 'desktop_clipboard', {'action': 'write', 'content': 'second'})
        assert replaced['written'] == 6 and observer.clipboard()[1] == 'second'
    finally:
        observer.close()
        _stop(proc)
        _cleanup(live_server)


def test_rust_node_without_a_display_answers_a_typed_error(live_server, tmp_path):
    proc = None
    try:
        proc, node_id = _join_gui_node(live_server, tmp_path, 'headless', None)
        for cmd_type, params in (('desktop_screenshot', {}),
                                 ('desktop_gui_action', {'action': 'click', 'x': 1, 'y': 1}),
                                 ('desktop_clipboard', {'action': 'read'})):
            result = _send(node_id, cmd_type, params)
            assert result == {'error': 'gui_unavailable: no graphical session: DISPLAY is not set'}, result
        assert proc.poll() is None  # no panic: the node keeps serving
        unknown = _send(node_id, 'desktop_gui_action', {'action': 'teleport'})
        assert unknown == {'error': 'Unknown GUI action: teleport'}
    finally:
        _stop(proc)
        _cleanup(live_server)
