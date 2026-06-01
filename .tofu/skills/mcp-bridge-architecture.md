---
name: mcp-bridge-architecture
description: MCP bridge: backend (lib/mcp/), routes/api_v1/mcp.py, App Store frontend, curated registry of 43 servers, auto-connect on startup. TERMINOLOGY: MCP = tools (function calling), NOT skills (prompt engineering)
enabled: true
tags: [mcp, architecture, tools, integration]
created: 2026-04-08T00:49:25Z
updated: 2026-05-29T03:17:03Z
---

# MCP Bridge Architecture

## Critical Terminology Distinction
- **MCP** = Tool invocation (function calling via JSON-RPC protocol)
- **Skill** = Prompt engineering (natural language instructions in SKILL.md that guide LLM behavior)
- These are COMPLETELY DIFFERENT concepts — never conflate them
- In our codebase: MCP code uses "server" / "app" terminology, NEVER "skill"

## File Structure
- `lib/mcp/__init__.py` — exports `get_bridge()`, `MCPBridge`
- `lib/mcp/types.py` — constants, TypedDicts, namespace helpers (`mcp__server__tool`)
- `lib/mcp/config.py` — persistent config CRUD for `data/config/mcp_servers.json`
- `lib/mcp/client.py` — core bridge: async event loop, lifecycle, tool discovery, dispatch
- `lib/mcp/registry.py` — curated catalog of **43** popular MCP servers (App Store style, internal total; opensource exports strip `hope` → 42)
- `lib/tasks_pkg/handlers/mcp.py` — ToolRegistry fallback handler for `mcp__` prefixed tools
- `routes/api_v1/mcp.py` — REST API (6 basic + 3 catalog endpoints; migrated from `routes/mcp.py` on 2026-05-29)

## Key Design Decisions
- Dedicated daemon thread runs asyncio event loop (Flask is sync, MCP SDK is async)
- Tool names use `mcp__{server}__{tool}` double-underscore namespace
- ToolRegistry.lookup() monkey-patched with MCP fallback
- MCP tools auto-injected into `_assemble_tool_list()` in model_config.py
- Frontend: App Store grid with categories, search, one-click install modal
- UI terminology: "Apps" (not "Skills" or "Servers") for user-facing labels

## Auto-connect on Startup
- `server.py` reads `data/config/mcp_servers.json` at startup
- If enabled servers exist, spawns background thread calling `bridge.connect_all()`
- This means MCP tools are available immediately without manual reconnection
- Banner shows MCP status: `🔌  MCP Apps: N server(s) auto-connecting`

## API Endpoints (post-migration 2026-05-29)
- GET `/api/v1/mcp/servers` — list configured servers
- POST `/api/v1/mcp/servers` — add/update a server config
- DELETE `/api/v1/mcp/servers/<name>` — remove
- POST `/api/v1/mcp/connect` — connect all or specific server
- POST `/api/v1/mcp/disconnect` — disconnect
- GET `/api/v1/mcp/tools` — list discovered tools
- GET `/api/v1/mcp/catalog` — curated catalog with install status
- POST `/api/v1/mcp/catalog/install` — one-click install + connect
- POST `/api/v1/mcp/catalog/uninstall` — disconnect + remove config (soft by default; `purge: true` for hard delete)

## Registry Categories (43 servers internally; 42 in opensource)
- Development: GitHub, GitLab, Git, Linear, Playwright, Context7, Jira
- Data & DB: PostgreSQL, SQLite, Redis, MongoDB, Supabase, Upstash, BigQuery
- Communication: Slack, Gmail, Discord
- Search & Web: Brave Search, Tavily, Exa, Fetch, Firecrawl, Puppeteer, Perplexity
- Productivity: Notion, Todoist, Google Drive, Asana, Zapier
- DevOps: Docker, Kubernetes, Sentry, Cloudflare, Vercel, AWS, **Hope** (internal only)
- Finance: Stripe
- Design: Figma
- Science & Research: Overleaf
- Other: Memory, Sequential Thinking, Filesystem, MCP Compass

## Special Config Handling in build_server_config
- Filesystem: dirs → CLI args; Postgres/SQLite/Redis/MongoDB: connection → CLI args
- Zapier: SSE transport, `ZAPIER_MCP_URL` env var becomes the URL
- Supabase: `SUPABASE_ACCESS_TOKEN` → `--access-token` CLI arg

## Internal-only entries (Meituan Sankuai)
- **Hope** (`id: 'hope'`, CAT_DEVOPS) — wraps `hope-mcp` package (sibling repo at `/mnt/.../hope-mcp`, `pip install -e .`). Env: `HOPE_BIN`, `HOPE_MCP_TIMEOUT`, `HOPE_MCP_MAX_PARALLEL`, `HOPE_MCP_DRY_RUN_DEFAULT`. Block lives between `# ── Meituan Internal` banner and `    },\n` outer close (4-space indent). `export.py` §14 strips the entire block on opensource mode (regex anchored on banner + 4-space indent).

