"""Differential gate: the Rust SQL authority vs the Python SQL authority.

Rust rewrite P3b (``docs/RUST_REWRITE_EVIDENCE.md``) ports
``lib/storage_authority_sql`` to ``packages/tofu-sql-authority``. Both serve
the same ``storage.v1`` contract over the same SQLite schema, so this gate
can demand more than the Tofu-DB differential: for every operation script in
``tests/tofudb/test_tofudb_differential.py`` (the recorded corpus, ~400
scripts across the catalog) it runs

* the Python authority (``python -m lib.storage_authority_sql``) and
* the Rust authority (``tofu-sql-authority serve``),

each a real ``StorageSupervisor`` child in its own isolated project root, and
requires per step the same success value or the same error code AND message,
then the same durable state: every table of both ``data/tofu.db`` files,
compared row by row after authority-clock values and generated identities
are tokenized. Command IDs are deterministic per step, so receipts compare
too. ``KNOWN_DIVERGENCES`` of the Tofu-DB gate do not apply: the two SQL
authorities share a schema and admit no documented divergence.

A script that uses an operation the Rust registry does not serve fails with
the missing names; ``test_port_coverage`` requires complete registration.
The wire-level failure injection cases live in
``test_rust_sql_authority_wire.py``.

Binary: ``TOFU_TEST_SQL_AUTHORITY_BIN`` must name an explicitly built candidate.
Use ``scripts/remote_cargo.py --after-build`` to build and run this oracle on
the same isolated node. Tests never implicitly compile on the production pod.
"""

from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any

import pytest

from tests.support.sql_authority_identity import (
    assert_child_identity, assert_process_generation, bind_candidate_identity, child_identity,
)
from tests.tofudb import test_tofudb_differential as corpus

pytestmark = [pytest.mark.unit, pytest.mark.serial]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYTHON_AUTHORITY_COMMAND = (sys.executable, '-m', 'lib.storage_authority_sql')


def rust_authority_binary() -> Path:
    """The explicit Rust candidate; never start a local compiler implicitly."""
    explicit = os.environ.get('TOFU_TEST_SQL_AUTHORITY_BIN', '').strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            pytest.fail(f'TOFU_TEST_SQL_AUTHORITY_BIN={explicit} is not an executable file')
        return path
    pytest.skip('Set TOFU_TEST_SQL_AUTHORITY_BIN to the explicit candidate; build through remote_cargo.py')


def unported_operations() -> set[str]:
    completed = subprocess.run(
        [str(rust_authority_binary()), 'unported'], capture_output=True, text=True, check=True)
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


class Authority:
    """One isolated SQL authority child and its client."""

    def __init__(self, root: Path, command: tuple[str, ...]):
        from lib.storage import StorageSupervisor

        self.root = root
        self.command = command
        self.supervisor = StorageSupervisor(
            project_root=root, backend='sqlite', startup_timeout=60,
            sql_authority_command=command)
        try:
            self.supervisor.start()
            self.driver = corpus.LegacyDriver(self.supervisor.client)
        except BaseException:
            self.supervisor.stop()
            raise

    @property
    def database(self) -> Path:
        return self.root / 'data' / 'tofu.db'

    def stop(self) -> None:
        self.supervisor.stop()


def record_pair_identity(python: Authority, rust: Authority, record_property, *, phase='started',
                         previous=None, restarted_role=None) -> dict:
    identities = {
        role: child_identity(authority.supervisor, authority.command)
        for role, authority in (('python', python), ('rust', rust))
    }
    assert restarted_role is None or (previous is not None and restarted_role in identities)
    try:
        for identity in identities.values():
            assert_child_identity(identity)
        left, right = identities['python'], identities['rust']
        assert left['pid'] != right['pid'], identities
        assert left['executable'] == str(Path(sys.executable).resolve()), identities
        assert left['cmdline'] == list(PYTHON_AUTHORITY_COMMAND), identities
        assert left['executable'] != right['executable'], 'oracle and candidate executable collapsed'
        assert right['cmdline'] == [str(rust_authority_binary()), 'serve'], identities
        assert left['executable_sha256'] != right['executable_sha256'], 'oracle and candidate bytes collapsed'
        bind_candidate_identity(right)
        if previous is not None:
            for role, identity in identities.items():
                assert_process_generation(role, previous[role], identity, restarted=role == restarted_role)
    finally:
        record_property('sql_authority_identity_' + phase, json.dumps(identities, sort_keys=True))
    return identities


