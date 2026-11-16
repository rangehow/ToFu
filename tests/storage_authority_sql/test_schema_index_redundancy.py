"""No table of the SQL authority indexes the same key twice (executable specification).

A secondary index whose columns, order, collation and predicate equal a PRIMARY KEY or UNIQUE
constraint's index is pure write amplification: every insert writes the same key into two b-trees.
``storage_events_task_idx`` duplicated the task-event primary key and cost 778 KB per Turn on
STORAGE-PRESSURE-COMPARE-1's fixed task (4.8% of the SQL authority's bytes). Fresh authorities never
create a duplicate; established ones retire them through the explicit offline index transition
(``OBSOLETE_DEFERRED_INDEX_NAMES``), never during startup.
"""
from __future__ import annotations

import collections
import sqlite3

import pytest

from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.schema import (
    OBSOLETE_DEFERRED_INDEX_NAMES,
    deferred_index_statements,
    initialize_schema,
)

pytestmark = pytest.mark.unit

RETIRED_DUPLICATES = frozenset({
    'storage_events_task_idx',
    'idx_storage_conversation_turns_order',
    'idx_tenant_users_email',
    'idx_storage_knowledge_chunks_document',
    'idx_storage_knowledge_assets_document',
})


@pytest.fixture
def fresh(tmp_path):
    connection = sqlite3.connect(tmp_path / 'fresh.db')
    connection.row_factory = sqlite3.Row
    initialize_schema(SQLiteSession(connection))
    for statement in deferred_index_statements('sqlite'):
        connection.execute(statement)
    connection.commit()
    connection.row_factory = None
    yield connection
    connection.close()


def _index_keys(connection, table):
    """Index name -> (columns with collation and order, partial predicate)."""
    keys = {}
    for row in connection.execute(f"PRAGMA index_list('{table}')").fetchall():
        name = row[1]
        columns = tuple((info[2], info[3], info[4]) for info in connection.execute(
            f"PRAGMA index_xinfo('{name}')").fetchall() if info[5])
        found = connection.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        sql = found[0] if found else None
        predicate = sql.split(' WHERE ', 1)[1].strip() if sql and ' WHERE ' in sql else None
        keys[name] = (columns, predicate)
    return keys


def test_a_fresh_authority_indexes_no_key_twice(fresh):
    duplicates = []
    tables = [row[0] for row in fresh.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    for table in tables:
        by_key = collections.defaultdict(list)
        for name, key in _index_keys(fresh, table).items():
            by_key[key].append(name)
        duplicates += [(table, sorted(names)) for names in by_key.values() if len(names) > 1]
    assert duplicates == []


def test_established_authorities_retire_each_duplicate_offline(fresh):
    assert RETIRED_DUPLICATES <= OBSOLETE_DEFERRED_INDEX_NAMES
    existing = {row[0] for row in fresh.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert not RETIRED_DUPLICATES & existing


def test_the_child_summary_hint_names_the_task_event_primary_key(fresh):
    """The Request Inspector prefix-range probe keeps its index hint after the duplicate is gone."""
    import lib.storage_authority_sql.operations_pkg._records as records

    session = SQLiteSession(fresh)
    fresh.row_factory = sqlite3.Row
    plans = []
    original = session.fetch_all

    def fetch_all(sql, params=()):
        if 'task_id >= ?' in sql:
            plans.append(' '.join(str(row[3]) for row in fresh.execute(
                'EXPLAIN QUERY PLAN ' + sql, params).fetchall()))
        return original(sql, params)

    session.fetch_all = fetch_all
    assert records._event_inspector_summary(session, {'task_ids': ['task']}) == {'records': []}
    assert plans and all('sqlite_autoindex_storage_events_1' in plan for plan in plans)
