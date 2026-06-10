"""tests/test_core_schema_parity.py — READ-ONLY parity gate for Core migration.

This ships NOTHING live. It never opens a DB connection and never touches
`init_db()`. It only compiles the SQLAlchemy Core definition of a table to
DDL strings (compile-only, via `_core_schema.both_ddl`) and compares them,
token-normalized, against the live hand-maintained DDL in `_schema_pg.py` /
`_schema_sqlite.py`.

Purpose: a table is only *eligible* to be migrated onto Core once its Core
definition byte-matches (modulo whitespace/IF-NOT-EXISTS) the schema already
on disk. Until this test is green for a table, wiring it into the live
bootstrap would silently diverge fresh installs from upgraded ones.

Run:  pytest tests/test_core_schema_parity.py -v
"""

import re

import pytest

sa = pytest.importorskip("sqlalchemy")

from lib.database._core_schema import (
    PRICING_CACHE as _PRICING_CACHE,
    RECENT_PROJECTS as _RECENT_PROJECTS,
    SCHEMA_META as _SCHEMA_META,
    TRADING_CONFIG as _TRADING_CONFIG,
    both_ddl,
    define_table,
    jsonb_column,
)


# ── Live DDL as it exists on disk today (source of truth) ───────────────
# conversations — PG base table (lib/database/_schema_pg.py:170)
LIVE_PG_CONVERSATIONS = """
    CREATE TABLE conversations (
        id TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT 'New Chat',
        messages JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        settings JSONB NOT NULL DEFAULT '{}'::jsonb,
        msg_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (id, user_id)
    )
"""

# conversations — SQLite base table (lib/database/_schema_sqlite.py:115)
# NOTE: SQLite base table carries search_text; PG adds it via later migration.
LIVE_SQLITE_CONVERSATIONS = """
    CREATE TABLE conversations (
        id TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL DEFAULT 'New Chat',
        messages TEXT NOT NULL DEFAULT '[]',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        settings TEXT NOT NULL DEFAULT '{}',
        msg_count INTEGER NOT NULL DEFAULT 0,
        search_text TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (id, user_id)
    )
"""


# trading_config — identical on both backends
# (lib/database/_schema_pg.py:513, _schema_sqlite.py:433)
LIVE_TRADING_CONFIG = """
    CREATE TABLE trading_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
"""

# schema_meta — core-owned bootstrap kv (version + active domains); same shape
# as trading_config, identical on both backends.
LIVE_SCHEMA_META = """
    CREATE TABLE schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
"""


# pricing_cache — system kv cache (lib/database/_schema_pg.py:862, _sqlite.py:773)
LIVE_PG_PRICING_CACHE = """
    CREATE TABLE pricing_cache (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at BIGINT NOT NULL
    )
"""
LIVE_SQLITE_PRICING_CACHE = """
    CREATE TABLE pricing_cache (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
"""

# recent_projects — MRU list (lib/database/_schema_pg.py:870, _sqlite.py:781)
LIVE_PG_RECENT_PROJECTS = """
    CREATE TABLE recent_projects (
        path TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 1,
        last_used BIGINT NOT NULL
    )
"""
LIVE_SQLITE_RECENT_PROJECTS = """
    CREATE TABLE recent_projects (
        path TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 1,
        last_used INTEGER NOT NULL
    )
"""


def _norm(ddl: str) -> str:
    """Canonicalize DDL to a *semantic* form so backends that differ only in
    formatting compare equal, while genuine schema divergences (column types,
    missing columns, different PK columns) still differ.

    Transforms applied (all semantics-preserving on both PG and SQLite):
      - drop ``IF NOT EXISTS`` and collapse whitespace;
      - strip double-quotes around identifiers (SQLAlchemy quotes reserved
        words like ``"key"``);
      - reorder ``DEFAULT x NOT NULL`` → ``NOT NULL DEFAULT x``;
      - rewrite an inline single-column ``PRIMARY KEY`` into a trailing
        ``PRIMARY KEY (col)`` table constraint (the form Core emits).

    These are deliberately narrow. The ``conversations`` xfail (strict) acts
    as a tripwire: if a transform here ever over-masks, that genuinely
    divergent table would start passing and fail the suite as an xpass.
    """
    s = ddl.strip()
    s = re.sub(r"\bIF NOT EXISTS\b", "", s, flags=re.IGNORECASE)
    s = s.replace('"', "")
    s = re.sub(r"\s+", " ", s)
    s = s.replace("( ", "(").replace(" )", ")")
    s = s.rstrip(";").strip().lower()
    # DEFAULT <val> NOT NULL  ->  NOT NULL DEFAULT <val>
    # <val> is a quoted string ('' / 'new chat') or a bare token (0).
    s = re.sub(r"default\s+('(?:[^']|'')*'|\S+)\s+not null", r"not null default \1", s)
    # inline single-col PK:  "<col> <type...> primary key,"  ->
    #   strip inline + append "primary key (<col>)" before the closing paren.
    m = re.search(r"\(\s*(\w+)\b[^,]*\bprimary key\b", s)
    if m and "primary key (" not in s:
        col = m.group(1)
        s = re.sub(r"\bprimary key\b", "", s, count=1)
        s = re.sub(r"\s+", " ", s).replace(" ,", ",").replace(", ", ", ")
        s = s.rstrip(")").rstrip().rstrip(",") + f", primary key ({col}))"
        s = re.sub(r"\s+", " ", s)
    # PRIMARY KEY implies NOT NULL on both PG and SQLite. For a single-column
    # PK, drop the redundant NOT NULL that Core makes explicit on that column,
    # so the trailing-constraint form (Core) matches the inline form (live).
    pk = re.search(r"primary key \((\w+)\)", s)
    if pk:
        col = pk.group(1)
        s = re.sub(rf"(\(\s*{col}\s+\w+) not null,", r"\1,", s)
    return s


