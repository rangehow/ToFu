"""Structural work-count comparison for one fixed conversation read.

Work-count evidence, **not a performance verdict**: this module counts storage
statements, document decodes and per-turn row materializations, and records no
clock, throughput or durability claim.

Observed counters and their seams (all existing; nothing here is a production
knob, cache or metric flag):

* ``select_statements`` — SELECT frames that reach SQLite from one in-process
  ``conversation.get`` handler, read through the ``sqlite3`` trace callback.
  This is authority-internal: the harness's own ``BEGIN``/``ROLLBACK`` and the
  Sidecar's client/dispatch boundary are excluded. ``mutating_statements``
  counts non-SELECT, non-transaction frames, so a write on the read path is
  visible instead of hidden behind a "queries" label.
* ``document_decodes`` — ``_common._load`` calls, patched in each reader module
  that bound the name (``_conversations``, ``_turns_core``,
  ``turn_projection_head``).
* ``turn_rows`` — ``_conversations._turn_public`` calls (the name that module
  bound from ``_turns_core`` at import); one per materialized stored Turn row.

Counters are observed on the SQLite Sidecar only. Round trips are the one
counter with a client-seam analogue — one ``client.query('conversation.get')``
is one request on both stacks — while document decodes and Turn-row
materializations on TofuDB happen inside the Rust engine with no Python seam.
The TofuDB cells are therefore recorded ``unmeasured`` per counter with the
observed engine-refusal reason; they are never estimated or modelled. The
native engine is the only seam that can observe them and no source-matched
engine is available in this offline environment (a native build is out of
scope).
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
import os
import sqlite3
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.unit

SHORT_TURNS = 1
MULTI_TURNS = 6
#: Full transcript reads materialize two JSON documents per Turn (projection and
#: settlement) plus the conversation settings document.
_DOCUMENTS_PER_MATERIALIZED_TURN = 2

_TRANSACTION_KEYWORDS = frozenset(
    {'BEGIN', 'COMMIT', 'END', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'})
_MUTATING_KEYWORDS = frozenset(
    {'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER',
     'VACUUM', 'PRAGMA'})


@dataclass(frozen=True)
class WorkCounts:
    """Observed structural work of one logical storage operation."""

    select_statements: int
    mutating_statements: int
    document_decodes: int
    turn_rows: int


@dataclass(frozen=True)
class Unmeasured:
    """A backend cell with no existing observation seam in this environment."""

    status: str
    reason: str


def _fixed_conversation(path: Path, turns: int):
    """One deterministic conversation on fixture-owned scratch storage."""
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
    from lib.storage_authority_sql.schema import initialize_schema
    from tests.storage_authority_sql.test_turns_delta_sync import (
        _seed_conv, _seed_turn,
    )

    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    connection.execute('PRAGMA synchronous=FULL')
    session = SQLiteSession(connection)
    initialize_schema(session)
    _seed_conv(session)
    for ordinal in range(1, turns + 1):
        _seed_turn(
            session, f'turn-{ordinal:03d}', ordinal, 1000 + ordinal,
            projection={'content': f'turn {ordinal} body',
                        'data': {'ordinal': ordinal}})
    return session, connection


def _statement_counts(frames: list[str]) -> tuple[int, int]:
    """SELECT and non-SELECT non-transaction frames from one handler call."""
    selects = mutations = 0
    for frame in frames:
        keyword = frame.lstrip().split(None, 1)[0].upper() if frame.strip() else ''
        if keyword in _TRANSACTION_KEYWORDS:
            continue
        if keyword == 'SELECT':
            selects += 1
        elif keyword in _MUTATING_KEYWORDS:
            mutations += 1
    return selects, mutations


def _measure_sqlite_conversation_get(path: Path, turns: int, **payload) -> tuple[WorkCounts, dict]:
    """Run one ``conversation.get`` and read the three existing seams."""
    from lib.storage_authority_sql import turn_projection_head
    from lib.storage_authority_sql.operations_pkg import _conversations, _turns_core

    session, connection = _fixed_conversation(path, turns)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    connection.execute('BEGIN')
    try:
        with ExitStack() as seams:
            # ``_load`` owns JSON-document decoding; each reader module binds it
            # at import time, so every actual decode site is observed.
            decodes = [
                seams.enter_context(patch.object(module, '_load', wraps=module._load))
                for module in (_conversations, _turns_core, turn_projection_head)
            ]
            # ``_conversations`` bound ``_turn_public`` from ``_turns_core`` at
            # import, so this is the binding the handler actually calls.
            materialized = seams.enter_context(patch.object(
                _conversations, '_turn_public', wraps=_conversations._turn_public))
            result = _conversations._conversation_get(
                session, {'conv_id': 'conv-d', 'user_id': 1, **payload})
            decoded = sum(site.call_count for site in decodes)
            rows = materialized.call_count
    finally:
        connection.rollback()
        connection.set_trace_callback(None)
        connection.close()
    selects, mutations = _statement_counts(statements)
    return WorkCounts(selects, mutations, decoded, rows), result


@lru_cache(maxsize=1)
def _native_engine_refusal() -> str | None:
    """The observed reason no native work could be measured here.

    ``None`` only when a source-matched engine is positively verified: the
    application root must yield a source fingerprint *and* the engine must match
    it. ``verify_current`` returns without refusing when the root has no
    fingerprint, which is not evidence of a match.
    """
    binary = os.environ.get('TOFUDB_BIN')
    if not binary:
        return 'TOFUDB_BIN is not set: no native TofuDB engine to measure'
    from serverctl_pkg.native_engine import source_fingerprint, verify_current

    root = Path(__file__).resolve().parents[2]
    if source_fingerprint(root) is None:
        return f'application sources at {root} have no verifiable storage-engine fingerprint'
    try:
        verify_current(Path(binary), root)
    except Exception as exc:  # the refusal is evidence, not a failure
        return f'{type(exc).__name__}: {exc}'
    return None


def structural_work_report(tmp_path: Path) -> dict:
    """The full table: observed SQLite counts and the TofuDB boundary."""
    short, short_result = _measure_sqlite_conversation_get(
        tmp_path / 'short.sqlite', SHORT_TURNS)
    multi, multi_result = _measure_sqlite_conversation_get(
        tmp_path / 'multi.sqlite', MULTI_TURNS)
    metadata, metadata_result = _measure_sqlite_conversation_get(
        tmp_path / 'metadata.sqlite', MULTI_TURNS, derive_messages=False)
    window, window_result = _measure_sqlite_conversation_get(
        tmp_path / 'window.sqlite', MULTI_TURNS, message_window=2)
    refusal = _native_engine_refusal()
    return {
        'schema': 'tofu.storage.structural-work.v2',
        'kind': 'work-count evidence, not a performance verdict',
        'conversation': {'short_turns': SHORT_TURNS, 'multi_turns': MULTI_TURNS},
        'sqlite_sidecar': {
            'conversation.get(full, short)': asdict(short),
            'conversation.get(full, multi)': asdict(multi),
            'conversation.get(metadata, multi)': asdict(metadata),
            'conversation.get(window=2, multi)': asdict(window),
        },
        'tofudb_engine': {
            'status': 'unmeasured',
            'reason': refusal or (
                'a source-matched engine is available, but this offline port '
                'only counts the SQLite authority seams; the native client '
                'seam is not measured here'),
            # Per-counter observability, so "unmeasured" cannot be read as a
            # portable count that simply was not run.
            'per_counter': {
                'client_round_trips': 'one storage.v2 request per operation; '
                                      'not observed here',
                'select_statements': 'no SQL in the native engine; not observed here',
                'document_decodes': 'engine-internal (no Python seam); not observed here',
                'turn_rows': 'engine-internal (no Python seam); not observed here',
            },
        },
        'content': {
            'short_messages': len(short_result['messages']),
            'multi_messages': len(multi_result['messages']),
            'multi_first': multi_result['messages'][0]['content'],
            'multi_last': multi_result['messages'][-1]['content'],
            'metadata_messages': len(metadata_result['messages']),
            'metadata_msg_count': metadata_result['metadata']['msg_count'],
            'window_messages': [item['content'] for item in window_result['messages']],
        },
    }


def test_conversation_read_work_counts_are_exact_and_stable(tmp_path):
    """The observed SQLite work of one read, short versus multi-turn.

    SELECT count stays constant while document decodes and Turn row
    materializations scale with the Turns a read materializes.  A future change
    that adds a statement, re-decodes a document or copies a Turn row per
    message changes this test.
    """
    report = structural_work_report(tmp_path)
    sqlite = report['sqlite_sidecar']

    short = sqlite['conversation.get(full, short)']
    multi = sqlite['conversation.get(full, multi)']
    assert short == {'select_statements': 2, 'mutating_statements': 0,
                     'document_decodes': 3, 'turn_rows': 1}
    assert multi == {'select_statements': 2, 'mutating_statements': 0,
                     'document_decodes': 13, 'turn_rows': 6}
    assert multi['document_decodes'] == (
        1 + _DOCUMENTS_PER_MATERIALIZED_TURN * MULTI_TURNS)
    assert multi['turn_rows'] == MULTI_TURNS

    # Work scales with the Turns a read materializes, not the Turns it stores.
    metadata = sqlite['conversation.get(metadata, multi)']
    assert metadata == {'select_statements': 2, 'mutating_statements': 0,
                        'document_decodes': 1, 'turn_rows': 0}
    window = sqlite['conversation.get(window=2, multi)']
    assert window == {'select_statements': 3, 'mutating_statements': 0,
                      'document_decodes': 5, 'turn_rows': 2}

    assert report['content'] == {
        'short_messages': 1, 'multi_messages': 6,
        'multi_first': 'turn 1 body', 'multi_last': 'turn 6 body',
        'metadata_messages': 0, 'metadata_msg_count': 6,
        'window_messages': ['turn 5 body', 'turn 6 body'],
    }
    # The other backend's cells must be explicitly unmeasured, never estimated,
    # and the recorded reason must be the observed one.
    reason = report['tofudb_engine']['reason']
    assert report['tofudb_engine']['status'] == 'unmeasured'
    assert reason == _native_engine_refusal()
    assert reason
    assert ('TOFUDB_BIN' in reason or 'does not match' in reason
            or 'no verifiable' in reason)
    print(
        'structural work (work counts, not time): '
        f"short={short} multi={multi} metadata={metadata} window={window}")


def test_native_engine_work_is_recorded_unmeasured_with_its_reason():
    """Pin the boundary: no TofuDB number without a source-matched engine.

    If an engine ever becomes available this fails/skips loudly instead of
    letting a report silently inherit stale or estimated native numbers.
    """
    refusal = _native_engine_refusal()
    if refusal is None:
        pytest.skip('source-matched native engine available; extend the port '
                    'to measure it rather than recording unmeasured')
    assert refusal
    assert ('TOFUDB_BIN' in refusal or 'does not match' in refusal
            or 'no verifiable' in refusal)