"""Private loopback relay from the resident execution host to a bridge owner in the API host.

Problem: in the managed split (``TOFU_PROCESS_ROLE=api`` + ``execution``) the
browser extension and the desktop nodes long-poll the API process, so each
bridge's process-local registry and command queue live there, while task
tools and context composition run in the execution process, whose registries
are always empty.

Decision: the API process stays the single owner of each bridge (it terminates
the device polls). It publishes one private endpoint record per bridge beside
the manager's other process records (``<project>/data/<record>.json``, mode
0600, rotating bearer token); the execution process selects that owner and the
bridge facade forwards its owner-scoped verbs over it. This module is the
shared transport; each bridge declares its format, record name and method
table (``lib/browser/bridge_relay.py``, ``lib/desktop/bridge_relay.py``).

Alternatives considered: moving the registries into the execution host would
need the API to forward every poll and every status/panel read the other way;
a durable shared registry would turn transient liveness into stored state.

Protocol: one loopback connection per call; a network-order u32 byte length
precedes one UTF-8 JSON object, both directions. Request ``{format, token,
method, params}``; response ``{format, result, error}``. The caller sends
nothing after its request, so the owner treats EOF as the caller's
cancellation and hands the method a set cancellation event, exactly like a
local caller's stop. There is no retry: a lost response is reported as an
unreachable owner, never replayed.

Entry points: ``RelayServer``, ``RelayClient``, ``OwnerSelection`` (process
role -> owner/relay/local), ``RelayUnavailable``, ``is_cancelled``.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import struct
import threading
import time
from pathlib import Path

from lib.log import get_logger
from lib.tasks_pkg.generated_command_control import ACKNOWLEDGEMENT_SECONDS

logger = get_logger(__name__)

# ACKNOWLEDGEMENT_SECONDS: non-blocking verbs answer from in-memory state, so
# they reuse the existing private execution-control acknowledgement contract
# instead of a new timeout. Blocking verbs add their own declared wait.

_LENGTH = struct.Struct('!I')


class RelayUnavailable(RuntimeError):
    """The bridge owner (API process) gave no acknowledged answer."""


def _encode(value) -> bytes:
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return _LENGTH.pack(len(data)) + data


def _read_exact(connection, count, deadline=None, cancel_event=None,
                cancel_observation_seconds=None) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()
        wait = None
        if deadline is not None:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise TimeoutError('Bridge owner did not answer in time')
        if cancel_event is not None:
            wait = cancel_observation_seconds if wait is None else min(
                wait, cancel_observation_seconds)
        connection.settimeout(wait)
        try:
            chunk = connection.recv(count - len(chunks))
        except TimeoutError:
            continue
        if not chunk:
            raise OSError('Bridge relay connection closed')
        chunks.extend(chunk)
    return bytes(chunks)


class _Cancelled(Exception):
    pass


_CANCELLED = object()


def is_cancelled(value) -> bool:
    return value is _CANCELLED


# ── Owner side (API process) ────────────────────────────────────────────

class RelayServer:
    """Loopback listener owned by the API process; one thread per call.

    Calls borrow a thread for as long as the forwarded bridge command waits,
    exactly like the local tool thread they replace. Close withdraws
    admission, cancels entered calls and joins them.
    """

    def __init__(self, path: Path, *, format: str, methods: dict, name: str):
        self._path = Path(path)
        self._format = format
        self._name = name
        self._token = secrets.token_hex(32)
        self._methods = methods
        self._listener = None
        self._accept_thread = None
        self._lock = threading.Lock()
        self._calls: dict[threading.Thread, threading.Event] = {}
        self._closing = threading.Event()

    def start(self) -> dict:
        from lib.json_store import write_json_atomic
        if self._listener is not None:
            raise RuntimeError(f'{self._name} relay already has a lifecycle')
        self._listener = socket.create_server(('127.0.0.1', 0))
        port = self._listener.getsockname()[1]
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name=f'{self._name}-relay-accept', daemon=True)
        self._accept_thread.start()
        record = {'format': self._format, 'host': '127.0.0.1', 'port': port,
                  'pid': os.getpid(), 'token': self._token}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(str(self._path), record, mode=0o600)
        logger.info('[BridgeRelay] %s owner listening on 127.0.0.1:%d', self._name, port)
        return {'host': '127.0.0.1', 'port': port}

    def _accept_loop(self):
        while not self._closing.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            cancel = threading.Event()
            thread = threading.Thread(target=self._serve, args=(connection, cancel),
                                      name=f'{self._name}-relay-call', daemon=True)
            with self._lock:
                if self._closing.is_set():
                    connection.close()
                    return
                self._calls[thread] = cancel
            thread.start()

    def _serve(self, connection, cancel):
        watcher = None
        fmt = self._format
        try:
            deadline = time.monotonic() + ACKNOWLEDGEMENT_SECONDS
            length = _LENGTH.unpack(_read_exact(connection, _LENGTH.size, deadline))[0]
            request = json.loads(_read_exact(connection, length, deadline))
            if not (isinstance(request, dict) and request.get('format') == fmt
                    and isinstance(request.get('token'), str)
                    and secrets.compare_digest(request['token'], self._token)
                    and request.get('method') in self._methods
                    and isinstance(request.get('params'), dict)):
                response = {'format': fmt, 'result': None, 'error': 'relay_refused'}
            else:
                watcher = threading.Thread(target=self._watch_peer, args=(connection, cancel),
                                           name=f'{self._name}-relay-peer', daemon=True)
                watcher.start()
                try:
                    result = self._methods[request['method']](request['params'], cancel)
                    response = {'format': fmt, 'result': result, 'error': None}
                except (KeyError, TypeError, ValueError) as exc:
                    response = {'format': fmt, 'result': None,
                                'error': f'relay_bad_request: {type(exc).__name__}'}
            connection.settimeout(ACKNOWLEDGEMENT_SECONDS)
            connection.sendall(_encode(response))
        except (OSError, ValueError, TypeError, struct.error) as exc:
            logger.info('[BridgeRelay] %s call ended without a response: %s',
                        self._name, type(exc).__name__)
        except Exception:
            logger.exception('[BridgeRelay] %s call failed', self._name)
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
            if watcher is not None:
                watcher.join()
            with self._lock:
                self._calls.pop(threading.current_thread(), None)

    @staticmethod
    def _watch_peer(connection, cancel):
        # The caller writes nothing after its request: any readable byte, EOF
        # or error means it left, so its forwarded wait must be withdrawn.
        try:
            connection.settimeout(None)
            connection.recv(1)
        except OSError:
            pass
        cancel.set()

    def close(self, timeout: float | None = None) -> bool:
        self._closing.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            calls = dict(self._calls)
        for cancel in calls.values():
            cancel.set()
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in calls:
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if self._accept_thread is not None:
            self._accept_thread.join(
                None if deadline is None else max(0.0, deadline - time.monotonic()))
        try:
            current = json.loads(self._path.read_text(encoding='utf-8'))
            if current.get('token') == self._token:
                self._path.unlink()
        except (OSError, ValueError):
            pass
        return not any(thread.is_alive() for thread in calls)


# ── Caller side (execution process) ─────────────────────────────────────

class RelayClient:
    """Forwards one bridge verb per connection to the published bridge owner."""

    def __init__(self, path: Path, *, format: str, name: str,
                 cancel_observation_seconds: float):
        self._path = Path(path)
        self._format = format
        self._name = name
        self._cancel_observation_seconds = cancel_observation_seconds

    def _endpoint(self) -> dict:
        try:
            record = json.loads(self._path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise RelayUnavailable(
                f'The {self._name} owner (API process) has not published its relay') from exc
        except (OSError, ValueError) as exc:
            raise RelayUnavailable(f'The {self._name} relay record is unreadable') from exc
        if (not isinstance(record, dict) or record.get('format') != self._format
                or record.get('host') != '127.0.0.1'
                or type(record.get('port')) is not int or not 0 < record['port'] < 65536
                or not isinstance(record.get('token'), str)):
            raise RelayUnavailable(f'The {self._name} relay record is invalid')
        return record

    def call(self, method: str, params: dict, *, wait_seconds: float | None = 0.0,
             cancel_event=None):
        """Forward one verb; ``wait_seconds=None`` waits as long as the verb does.

        Returns the owner's result, or the cancellation sentinel
        (:func:`is_cancelled`) once ``cancel_event`` is set.
        """
        from tofu_db._loopback import create_connection
        endpoint = self._endpoint()
        deadline = (None if wait_seconds is None else
                    time.monotonic() + max(0.0, float(wait_seconds)) + ACKNOWLEDGEMENT_SECONDS)
        request = {'format': self._format, 'token': endpoint['token'],
                   'method': method, 'params': params}
        try:
            with create_connection((endpoint['host'], endpoint['port']),
                                   ACKNOWLEDGEMENT_SECONDS) as connection:
                connection.settimeout(ACKNOWLEDGEMENT_SECONDS)
                connection.sendall(_encode(request))
                length = _LENGTH.unpack(_read_exact(
                    connection, _LENGTH.size, deadline, cancel_event,
                    self._cancel_observation_seconds))[0]
                response = json.loads(_read_exact(
                    connection, length, deadline, cancel_event,
                    self._cancel_observation_seconds))
        except _Cancelled:
            return _CANCELLED
        except (OSError, ValueError, TypeError, struct.error) as exc:
            raise RelayUnavailable(
                f'The {self._name} owner (API process) is unreachable: {type(exc).__name__}'
            ) from exc
        if (not isinstance(response, dict) or response.get('format') != self._format
                or response.get('error') is not None):
            raise RelayUnavailable(
                f'The {self._name} owner refused the relay call: '
                f'{(response or {}).get("error") if isinstance(response, dict) else "malformed"}')
        return response.get('result')


class OwnerSelection:
    """One bridge's owner choice for this process, from its process role.

    ``api`` publishes the relay for the bridge it owns; ``execution`` and its
    shards forward to it; ``all`` (and embedders) keep the local in-process owner.
    """

    def __init__(self, *, name: str, record_path, make_server, make_client):
        self._name = name
        self._record_path = record_path
        self._make_server = make_server
        self._make_client = make_client
        self._lock = threading.Lock()
        self._client = None
        self._server = None

    def selected(self):
        return self._client

    def configure(self, *, process_role: str, environ) -> str:
        project = (environ.get('TOFU_PROJECT_PATH') or '').strip()
        with self._lock:
            if process_role == 'api':
                if not project:
                    raise RuntimeError(f'A separate API needs its explicit project to publish '
                                       f'the {self._name} relay')
                if self._server is None:
                    server = self._make_server(self._record_path(project))
                    server.start()
                    self._server = server
                return 'owner'
            if process_role in ('execution', 'execution_shard'):
                if not project:
                    raise RuntimeError(f'Resident execution needs its explicit project to '
                                       f'reach the {self._name} owner')
                self._client = self._make_client(self._record_path(project))
                return 'relay'
            return 'local'

    def stop(self, timeout: float = 2.0) -> bool:
        with self._lock:
            server, self._server = self._server, None
            self._client = None
        return True if server is None else server.close(timeout)