@pytest.fixture()
def pair_identity(record_property):
    previous = None

    def observe(pair, *, phase='started', restarted_role=None):
        nonlocal previous
        previous = record_pair_identity(*pair, record_property, phase=phase,
                                        previous=previous, restarted_role=restarted_role)

    return observe


def _seed_payment_ties(root: Path, documents: list[dict]) -> None:
    """Pre-create one authority with the corpus' tie rows (both sides alike)."""
    from lib.storage_authority_sql import schema
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession

    data_dir = root / 'data'
    data_dir.mkdir(parents=True)
    with closing(sqlite3.connect(data_dir / 'tofu.db')) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            schema.initialize_schema(SQLiteSession(connection))
            connection.executemany(
                'INSERT INTO billing_payments(id, user_id, provider, provider_id, '
                'amount_minor, currency, credit_micro, status, created_at, settled_at, raw) '
                'VALUES(:id, :user_id, :provider, :provider_id, :amount_minor, :currency, '
                ':credit_micro, :status, :created_at, :settled_at, :raw)',
                [{**document, 'raw': json.dumps(document['raw'])} for document in documents],
            )


@pytest.fixture()
def authorities(tmp_path, request, pair_identity):
    binary = rust_authority_binary()
    tie_documents = corpus._payment_tie_documents(request)
    if tie_documents:
        for side in ('python-authority', 'rust-authority'):
            _seed_payment_ties(tmp_path / side, tie_documents)
    python = Authority(tmp_path / 'python-authority', PYTHON_AUTHORITY_COMMAND)
    try:
        rust = Authority(tmp_path / 'rust-authority', (str(binary), 'serve'))
    except BaseException:
        python.stop()
        raise
    try:
        pair_identity((python, rust))
        yield python, rust
    finally:
        rust.stop()
        python.stop()


def _step_command_id(script: str, index: int) -> str:
    return 'diff-' + hashlib.sha256(f'{script}\0{index}'.encode()).hexdigest()[:24]


def run_script(name: str, script: list, driver) -> list:
    outcomes = []
    for index, step in enumerate(script):
        payload = corpus._resolve_payload_refs(step.payload, outcomes)
        command_id = _step_command_id(name, index) if step.command or step.maintenance else None
        if step.eventual:
            outcome = _eventual(driver, step, payload, command_id)
        else:
            outcome = driver.execute(step.operation, payload, command_id, maintenance=step.maintenance)
        outcomes.append((step, outcome))
    return outcomes


def _eventual(driver, step, payload, command_id):
    deadline = time.monotonic() + corpus._EVENTUAL_TIMEOUT_S
    outcome = driver.execute(step.operation, payload, command_id, maintenance=step.maintenance)
    anchored, since = None, 0.0
    while time.monotonic() < deadline:
        candidate = driver.execute(step.operation, payload, command_id, maintenance=step.maintenance)
        if candidate.ok:
            outcome = candidate
            if step.eventual_min_hits <= 0 or (
                    isinstance(candidate.value, list) and len(candidate.value) >= step.eventual_min_hits):
                now = time.monotonic()
                if since and candidate.value == anchored:
                    if now - since >= corpus._EVENTUAL_STABLE_WINDOW_S:
                        return candidate
                else:
                    anchored, since = candidate.value, now
        time.sleep(corpus._EVENTUAL_POLL_INTERVAL_S)
    return outcome


def _comparable(step, outcome, tokens: dict[str, str]):
    ignored = frozenset(corpus._DROP_FIELDS) | step.ignore_fields
    identity_field = 'document_id' if step.operation == 'paper.document.resolve' else ''
    value = corpus._worker_comparison_value(step.operation, outcome.value)
    value = corpus._drop_comparison_fields(value, ignored)
    return corpus._volatile_signature(outcome.value, ignored), corpus._tokenize_server_ids(value, tokens, identity_field)


def step_divergences(name: str, python_outcomes: list, rust_outcomes: list) -> list[str]:
    divergences = []
    python_tokens: dict[str, str] = {}
    rust_tokens: dict[str, str] = {}
    for index, ((step, left), (_, right)) in enumerate(zip(python_outcomes, rust_outcomes)):
        if not step.compare:
            if not (left.ok and right.ok):
                divergences.append(corpus._format_divergence(name, index, step, left, right))
            continue
        if left.ok != right.ok:
            same = False
        elif not left.ok:
            same = (left.error_code, left.error_message) == (right.error_code, right.error_message)
        elif step.compare_shape_only:
            same = corpus._same_success_shape(left, right)
        else:
            left_signature, left_value = _comparable(step, left, python_tokens)
            right_signature, right_value = _comparable(step, right, rust_tokens)
            same = left_signature == right_signature and corpus._json_equal(left_value, right_value)
        if not same:
            divergences.append(corpus._format_divergence(name, index, step, left, right).replace(
                'tofu-db:', 'rust:') + (f'\n  python message: {left.error_message}' if not left.ok else ''))
    return divergences