# Define the referenced `users` table + `conversations` ONCE at module scope.
# Core needs the FK target table present in the same MetaData to compile
# `REFERENCES users(id)`, and a Table may only be defined once per MetaData.
_USERS = define_table(
    "users",
    sa.Column("id", sa.Integer, primary_key=True),
)

_CONVERSATIONS = define_table(
    "conversations",
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Integer, nullable=False),
    sa.Column("title", sa.Text, nullable=False, server_default="New Chat"),
    sa.Column("messages", jsonb_column(), nullable=False, server_default="[]"),
    sa.Column("created_at", sa.BigInteger, nullable=False),
    sa.Column("updated_at", sa.BigInteger, nullable=False),
    sa.Column("settings", jsonb_column(), nullable=False, server_default="{}"),
    sa.Column("msg_count", sa.Integer, nullable=False, server_default="0"),
    sa.PrimaryKeyConstraint("id", "user_id"),
    sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
)


def test_trading_config_pg_parity():
    core = both_ddl(_TRADING_CONFIG)["pg"]
    assert _norm(core) == _norm(LIVE_TRADING_CONFIG), (
        "\n--- Core PG ---\n" + core +
        "\n--- Live PG ---\n" + LIVE_TRADING_CONFIG
    )


def test_trading_config_sqlite_parity():
    core = both_ddl(_TRADING_CONFIG)["sqlite"]
    assert _norm(core) == _norm(LIVE_TRADING_CONFIG), (
        "\n--- Core SQLite ---\n" + core +
        "\n--- Live SQLite ---\n" + LIVE_TRADING_CONFIG
    )


def test_schema_meta_pg_parity():
    core = both_ddl(_SCHEMA_META)["pg"]
    assert _norm(core) == _norm(LIVE_SCHEMA_META), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_SCHEMA_META
    )


def test_schema_meta_sqlite_parity():
    core = both_ddl(_SCHEMA_META)["sqlite"]
    assert _norm(core) == _norm(LIVE_SCHEMA_META), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SCHEMA_META
    )


def test_pricing_cache_pg_parity():
    core = both_ddl(_PRICING_CACHE)["pg"]
    assert _norm(core) == _norm(LIVE_PG_PRICING_CACHE), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_PRICING_CACHE
    )


def test_pricing_cache_sqlite_parity():
    core = both_ddl(_PRICING_CACHE)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_PRICING_CACHE), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_PRICING_CACHE
    )


def test_recent_projects_pg_parity():
    core = both_ddl(_RECENT_PROJECTS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_RECENT_PROJECTS), (
        "\n--- Core PG ---\n" + core + "\n--- Live PG ---\n" + LIVE_PG_RECENT_PROJECTS
    )


def test_recent_projects_sqlite_parity():
    core = both_ddl(_RECENT_PROJECTS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_RECENT_PROJECTS), (
        "\n--- Core SQLite ---\n" + core + "\n--- Live SQLite ---\n" + LIVE_SQLITE_RECENT_PROJECTS
    )


# conversations is a KNOWN-DIVERGENT reference case: its SQLite base table
# carries `search_text` while PG adds that column via a later migration, so a
# single Core Table cannot byte-match both backends. These are xfail to keep
# the divergence visible/documented WITHOUT blocking the suite. Do NOT wire
# conversations onto Core until this asymmetry is resolved.
@pytest.mark.xfail(reason="conversations schema differs per backend (search_text); not Core-eligible", strict=True)
def test_conversations_pg_parity():
    core = both_ddl(_CONVERSATIONS)["pg"]
    assert _norm(core) == _norm(LIVE_PG_CONVERSATIONS), (
        "\n--- Core PG ---\n" + core +
        "\n--- Live PG ---\n" + LIVE_PG_CONVERSATIONS
    )


@pytest.mark.xfail(reason="conversations schema differs per backend (search_text); not Core-eligible", strict=True)
def test_conversations_sqlite_parity():
    core = both_ddl(_CONVERSATIONS)["sqlite"]
    assert _norm(core) == _norm(LIVE_SQLITE_CONVERSATIONS), (
        "\n--- Core SQLite ---\n" + core +
        "\n--- Live SQLite ---\n" + LIVE_SQLITE_CONVERSATIONS
    )
