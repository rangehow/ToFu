---
name: chatui-to-tofu-db-rename-migration
description: chatui→tofu PG DB rename: data lives in 'chatui' db; conversations PK is composite (id,user_id); rename needs server stopped
enabled: true
tags: [database, postgres, migration, rebrand]
created: 2026-06-01T03:40:59Z
updated: 2026-06-01T03:40:59Z
---

# Renaming the live PG database chatui → tofu

## Background
The app default PG db name was rebranded `chatui` → `tofu` (lib/database/_core.py
default `TOFU_PG_DBNAME` → 'tofu'). Real data historically lived in a db literally
named **`chatui`**. If the server starts with no `TOFU_PG_DBNAME` set, it
auto-bootstraps a fresh near-empty `tofu` db and writes NEW conversations there →
sidebar looks "wiped" but `chatui` db is intact. PG on port **15439**, user
`hadoop-aipnlp` (see chatui-db-postgres-port-15439-direct-access memory).

## Two ways to fix
1. **Zero-downtime, no rename**: set `TOFU_PG_DBNAME=chatui` in the server's env and
   restart. Keeps db literally named chatui.
2. **Actual rename** (what the user wanted): `ALTER DATABASE chatui RENAME TO tofu`.
   This is INSTANT (catalog-only, no data copy) BUT requires **zero connections to
   BOTH chatui and tofu** — impossible while server.py runs (holds ~25 conns to tofu).
   Must run during a server-down window. PG process itself stays up; a script talks
   to it directly.

## Gotchas discovered
- **conversations PRIMARY KEY is COMPOSITE `(id, user_id)`**, NOT `id` alone. Any
  upsert must use `ON CONFLICT (id, user_id)`. FK `user_id → users(id) ON DELETE CASCADE`.
- search_tsv is a normal tsvector column (NOT generated) — preserve explicitly when
  copying rows (`search_tsv::text` out, `%s::tsvector` in).
- When server is misrouted to `tofu`, capture the new convs that exist ONLY in tofu
  before renaming (else lost). Compare id sets; watch for byte-identical dups.
- pg_dump/psql 18.4 available in env tofu.

## Deliverable
`scripts/migrate_chatui_to_tofu.py` (--apply): pg_dump chatui →
data/chatui_pre_rename_backup.sql, rename misrouted tofu→tofu_misroute_bak (kept,
not dropped), ALTER chatui→tofu, reimport saved convs from
data/tofu_misroute_convs.json with ON CONFLICT (id,user_id) DO NOTHING. Preflight
ABORTS if either db has live connections. User runs it themselves during restart
(agent must NOT stop the server — see mandatory-approval memory).

