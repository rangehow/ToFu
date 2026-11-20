"""Standard-library RFC 6455 client for the black-box push cases.

Speaks only the wire: one masked text frame per ``send_text``, and
``receive`` returns ``('text', str)``, ``('close', code)`` or
``('eof', None)``. Server pings are answered; fragmented messages are
reassembled. No server internals are imported.
"""

from __future__ import annotations

import base64
import os
import re
import socket
import ssl
import struct
from urllib.parse import urlsplit

OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class HandshakeRefused(Exception):
    def __init__(self, status: int):
        super().__init__(f'upgrade refused with HTTP {status}')
        self.status = status


class PushSocket:
    def __init__(self, base: str, path: str = '/api/push', headers=None, timeout: float = 60):
        target = urlsplit(base)
        secure = target.scheme == 'https'
        port = target.port or (443 if secure else 80)
        raw = socket.create_connection((target.hostname, port), timeout=timeout)
        self._socket = (ssl.create_default_context().wrap_socket(raw, server_hostname=target.hostname)
                        if secure else raw)
        self._buffer = b''
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        lines = [f'GET {path} HTTP/1.1', f'Host: {target.netloc}', 'Upgrade: websocket',
                 'Connection: Upgrade', f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13',
                 *(f'{name}: {value}' for name, value in (headers or {}).items())]
        self._socket.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
        head = self._read_until(b'\r\n\r\n')
        status_line = head.split(b'\r\n', 1)[0].decode('latin-1')
        match = re.match(r'HTTP/1\.[01] (\d{3})', status_line)
        assert match, f'no HTTP status line in upgrade reply: {status_line!r}'
        self.status = int(match.group(1))
        self.response_headers = {}
        for line in head.split(b'\r\n')[1:]:
            name, _, value = line.decode('latin-1').partition(':')
            if name:
                self.response_headers[name.strip().lower()] = value.strip()
        if self.status != 101:
            self._socket.close()
            raise HandshakeRefused(self.status)

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise ConnectionError('connection closed during the handshake')
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(marker)
        return head

    def _read_exact(self, count: int) -> bytes | None:
        while len(self._buffer) < count:
            chunk = self._socket.recv(65536)
            if not chunk:
                return None
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 1 << 16:
            header += bytes([0x80 | 126]) + struct.pack('!H', length)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode('utf-8'))

    def receive(self, timeout: float = 60):
        self._socket.settimeout(timeout)
        message = b''
        while True:
            head = self._read_exact(2)
            if head is None:
                return 'eof', None
            opcode, length = head[0] & 0x0F, head[1] & 0x7F
            if length == 126:
                length = struct.unpack('!H', self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read_exact(8))[0]
            payload = self._read_exact(length) if length else b''
            if payload is None:
                return 'eof', None
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else None
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                return 'close', code
            message += payload
            if head[0] & 0x80:
                return 'text', message.decode('utf-8')

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, struct.pack('!H', 1000))
        except OSError:
            pass
        self._socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
