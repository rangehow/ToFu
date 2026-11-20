"""The Rust storage.v2 client answers exactly like ``lib/storage/v2_client.py``.

Phase 3a of docs/RUST_REWRITE_EVIDENCE.md. One recorded command corpus runs
through the Python client and through ``packages/tofu-storage-client`` (its
``tofu-storage-client-diff`` driver), each against its own real
``tofu-db serve`` in an isolated data directory, behind a framing-aware
fault proxy that injects the same faults into both: disconnects before and
after dispatch, a response delayed past the socket deadline, and synthetic
busy/load-shedding answers. Real quota refusals come from the engine's own
bounds (a billing scan bound, a paper anchor bound, the swarm session
capacity). Results and classified errors must be identical.

Write-time credential redaction is compared the same way: the Rust
``CredentialRedactor`` reads the same vault (Fernet) and ``server_config.json``
as ``lib/tool_results/credential_redaction.py``, and a redacted command's
stored value is read back on both sides.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from lib.storage.errors import StorageError
from tests.support.tofudb_binary import ensure_storage_client_driver
from tests.tofudb.test_tofudb_differential import (
    _OWNER_ID,
    _TENANT_ID,
    _TEST_SECRET,
    _resolve_tofudb_binary,
)
from tofu_db import wire

# serial: builds/spawns the native engine and the Rust driver.
pytestmark = [pytest.mark.unit, pytest.mark.serial]

_ROOT = Path(__file__).resolve().parents[2]


def _contract_bound(table: str, field: str) -> int:
    contract = json.loads((_ROOT / 'contracts/tofudb_ir_v1.json').read_text(encoding='utf-8'))
    return next(entry[field] for entry in contract['logicalTables'] if entry['name'] == table)


# ── Real authority in an isolated data directory ────────────────────────
class Daemon:
    def __init__(self, binary: Path):
        self.data_dir = Path(tempfile.mkdtemp(prefix='tofudb-rust-client-'))
        env = dict(os.environ, TOFU_STORAGE_TOKEN=_TEST_SECRET.decode('ascii'))
        subprocess.run([str(binary), 'init', '--data-dir', str(self.data_dir / 'db')],
                       check=True, capture_output=True, text=True, timeout=120, env=env)
        self.process = subprocess.Popen(
            [str(binary), 'serve', '--data-dir', str(self.data_dir / 'db'),
             '--owner-id', str(_OWNER_ID), '--tenant-id', str(_TENANT_ID),
             '--search-projection-dir', str(self.data_dir / 'projection')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, text=True)
        readiness = self.process.stdout.readline().strip()
        if not readiness:
            self.close()
            raise RuntimeError('tofu-db serve exited before readiness')
        self.port = int(json.loads(readiness)['port'])

    def close(self) -> None:
        self.process.stdin.close()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.process.stdout.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)


# ── Framing-aware fault proxy ───────────────────────────────────────────
def _read_raw_frame(sock: socket.socket) -> tuple[bytes, dict[int, Any]] | None:
    head = b''
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    (length,) = struct.unpack('>I', head)
    rest = b''
    while len(rest) < length + 4:
        chunk = sock.recv(length + 4 - len(rest))
        if not chunk:
            return None
        rest += chunk
    return head + rest, wire.decode_message(rest[:length])


class FaultProxy:
    """Forward frames; apply the next planned action to each request by operation.

    Actions: ``pass``; ``close_before`` (drop the connection, request never
    dispatched); ``close_after`` (dispatch, discard the answer, drop the
    connection); ``("delay", seconds)``; ``("error", code, message, retryable)``
    (answer without dispatching).
    """

    def __init__(self, upstream_port: int):
        self.upstream_port = upstream_port
        self.plan: dict[str, list[Any]] = {}
        self.dispatched: list[str] = []
        self._lock = threading.Lock()
        self._listener = socket.create_server(('127.0.0.1', 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _next_action(self, operation: str) -> Any:
        with self._lock:
            actions = self.plan.get(operation)
            return actions.pop(0) if actions else 'pass'

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        upstream = socket.create_connection(('127.0.0.1', self.upstream_port))
        try:
            while True:
                frame = _read_raw_frame(client)
                if frame is None:
                    return
                raw, fields = frame
                kind = fields.get(wire.FIELD_KIND)
                if kind == wire.KIND_BLOB_CHUNK:
                    upstream.sendall(raw)
                    continue
                if kind != wire.KIND_REQUEST:
                    upstream.sendall(raw)
                    answer = _read_raw_frame(upstream)
                    if answer is None:
                        return
                    client.sendall(answer[0])
                    continue
                operation = fields.get(wire.FIELD_OPERATION)
                action = self._next_action(operation)
                if action == 'close_before':
                    return
                if isinstance(action, tuple) and action[0] == 'error':
                    _, code, message, retryable = action
                    client.sendall(wire.pack_frame(wire.encode_message({
                        wire.FIELD_KIND: wire.KIND_RESPONSE,
                        wire.FIELD_PROTOCOL_VERSION: wire.PROTOCOL_VERSION,
                        wire.FIELD_CORRELATION_ID: fields[wire.FIELD_CORRELATION_ID],
                        wire.FIELD_SCHEMA_ID: fields[wire.FIELD_SCHEMA_ID],
                        wire.FIELD_PAYLOAD: b'',
                        wire.FIELD_STATUS: 503,
                        wire.FIELD_ERROR_CODE: code,
                        wire.FIELD_ERROR_MESSAGE: message,
                        wire.FIELD_RETRYABLE: retryable,
                    })))
                    continue
                with self._lock:
                    self.dispatched.append(operation)
                upstream.sendall(raw)
                answer = []
                while True:
                    frame = _read_raw_frame(upstream)
                    if frame is None:
                        return
                    answer.append(frame[0])
                    if frame[1].get(wire.FIELD_KIND) == wire.KIND_RESPONSE:
                        break
                if action == 'close_after':
                    return
                if isinstance(action, tuple) and action[0] == 'delay':
                    time.sleep(action[1])
                client.sendall(b''.join(answer))
        except OSError:
            return
        finally:
            client.close()
            upstream.close()

    def close(self) -> None:
        self._listener.close()


# ── The two clients behind one command interface ─────────────────────────
def _outcome(call):
    try:
        return {'ok': True, 'value': call()}
    except StorageError as error:
        return {'ok': False, 'error': error.to_payload()}


class PythonDriver:
    def __init__(self, monkeypatch, config_dir: Path):
        self._client = None
        self._monkeypatch = monkeypatch
        self._config_dir = config_dir

    def __call__(self, command: dict[str, Any]) -> Any:
        from lib.storage.v2_client import StorageV2Client
        call = command['call']
        if call == 'open':
            self._client = StorageV2Client(
                command['host'], command['port'], command['secret'].encode('ascii'),
                owner_id=command['owner_id'], tenant_id=command.get('tenant_id'),
                timeout=command.get('timeout', 5.0), read_attempts=command.get('read_attempts', 3))
            return {'ok': True, 'value': None}
        storage = self._client
        deadline = command.get('deadline')
        payload = command.get('payload')
        if call == 'query':
            return _outcome(lambda: storage.query(command['operation'], payload, deadline))
        if call == 'command':
            return _outcome(lambda: storage.command(
                command['operation'], payload, command.get('command_id'), deadline=deadline))
        if call == 'maintenance':
            return _outcome(lambda: storage.maintenance(
                command['operation'], payload,
                **({'deadline': deadline} if deadline is not None else {})))
        if call == 'query_many':
            answers = storage.query_many([tuple(request) for request in command['requests']],
                                         deadline)
            return {'ok': True, 'value': [
                {'ok': False, 'error': answer.to_payload()} if isinstance(answer, StorageError)
                else {'ok': True, 'value': answer} for answer in answers]}
        if call == 'health':
            return {'ok': True, 'value': storage.health(
                **({'deadline': deadline} if deadline is not None else {}))}
        if call == 'metrics':
            return {'ok': True, 'value': storage.metrics(
                **({'deadline': deadline} if deadline is not None else {}))}
        if call in {'worker_watch', 'conversation_watch'}:
            deadline_ms = int(time.time() * 1000) + command['deadline_ms_from_now']
            return _outcome(lambda: getattr(storage, call)(
                cursor=command.get('cursor'), deadline_unix_ms=deadline_ms))
        if call == 'checkpoint_available':
            return _outcome(lambda: storage.native_checkpoint_available(user_id=command['user_id']))
        if call == 'set_redactor':
            return {'ok': True, 'value': None}  # Python's command() always redacts
        if call == 'close':
            storage.close()
            return {'ok': True, 'value': None}
        raise AssertionError(call)


class RustDriver:
    def __init__(self, binary: Path):
        self._process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def __call__(self, command: dict[str, Any]) -> Any:
        self._process.stdin.write(json.dumps(command) + '\n')
        self._process.stdin.flush()
        line = self._process.stdout.readline()
        assert line, 'tofu-storage-client-diff exited'
        return json.loads(line)

    def close(self) -> None:
        self._process.stdin.close()
        self._process.wait(timeout=30)
        self._process.stdout.close()


# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture(scope='module')
def binaries():
    engine = _resolve_tofudb_binary()
    driver = ensure_storage_client_driver(_ROOT)
    if driver is None:
        pytest.skip('no cargo to build packages/tofu-storage-client '
                    '(set TOFU_STORAGE_CLIENT_DIFF_BIN)')
    return engine, driver


_SECRET_VALUE = 'ghp_differentialSecret0123456789'
_PROVIDER_KEY = 'sk-provider key/with+symbols=='
# A recorded owner read for the corpus (hook and cache semantics); the real
# read on both sides is compared in test_rust_model_routing_read_matches_python.
_OWNER_KEYS = [['cred-routing', 'sk-routing-owner-key-0001'], ['cred-envelope', 'app id 密钥 42']]


@pytest.fixture
def credential_files(monkeypatch, tmp_path):
    """A vault and server_config.json under one config directory, for both sides."""
    from lib import credentials_vault
    from lib.tool_results import credential_redaction
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    monkeypatch.setattr(credentials_vault, '_STORE_PATH', config_dir / 'credentials_vault.json')
    monkeypatch.setattr(credentials_vault, '_KEY_PATH', config_dir / '.credentials_vault.key')
    monkeypatch.setattr(credentials_vault, '_fernet', None)
    monkeypatch.setattr(credential_redaction, '_server_config_path',
                        lambda: str(config_dir / 'server_config.json'))
    monkeypatch.setattr(credential_redaction, '_cached', {})
    monkeypatch.setattr(credential_redaction, '_failing', set())
    credentials_vault.set_entry('github_token', _SECRET_VALUE)
    credentials_vault.set_entry('npm-token.v2', '  密钥-npm-value-ok  ')
    credentials_vault.set_entry('short', 'tiny')
    (config_dir / 'server_config.json').write_text(json.dumps({
        'providers': [
            {'id': 'openai', 'api_key': _PROVIDER_KEY, 'api_keys': ['sk-second-key-123', 7]},
            {'name': 'oauthy', 'oauth': True, 'api_key': 'oauth-managed-value'},
            {'api_key': 'sk-unnamed-provider-key'},
        ],
        'mt_provider': {'api_key': 'mt-provider-secret'},
    }), encoding='utf-8')
    yield config_dir
    monkeypatch.setattr(credentials_vault, '_fernet', None)


@pytest.fixture
def credential_config(monkeypatch, credential_files):
    from lib.tool_results import credential_redaction
    monkeypatch.setattr(credential_redaction, '_model_routing_values', lambda owner: (
        (f'model-routing {credential_id}', key) for credential_id, key in _OWNER_KEYS))
    return credential_files


# ── The recorded corpus ──────────────────────────────────────────────────
def _corpus() -> list[dict[str, Any]]:
    scan_rows = _contract_bound('billing_wallets_and_ledger', 'maximumListScanRows')
    anchor_bytes = _contract_bound('paper_notes', 'maximumDocumentBytes')
    capacity = _contract_bound('swarm_sessions', 'maximumSessionsPerOwner')
    big = 'b' * (9 * 1024 * 1024)
    note = {'user_id': _OWNER_ID, 'id': 'note-1', 'paper_hash': 'paper', 'lang': 'en',
            'anchor': {'page': 1}, 'note': 'n', 'created_at': 1, 'updated_at': 1}
    steps: list[dict[str, Any]] = [
        {'call': 'query', 'operation': 'system.schema_version'},
        {'call': 'query', 'operation': 'conversation.count'},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'absent'}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'put-alpha',
         'payload': {'namespace': 'ns', 'key': 'alpha', 'value': {'n': 1, 'l': [True, None]}}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'put-alpha',
         'payload': {'namespace': 'ns', 'key': 'alpha', 'value': {'n': 1, 'l': [True, None]}}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'put-alpha',
         'payload': {'namespace': 'ns', 'key': 'alpha', 'value': 'different'}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'put-cas',
         'payload': {'namespace': 'ns', 'key': 'alpha', 'value': 2, 'expected_version': 9}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'alpha'}},
        {'call': 'query', 'operation': 'record.list', 'payload': {'namespace': 'ns'}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns'}},
        {'call': 'query', 'operation': 'nonexistent.operation'},
        {'call': 'query', 'operation': 'Bad Operation'},
        {'call': 'query', 'operation': 'a' * 150},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'c' * 300,
         'payload': {'namespace': 'ns', 'key': 'long-id', 'value': 1}},
        {'call': 'maintenance', 'operation': 'turn.cleanup', 'payload': {'limit': 0}},
        {'call': 'maintenance', 'operation': 'turn.cleanup', 'payload': {'limit': 10}},
        {'call': 'query_many', 'requests': [
            ['conversation.count', None], ['record.get', {'namespace': 'ns', 'key': 'alpha'}],
            ['nonexistent.operation', None], ['system.schema_version', {}]]},
        # A streamed upload (9 MiB) and a streamed response.
        {'call': 'command', 'operation': 'record.put', 'command_id': 'put-big',
         'payload': {'namespace': 'big', 'key': 'blob', 'value': big}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'big', 'key': 'blob'},
         'digest': True},
        # Real quotas: a deterministic per-request bound and a capacity quota.
        {'call': 'query', 'operation': 'billing.ledger.list',
         'payload': {'user_id': 'billing-user', 'limit': 1, 'kinds': [], 'offset': scan_rows}},
        {'call': 'command', 'operation': 'paper.note.create', 'command_id': 'note-big',
         'payload': dict(note, id='note-big', anchor={'text': 'x' * anchor_bytes})},
        {'call': 'command', 'operation': 'paper.note.create', 'command_id': 'note-ok',
         'payload': note},
        {'call': 'query', 'operation': 'paper.note.list',
         'payload': {'user_id': _OWNER_ID, 'paper_hash': 'paper', 'lang': 'en'}},
    ]
    for index in range(capacity + 1):
        steps.append({'call': 'command', 'operation': 'swarm.session.save',
                      'command_id': f'capacity-{index}', 'quiet': index < capacity,
                      'payload': {'swarm_key': f'capacity-{index}', 'specs': [{'id': 'a'}],
                                  'config': {'user_id': _OWNER_ID}, 'now_ms': 1_000 + index}})
    steps += [
        # Injected faults: the same plan on both sides.
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'alpha'},
         'faults': {'record.get': ['close_before']}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'alpha'},
         'faults': {'record.get': ['close_before'] * 3}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'lost-ack',
         'payload': {'namespace': 'ns', 'key': 'lost', 'value': 'committed once'},
         'faults': {'record.put': ['close_after']}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'lost'}},
        {'call': 'query', 'operation': 'conversation.count', 'deadline': 0.5,
         'faults': {'conversation.count': [('delay', 2.0)]}},
        {'call': 'query', 'operation': 'conversation.count', 'deadline': 0.5,
         'faults': {'conversation.count': [('delay', 2.0)] * 3}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'slow-ack', 'deadline': 0.5,
         'payload': {'namespace': 'ns', 'key': 'slow', 'value': 1},
         'faults': {'record.put': [('delay', 2.0)]}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'busy',
         'payload': {'namespace': 'ns', 'key': 'busy', 'value': 1},
         'faults': {'record.put': [('error', 'database_busy', 'busy', True)] * 2}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'shed',
         'payload': {'namespace': 'ns', 'key': 'shed', 'value': 1},
         'faults': {'record.put': [('error', 'database_resource_exhausted', 'shed', True)] * 3}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'shed'},
         'faults': {'record.get': [('error', 'database_resource_exhausted', 'quota', False)]}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'x'},
         'faults': {'record.get': [('error', 'deadline_elapsed', 'late', False)] * 3}},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'scope',
         'payload': {'namespace': 'ns', 'key': 'scope', 'value': 1},
         'faults': {'record.put': [('error', 'authority_scope_mismatch', 'scope', False)]}},
        {'call': 'query', 'operation': 'record.get', 'payload': {'namespace': 'ns', 'key': 'x'},
         'faults': {'record.get': [('error', 'invalid_request', 'engine-only code', False)]}},
        {'call': 'maintenance', 'operation': 'turn.cleanup', 'payload': {'limit': 3},
         'faults': {'turn.cleanup': ['close_before']}},
        # Write-time redaction on a command, read back.
        {'call': 'set_redactor'},
        {'call': 'command', 'operation': 'record.put', 'command_id': 'redacted',
         'payload': {'namespace': 'ns', 'key': 'redacted', 'value': {
             'text': f'Bearer {_SECRET_VALUE} and {_PROVIDER_KEY} via sk-routing-owner-key-0001',
             'nested': ['sk-second-key-123', 'tiny', {_SECRET_VALUE: 'key kept'}]}}},
        {'call': 'query', 'operation': 'record.get',
         'payload': {'namespace': 'ns', 'key': 'redacted'}},
        # Health, metrics, commit watches and checkpoint capability.
        {'call': 'health'},
        {'call': 'metrics'},
        {'call': 'worker_watch', 'cursor': None, 'deadline_ms_from_now': 300},
        {'call': 'conversation_watch', 'cursor': None, 'deadline_ms_from_now': 300},
        {'call': 'conversation_watch', 'cursor': None, 'deadline_ms_from_now': -5},
        {'call': 'checkpoint_available', 'user_id': _OWNER_ID},
        {'call': 'close'},
        {'call': 'query', 'operation': 'conversation.count'},
        {'call': 'health'},
    ]
    return steps


def _without_commit_clock(value: Any) -> Any:
    """Each authority stamps its own commit wall clock (``*_at_ms``)."""
    if isinstance(value, dict):
        return {key: '<commit clock>' if key.endswith('_at_ms') and type(item) is int
                else _without_commit_clock(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_commit_clock(item) for item in value]
    return value


def _normalized(step: dict[str, Any], outcome: Any) -> Any:
    """Erase only values that differ between two authorities by construction."""
    outcome = _without_commit_clock(outcome)
    if step.get('digest') and outcome.get('ok'):
        import hashlib
        outcome = dict(outcome, value=hashlib.sha256(
            json.dumps(outcome['value'], sort_keys=True).encode()).hexdigest())
    call = step['call']
    value = outcome.get('value')
    if call in {'health', 'metrics'} and isinstance(value, dict):
        # Live counters and per-authority identities; shape and verdict stay compared.
        stable = {'ready', 'backend', 'protocol', 'owner_id', 'tenant_id', 'probe_error'}
        outcome = dict(outcome, value={
            key: (item if key in stable else type(item).__name__) for key, item in value.items()})
    if call in {'worker_watch', 'conversation_watch'} and outcome.get('ok'):
        # The cursor epoch is each authority process's UUID.
        outcome = dict(outcome, value=json.loads(json.dumps(value, sort_keys=True)))
        cursor = outcome['value'].get('cursor') if isinstance(outcome['value'], dict) else None
        if isinstance(cursor, dict) and 'epoch' in cursor:
            cursor['epoch'] = '<epoch>'
    return outcome


def _run(driver, port: int, proxy: FaultProxy, corpus, *, config_dir: Path) -> list[Any]:
    outcomes = []
    # Python's command() always redacts, so the Rust client gets its redactor
    # at open; with none it refuses payload commands.
    opened = driver({'call': 'open', 'host': '127.0.0.1', 'port': port,
                     'secret': _TEST_SECRET.decode('ascii'), 'owner_id': _OWNER_ID,
                     'tenant_id': _TENANT_ID, 'timeout': 15.0, 'read_attempts': 3,
                     'config_dir': str(config_dir), 'owner_keys': _OWNER_KEYS})
    assert opened['ok'], opened
    for step in corpus:
        proxy.plan = {operation: list(actions) for operation, actions in step.get('faults', {}).items()}
        command = {key: value for key, value in step.items()
                   if key not in {'faults', 'quiet', 'digest'}}
        if command['call'] == 'set_redactor':
            command['config_dir'] = str(config_dir)
            command['owner_keys'] = _OWNER_KEYS
        outcome = driver(command)
        assert not any(proxy.plan.values()), (step, proxy.plan)
        if not step.get('quiet'):
            outcomes.append((step, _normalized(step, outcome)))
    return outcomes


def test_rust_client_matches_python_over_the_recorded_corpus(binaries, monkeypatch,
                                                           credential_config):
    engine, driver_binary = binaries
    corpus = _corpus()
    results = {}
    for side in ('python', 'rust'):
        daemon = Daemon(engine)
        proxy = FaultProxy(daemon.port)
        driver = (PythonDriver(monkeypatch, credential_config) if side == 'python'
                  else RustDriver(driver_binary))
        try:
            results[side] = _run(driver, proxy.port, proxy, corpus, config_dir=credential_config)
        finally:
            if side == 'rust':
                driver.close()
            proxy.close()
            daemon.close()
    mismatches = [
        (step, python, rust)
        for (step, python), (_, rust) in zip(results['python'], results['rust'])
        if python != rust
    ]
    report = [{'step': {key: value for key, value in step.items() if key != 'payload'},
               'python': python, 'rust': rust} for step, python, rust in mismatches]
    if report:
        pytest.fail(json.dumps(report, default=str, indent=1)[:40000], pytrace=False)
    # The corpus exercised what it claims: classified errors, retries and quotas.
    codes = {outcome['error']['code'] for _, outcome in results['python'] if not outcome['ok']}
    assert {'database_unavailable', 'database_timeout', 'database_resource_exhausted',
            'database_conflict', 'unknown_operation', 'database_protocol_error',
            'database_forbidden', 'database_internal'} <= codes
    messages = {outcome['error']['message'] for _, outcome in results['python'] if not outcome['ok']}
    assert {'storage.v2 authority closed the connection', 'storage.v2 authority is unavailable',
            'command_id was reused for a different request',
            'billing ledger list scan exceeds its row bound',
            'paper note anchor exceeds its byte bound',
            'database_resource_exhausted'} <= messages
    redacted = next(outcome for step, outcome in results['rust']
                    if step.get('payload') == {'namespace': 'ns', 'key': 'redacted'})
    assert '[redacted credential: GITHUB_TOKEN]' in json.dumps(redacted)
    assert '[redacted credential: model-routing cred-routing]' in json.dumps(redacted)
    assert _SECRET_VALUE not in json.dumps(redacted['value']['value']['text'])


def test_rust_redaction_matches_python_on_the_same_known_set(binaries, credential_config):
    from lib.tool_results.credential_redaction import redact_value
    _, driver_binary = binaries
    corpus = [
        f'token {_SECRET_VALUE} end',
        f'url ?key={_PROVIDER_KEY.replace(" ", "%20").replace("/", "%2F").replace("+", "%2B")}'
        f'&x=1',
        'plus form sk-provider+key%2Fwith%2Bsymbols%3D%3D',
        '密钥-npm-value-ok inside 中文',
        {'a': [f'x{_SECRET_VALUE}x', 'mt-provider-secret', 'sk-second-key-123', 'tiny']},
        {'oauth': 'oauth-managed-value', 'unnamed': 'sk-unnamed-provider-key', 'n': 3},
        f'{_SECRET_VALUE}{_SECRET_VALUE}',
        'nothing to redact here at all',
        '',
        'routing sk-routing-owner-key-0001 and app%20id%20%E5%AF%86%E9%92%A5%2042 and app+id+',
        # Overlapping occurrences: the longest wins, then the earliest.
        f'{_SECRET_VALUE[:-4]}sk-second-key-123{_SECRET_VALUE}',
    ]
    driver = RustDriver(driver_binary)
    try:
        for value in corpus:
            rust = driver({'call': 'redact', 'config_dir': str(credential_config), 'value': value})
            assert rust == {'ok': True, 'value': redact_value(value)}, value
            owned = driver({'call': 'redact', 'config_dir': str(credential_config), 'value': value,
                            'owner_id': _OWNER_ID, 'owner_keys': _OWNER_KEYS})
            assert owned == {'ok': True, 'value': redact_value(value, owner_user_id=_OWNER_ID)}, value
    finally:
        driver.close()


_MR_RAW_KEY = 'mr-only-appid-0123456789abcdef6672'
_MR_ENVELOPE_KEY = 'mr-envelope key/密钥+0733=='


def _model_routing_document():
    from tests.credentials_vault.test_model_routing_credential_redaction import _document
    document = _document()
    # A referenced secret that was never stored is skipped by both reads.
    document['credentials'].append(dict(document['credentials'][0], credential_id='cred-gone',
                                        secret_reference='secret-gone'))
    return document


def test_rust_model_routing_read_matches_python_for_an_owners_keys(
        binaries, monkeypatch, credential_files):
    """The real decrypting read on both sides, over one TofuDB authority.

    Python seals and commits an owner's model-routing credentials through
    ``ModelRoutingRepository``; the Rust ``ModelRoutingKnownSecrets`` reads
    them back through its own client and personal key file. Redaction,
    a written command's stored value, and the failure path (a secret whose
    binding no longer matches) must be identical.
    """
    import lib.config_dir as config_dir_module
    from lib import secret_envelope
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary, repository
    from lib.model_routing.change_notifications import mark_model_routing_secrets_changed
    from lib.model_routing.credential_envelope import ENVELOPE_FORMAT
    from lib.storage.v2_client import StorageV2Client
    from lib.tool_results import credential_redaction
    engine, driver_binary = binaries
    monkeypatch.setattr(config_dir_module, 'CONFIG_DIR', str(credential_files))
    monkeypatch.delenv('TOFU_SECRET_ENCRYPTION_KEY', raising=False)
    monkeypatch.setattr(secret_envelope, '_KEY_PATH', credential_files / '.secret_encryption.key')
    monkeypatch.setattr(secret_envelope, '_cached_fernet', None)
    monkeypatch.setattr(secret_envelope, '_cached_key_fingerprint', '')
    monkeypatch.setattr(secret_envelope, '_cached_personal_key', None)

    daemon = Daemon(engine)
    python = StorageV2Client('127.0.0.1', daemon.port, _TEST_SECRET, owner_id=_OWNER_ID,
                             tenant_id=_TENANT_ID, timeout=15.0)
    driver = RustDriver(driver_binary)
    try:
        class Repository(ModelRoutingRepository):
            def _client(self, *, write=False):
                return python

        monkeypatch.setattr('lib.model_routing.known_secrets.ModelRoutingRepository', Repository)
        boundary = OwnerBoundary.create(_OWNER_ID)
        Repository().put_secret(boundary, _MR_RAW_KEY, secret_reference='secret-raw')
        Repository().put_secret(boundary, json.dumps({
            'format': ENVELOPE_FORMAT, 'api_key': _MR_ENVELOPE_KEY, 'extra_headers': {}}),
            secret_reference='secret-envelope')
        Repository().compare_and_swap(boundary, _model_routing_document(), expected_revision=0)

        opened = driver({'call': 'open', 'host': '127.0.0.1', 'port': daemon.port,
                         'secret': _TEST_SECRET.decode('ascii'), 'owner_id': _OWNER_ID,
                         'tenant_id': _TENANT_ID, 'timeout': 15.0, 'read_attempts': 3})
        assert opened['ok'], opened
        unredacted = driver({'call': 'command', 'operation': 'record.put', 'command_id': 'raw',
                             'payload': {'namespace': 'mr', 'key': 'raw', 'value': 'x'}})
        assert unredacted['error']['message'] == (
            'storage.v2 command refused: no write redactor is installed')
        assert driver({'call': 'install_credential_redaction',
                       'config_dir': str(credential_files)})['ok']

        encoded = _MR_ENVELOPE_KEY.replace(' ', '%20').replace('/', '%2F').replace('+', '%2B')
        corpus = [
            f'FRIDAY_APPID={_MR_RAW_KEY}',
            f'Authorization: Bearer {_MR_ENVELOPE_KEY}',
            f'?key={encoded}&x=1',
            {'nested': [_MR_RAW_KEY, {'t': f'{_SECRET_VALUE} {_MR_ENVELOPE_KEY}'}]},
            f'{_MR_RAW_KEY[:-4]}{_MR_ENVELOPE_KEY}{_MR_RAW_KEY}',
            'nothing to redact',
            # Known only if a read wrongly accepted the unbound rotation below.
            'rotated mr-rotated-0123456789abcdef',
        ]

        def compare(expect_owner_keys: bool) -> None:
            for value in corpus:
                expected = credential_redaction.redact_value(value, owner_user_id=_OWNER_ID)
                rust = driver({'call': 'redact_installed', 'owner_id': _OWNER_ID, 'value': value})
                assert rust == {'ok': True, 'value': expected}, value
            redacted = json.dumps(driver({'call': 'redact_installed', 'owner_id': _OWNER_ID,
                                          'value': corpus[:3]}))
            assert ('[redacted credential: model-routing cred-raw]' in redacted
                    and '[redacted credential: model-routing cred-envelope]' in redacted
                    ) is expect_owner_keys
            assert (_MR_RAW_KEY in redacted) is not expect_owner_keys

        compare(expect_owner_keys=True)
        # Without an owner only the owner-independent sources apply.
        assert driver({'call': 'redact_installed', 'value': _MR_RAW_KEY}) == {
            'ok': True, 'value': credential_redaction.redact_value(_MR_RAW_KEY)}

        # A written command's stored value is identical.
        text = f'echo {_MR_RAW_KEY}; Bearer {_MR_ENVELOPE_KEY}; {_SECRET_VALUE}'
        python.command('record.put', {'namespace': 'mr', 'key': 'py', 'value': text}, 'mr-py')
        written = driver({'call': 'command', 'operation': 'record.put', 'command_id': 'mr-rs',
                          'payload': {'namespace': 'mr', 'key': 'rs', 'value': text}})
        assert written['ok'], written
        stored = [python.query('record.get', {'namespace': 'mr', 'key': key})['value']
                  for key in ('py', 'rs')]
        assert stored[0] == stored[1]
        assert _MR_RAW_KEY not in stored[1] and _MR_ENVELOPE_KEY not in stored[1]

        # A secret whose binding no longer opens fails the whole owner read on
        # both sides once the stamp moves; both keep the previous set.
        python.command('model_routing.secret.put', {
            **boundary.payload(), 'secret_reference': 'secret-raw',
            'ciphertext': secret_envelope.seal_secret(
                'mr-rotated-0123456789abcdef', purpose=repository._SECRET_PURPOSE,
                owner_user_id=_OWNER_ID, record_id='another-record'),
            'key_hint': '***', 'updated_at': time.time()}, 'mr-unbound')
        mark_model_routing_secrets_changed(user_id=_OWNER_ID)
        compare(expect_owner_keys=True)
    finally:
        driver.close()
        python.close()
        daemon.close()


def test_rust_contract_projection_matches_python(binaries):
    from lib.agent_core.storage_errors import ERROR_CODES
    _, driver_binary = binaries
    driver = RustDriver(driver_binary)
    try:
        contract = driver({'call': 'contract'})['value']
    finally:
        driver.close()
    assert set(contract['error_codes']) == set(ERROR_CODES)
    catalog = json.loads((_ROOT / 'contracts/storage_operations_v1.json').read_text())
    storage_v2 = json.loads((_ROOT / 'contracts/storage_v2.json').read_text())
    assert set(contract['operations']) == (
        {entry['name'] for entry in catalog['operations']}
        | set(storage_v2['controlOperations']['operations']))
