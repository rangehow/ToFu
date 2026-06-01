---
name: chatui-db-postgres-port-15439-direct-access
description: ChatUI Postgres direct access: port 15439, db chatui, user hadoop-aipnlp, conversations.messages JSONB schema
enabled: true
tags: [database, postgresql, debug, query, schema]
created: 2026-04-18T03:23:46Z
updated: 2026-04-18T03:23:46Z
---

# ChatUI Database Direct Access (Postgres)

For scripts/probes that need raw DB access (e.g. investigating a specific conversation,
running ad-hoc analytics), bypass Flask's `get_db()` (requires app context) and connect
directly with psycopg2.

## Connection parameters (this project)

The running PG instance is on **port 15439** (NOT the code default of 15432 in
`lib/database/_core.py`). To check: `grep 'listening on IPv4' logs/postgresql.log | tail -1`.

```python
import psycopg2, psycopg2.extras
conn = psycopg2.connect("host=127.0.0.1 port=15439 dbname=chatui user=hadoop-aipnlp")
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
```

**DO NOT** use DSNs without explicit `user=` — Postgres defaults to current shell user
and may fail on Kerberos (`could not initiate GSSAPI security context`) when `krb5.conf`
is misconfigured for the SANKUAI.COM realm.

**DO NOT** trust the SQLite file `data/chatui.db` when Postgres is running — it's an
empty shadow of the old backend. Always check for live PG first:
`ps -ef | grep 'postgres -D'` shows the data-dir of any active instance.

## Schema — single-table conversation storage

There is **no separate `messages` table**. All messages live as a JSON array in
`conversations.messages` (jsonb):

```
conversations:
  id, user_id, title, messages (jsonb), created_at, updated_at,
  settings (jsonb), msg_count, search_text, search_tsv
```

Message object keys (per entry in `messages` array):
- `role`, `content`, `thinking`, `timestamp`
- `model`, `finishReason`, `thinkingDepth`, `traceId`, `elapsedMs`, `usage`
- `toolRounds` (list), `apiRounds` (list), `images`, `pdfTexts`
- `_showingTranslation`, `_translateDone`, `_translateField`, `_translateModel`, `_translateTaskId`, `translatedContent`
- `_emitContent`, `_emitToolName` (emit_to_user feature)

`settings` subkeys: `model`, `preset`, `thinkingDepth`, `defaultThinkingDepth`, `projectPaths`,
`browserEnabled`, `fetchEnabled`, `memoryEnabled`, `searchMode`, `autoTranslate`, etc.

## Example: inspect a single conversation

```python
cur.execute("SELECT id,title,created_at,updated_at,msg_count,settings FROM conversations WHERE id=%s",(conv_id,))
row = cur.fetchone()
print('Title:', row['title'], 'Depth:', row['settings'].get('thinkingDepth'))
cur.execute("SELECT messages FROM conversations WHERE id=%s",(conv_id,))
msgs = cur.fetchone()['messages']   # already parsed to list[dict] by psycopg2
for m in msgs:
    print(m['role'], len(m.get('content','')), 'thinking=', len(m.get('thinking','')))
```

## When to look in app.log vs DB vs raw_sse.log

| Looking for… | Source |
|---|---|
| The prompt user actually sent | `conversations.messages[user].content` |
| What depth/effort was used | `conversations.messages[assistant].thinkingDepth` + `conversations.settings.thinkingDepth` |
| Exact LLM round outcome | `grep conv_id logs/app.log` → `stream_llm_response complete` line |
| Was thinking actually returned on the wire | `LLM_DEBUG_RAW_SSE=opus` → `logs/raw_sse.log` (see raw `reasoning_content` fields) |
| Thinking text that was captured | `conversations.messages[assistant].thinking` |

## Common pitfall

`data/chatui.db` (SQLite) being empty **doesn't** mean conversations are lost — when PG
is running, it's authoritative. Always check PG first with the explicit `user=hadoop-aipnlp`
DSN, never rely on the default role `chatui` (doesn't exist in this installation).