# ── Durable-state comparison ─────────────────────────────────────────────
# Generated identities, whole or embedded in composite keys (`11:<hex>:<hex>`):
# uuid4 (dashed or simple) and secrets.token_hex(16|32). Each distinct run
# maps to one token per authority, so equal structure is still required.
_IDENTITY_RUN = re.compile(
    r'(?<![0-9a-f])(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}|[0-9a-f]{32})'
    r'(?![0-9a-f])')
_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}')


class _StateTokens:
    """Per-authority tokens: identities stay consistent within one database."""

    def __init__(self, window: tuple[float, float]):
        self.identities: dict[str, str] = {}
        self.window = window

    def scalar(self, value: Any) -> Any:
        low, high = self.window
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            if low * 1000 <= value <= high * 1000 or low <= value <= high:
                return '<authority-clock>'
            return value
        if isinstance(value, str):
            if _ISO.match(value) and value[:4] >= time.strftime('%Y', time.gmtime(low)):
                return '<authority-clock-iso>'
            return _IDENTITY_RUN.sub(
                lambda match: self.identities.setdefault(match.group(0), f'<id:{len(self.identities)}>'), value)
        return value

    def document(self, value: Any) -> Any:
        if isinstance(value, dict):
            document = {self.scalar(key): self.document(item) for key, item in sorted(value.items())}
            # A set emitted in lexical order of independently random UUIDs
            # (the corpus' `_tokenize_server_ids` applies the same rule).
            if isinstance(document.get('deletedTurnIds'), list):
                document['deletedTurnIds'] = sorted(document['deletedTurnIds'], key=str)
            return document
        if isinstance(value, list):
            return [self.document(item) for item in value]
        return self.scalar(value)


def _decode_column(value: Any, tokens: _StateTokens) -> Any:
    if isinstance(value, bytes):
        from lib.storage_authority_sql.receipt_codec import decode_receipt_response
        try:
            return tokens.document(decode_receipt_response(value))
        except Exception:
            return {'$bytes': base64.b64encode(value).decode()}
    if isinstance(value, str) and value[:1] in '{["':
        try:
            return tokens.document(json.loads(value))
        except ValueError:
            return value
    return tokens.scalar(value)


# Columns that are per-request identities derived from server-generated
# values (receipt keys hash command IDs/digests over payloads that embed
# each authority's own generated ids).
_IDENTITY_COLUMNS = {
    'storage_command_receipts_v2': {'command_key', 'request_digest'},
    'storage_command_receipts': {'request_digest'},
}


