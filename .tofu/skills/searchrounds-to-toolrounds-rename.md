---
name: searchRounds-to-toolRounds-rename
description: Renamed searchRounds → toolRounds across entire codebase (DB, Python, JS)
enabled: true
tags: [rename, refactor, database, schema]
created: 2026-04-12T04:01:50Z
updated: 2026-04-12T04:01:50Z
---

# searchRounds → toolRounds Rename (April 2026)

## What was renamed
Legacy "searchRounds" terminology from when tool system was search-only has been renamed to "toolRounds" everywhere.

## Naming map
| Old | New | Context |
|---|---|---|
| `searchRounds` (JS/JSON key) | `toolRounds` | Message property, SSE events, API payloads |
| `search_rounds` (DB column) | `tool_rounds` | task_results table column |
| `search_round_num` (Python var) | `tool_round_num` | Orchestrator/dispatch counter |
| `getSearchRoundsFromMsg` (JS func) | `getToolRoundsFromMsg` | core.js helper |
| `renderSearchRoundsHTML` (JS func) | `renderToolRoundsHTML` | ui.js renderer |
| `_syncSearchRoundsDOM` (JS func) | `_syncToolRoundsDOM` | ui.js DOM sync |
| `_continueSearchRounds` (JS) | `_continueToolRounds` | Continue checkpoint marker |
| `checkpointSearchRounds` (API) | `checkpointToolRounds` | Continue config payload |
| `_checkpointSearchRounds` (Python) | `_checkpointToolRounds` | Task dict stash key |

## DB Migration
- Schema version bumped 7 → 8
- Column renamed: `ALTER TABLE task_results RENAME COLUMN search_rounds TO tool_rounds`
- Conversations messages JSON: `REPLACE(messages::text, '"searchRounds"', '"toolRounds"')::jsonb`
- Both migrations are idempotent

## Backward compat
- `getToolRoundsFromMsg()` still falls back to `msg.searchResults` for very old data
- `_API_MESSAGE_FIELDS` whitelist strips toolRounds automatically (not in the set)

