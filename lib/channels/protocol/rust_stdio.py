"""Core side of the stdio binding through the Rust link (packages/tofu-channel-stdio).

Responsibility: the same interface as ``StdioLink`` (``bind``, ``request``,
``returncode``, ``close(timeout)``, ``stderr_tail``) for ``AdapterHost``, with
the adapter child owned by the Rust ``tofu-channel-stdio-link`` binary
(``TOFU_CHANNEL_STDIO_LINK_BIN``). The binary runs in its own session with the
given environment and directory, which the adapter inherits; this side speaks
JSON-RPC lines with it through ``stdio._Peer``. Requests are forwarded to the
adapter with their errors intact; the adapter's ``events`` and ``health`` reach
the bound host here. Link controls start with ``$/``: ``$/close`` carries the
close timeout before stdin closes, ``$/status`` (adapter exit) and ``$/exit``
(link exit) report the adapter's return code and stderr tail, so
``returncode`` and ``stderr_tail`` update at those points only.

Entry point: ``RustStdioLink``. Dependencies: ``stdio._Peer``, ``contract``.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import Any

from lib.channels.protocol.contract import ERRORS, METHODS, ProtocolError
from lib.channels.protocol.stdio import _Peer

LINK_BIN_ENV = 'TOFU_CHANNEL_STDIO_LINK_BIN'


class RustStdioLink:
    """Core side of the stdio binding: one adapter child behind the Rust link binary."""

    def __init__(self, argv: list[str], *, env: Mapping[str, str] | None = None, cwd: str | None = None):
        binary = os.environ.get(LINK_BIN_ENV)
        if not binary:
            raise RuntimeError(f'{LINK_BIN_ENV} names no tofu-channel-stdio-link binary')
        self._proc = subprocess.Popen([os.path.abspath(binary), '--', *argv], cwd=cwd,
                                      env=dict(env) if env is not None else None,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
        self._host = None
        self._status: dict[str, Any] = {'returncode': None, 'stderrTail': ''}
        self._exited = False
        self._peer = _Peer(self._proc.stdout, self._proc.stdin, self._handle, name='channel-adapter-rust-link')
        self._peer.start()

    def bind(self, host) -> None:
        self._host = host

    def _handle(self, method: str, params: dict[str, Any]) -> Any:
        if method in ('$/status', '$/exit'):
            self._status = {'returncode': params.get('returncode'), 'stderrTail': str(params.get('stderrTail') or '')}
            self._exited = self._exited or method == '$/exit'
            return None
        spec = METHODS.get(method)
        if spec is None or spec['direction'] != 'adapter_to_core':
            raise ProtocolError(ERRORS['methodNotFound'], f'The core has no method {method!r}')
        if self._host is None:
            raise ProtocolError(ERRORS['notInitialized'], 'No core endpoint is bound')
        result = getattr(self._host, method)(params)
        return result if spec['kind'] == 'request' else None

    def request(self, method: str, params: dict[str, Any]) -> Any:
        return self._peer.request(method, params)

    @property
    def returncode(self) -> int | None:
        return self._status['returncode']

    @property
    def stderr_tail(self) -> str:
        return self._status['stderrTail']

    def close(self, timeout: float) -> bool:
        """Hand the timeout to the link and close its stdin; it closes and reaps the adapter, then exits."""
        try:
            self._peer.notify('$/close', {'timeout': timeout})
        except ProtocolError:
            pass  # the link is gone; its exit is reaped below
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.wait()  # the link escalates to SIGKILL of the adapter group, so it always ends
        self._peer.join(timeout)
        return self._exited and self._status['returncode'] is not None


__all__ = ['RustStdioLink']