def database_state(path: Path, window: tuple[float, float]) -> dict[str, list]:
    tokens = _StateTokens(window)
    state: dict[str, list] = {}
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            kept = [column for column in columns if column not in _IDENTITY_COLUMNS.get(table, ())]
            rows = []
            for raw in connection.execute(f'SELECT {",".join(chr(34) + c + chr(34) for c in kept)} FROM "{table}"'):
                rows.append([_decode_column(value, tokens) for value in raw])
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True, default=str))
            state[table] = rows
        state['$schema'] = sorted(
            (row[0], row[1], row[2] or '') for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    return state


def state_divergences(python_db: Path, rust_db: Path, window) -> list[str]:
    left, right = database_state(python_db, window), database_state(rust_db, window)
    divergences = []
    for table in sorted(set(left) | set(right)):
        if left.get(table) != right.get(table):
            divergences.append(
                f'table {table}:\n  python: {json.dumps(left.get(table), default=str)[:1500]}\n'
                f'  rust:   {json.dumps(right.get(table), default=str)[:1500]}')
    return divergences


# ── The gate ─────────────────────────────────────────────────────────────
_SCRIPTS = corpus._all_scripts()


def test_port_coverage():
    """Report the port ratchet; fails only if the catalog drifted from the crate."""
    missing = unported_operations()
    catalog = json.loads((REPOSITORY_ROOT / 'contracts' / 'storage_operations_v1.json').read_text())
    names = {entry['name'] for entry in catalog['operations']}
    assert missing <= names
    assert not missing, 'unported catalog operations: ' + ', '.join(sorted(missing))
    print(f'rust SQL authority serves {len(names) - len(missing)}/{len(names)} catalog operations')


@pytest.mark.parametrize('script_name', sorted(_SCRIPTS), ids=lambda name: name)
def test_script_matches_python_authority(script_name, request, record_property, pair_identity):
    script = _SCRIPTS[script_name]
    missing = sorted({step.operation for step in script} & unported_operations())
    assert not missing, 'not yet ported: ' + ', '.join(missing)
    authorities = request.getfixturevalue('authorities')
    python, rust = authorities
    started = time.time() - 60
    python_outcomes = run_script(script_name, script, python.driver)
    rust_outcomes = run_script(script_name, script, rust.driver)
    record_property('sql_authority_operations', json.dumps([step.operation for step in script]))
    for outcomes in (python_outcomes, rust_outcomes):
        corpus._assert_management_outcomes(script_name, outcomes)
    divergences = step_divergences(script_name, python_outcomes, rust_outcomes)
    assert not divergences, 'Rust SQL authority diverged:\n' + '\n'.join(divergences)
    # Compare at the same projection boundary, not two worker scheduling points.
    # The existing pytest timeout also bounds a projection that cannot drain.
    for authority in authorities:
        with closing(sqlite3.connect(
                f'file:{authority.database}?mode=ro', uri=True)) as connection:
            while connection.execute(
                    'SELECT COUNT(*) FROM storage_projection_outbox').fetchone()[0]:
                time.sleep(0.01)
    pair_identity(authorities, phase='executed')
    python.stop()
    rust.stop()
    window = (started, time.time() + 366 * 86400)
    divergences = state_divergences(python.database, rust.database, window)
    assert not divergences, 'durable state diverged:\n' + '\n'.join(divergences)


def test_tokenizer_keeps_fixed_timestamps_and_tokenizes_clock_values():
    tokens = _StateTokens((1_900_000_000.0, 1_950_000_000.0))
    assert tokens.scalar(1_700_000_000_000) == 1_700_000_000_000
    assert tokens.scalar(1_900_000_100_000) == '<authority-clock>'
    assert tokens.scalar(1_900_000_100.5) == '<authority-clock>'
    uid = '0b4c8a7e-0000-4000-8000-000000000001'
    assert tokens.scalar(uid) == tokens.scalar(uid) == '<id:0>'


def test_explicit_python_oracle_ignores_inherited_rust_candidate(tmp_path, monkeypatch, record_property):
    monkeypatch.setenv('TOFU_TEST_SQL_AUTHORITY_BIN', str(tmp_path / 'must-not-launch'))
    python = Authority(tmp_path / 'python', PYTHON_AUTHORITY_COMMAND)
    try:
        identity = child_identity(python.supervisor, PYTHON_AUTHORITY_COMMAND)
        record_property('sql_authority_identity_regression', json.dumps(identity, sort_keys=True))
        assert_child_identity(identity)
        assert identity['executable'] == str(Path(sys.executable).resolve())
        assert identity['cmdline'] == list(PYTHON_AUTHORITY_COMMAND)
    finally:
        python.stop()


def test_identity_rejects_python_versus_python_collapse(tmp_path, record_property):
    python = Authority(tmp_path / 'python', PYTHON_AUTHORITY_COMMAND)
    try:
        other_python = Authority(tmp_path / 'other-python', PYTHON_AUTHORITY_COMMAND)
        try:
            with pytest.raises(AssertionError, match='executable collapsed'):
                record_pair_identity(python, other_python, record_property, phase='rejected_collapse')
        finally:
            other_python.stop()
    finally:
        python.stop()


def test_identity_reads_actual_command_not_only_popen_args(record_property):
    from types import SimpleNamespace

    command = [sys.executable, '-c', 'import time; time.sleep(60)']
    process = subprocess.Popen(command)
    try:
        # Simulate a configured launch receipt that disagrees with the live child.
        process.args = list(PYTHON_AUTHORITY_COMMAND)
        identity = child_identity(SimpleNamespace(_process=process), PYTHON_AUTHORITY_COMMAND)
        record_property('sql_authority_identity_regression', json.dumps(identity, sort_keys=True))
        assert identity['popen_args'] == list(PYTHON_AUTHORITY_COMMAND)
        assert identity['cmdline'] == command
        with pytest.raises(AssertionError, match='actual child command differs'):
            assert_child_identity(identity)
    finally:
        process.terminate()
        process.wait(timeout=15)
