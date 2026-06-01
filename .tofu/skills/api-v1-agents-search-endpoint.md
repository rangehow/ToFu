---
name: api-v1-agents-search-endpoint
description: v1 search agents: POST /api/v1/agents/search (sync) + /async with full toggle set
enabled: true
tags: [api, v1, search, agents]
created: 2026-05-26T03:33:09Z
updated: 2026-05-26T03:33:09Z
---

# /api/v1/agents/search — Public Search Endpoint

External-facing wrapper around `lib.search.perform_web_search`. Lives at
`routes/api_v1/agents.py` (search section, ~line 240–440). Two routes:

- `POST /api/v1/agents/search` — synchronous, full pipeline (10–30s)
- `POST /api/v1/agents/search/async` — returns `{task_id}`; events via
  `GET /api/v1/tasks/{id}/events|stream` and the `search` push channel.

Scope: `agents:search` (added to `lib/api_keys.py:ALL_SCOPES`).

## Toggles (request body)
- `query` (required, ≤500)
- `max_results` (1–50)
- `freshness` ('' | day | week | month | year)
- `user_question` (≤2000)  — context for the LLM relevance filter
- `fetch_pages` (default true) — false ⇒ engine snippets only (~1–3s)
- `filter` (default true) — false ⇒ skip step-5 LLM relevance filter
- `rerank` (default true) — false ⇒ skip step-6 BM25
- `engines` (subset of DDG-HTML/Brave/Bing/DDG-API/SearXNG; unknown → fallback)
- `max_chars_per_page` (1000–200000)

## perform_web_search() now accepts kwargs
Threaded through (kw-only, defaults preserve legacy behaviour):
`fetch_pages`, `filter_pages`, `rerank`, `engines`, `max_chars_per_page`.
Engine submission filtered via `engine_allow` set. Step 5 / step 6 skip
when toggle False (logs `step5/6 skipped — caller passed ...=False`).

## Async wiring
Module-level `_search_runtime = TaskRuntime('search', ttl=1800, push_channel='search')`
in `routes/api_v1/agents.py`. Registered in `routes/api_v1/tasks.py::_registries`
so generic `/api/v1/tasks/{id}/*` endpoints find it.

## Capability advertisement
`routes/api_v1/capabilities.py::_agents_summary` lists `search` and
`search.async` entries.

## Smoke test
`debug/test_search_api.py` — uses Quart test client + monkeypatched
`perform_web_search` to validate route wiring without hitting engines.
Cannot be auto-run in environments lacking Python 3.

