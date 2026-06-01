---
name: hope-mcp-test-strategy
description: hope-mcp test layering: unit + edge + server + stdio E2E + real-hope smoke
enabled: true
tags: [hope-mcp, testing, mcp, pytest]
created: 2026-04-20T03:58:31Z
updated: 2026-04-20T03:58:31Z
---

# hope-mcp Test Strategy (34 tests, 5 layers)

Repo: `/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/hope-mcp`

## Layered test suite

| File | Tests | Layer | Purpose |
|---|---|---|---|
| `test_cli.py` | 4 | Unit | Async subprocess wrapper: arg passthrough, non-zero exit, JSON parse, missing binary |
| `test_jobs.py` | 9 | Unit | Tool logic: submit/stop/batch-stop, dedup, dry-run default, partial failure |
| `test_cli_edge.py` | 10 | Edge | Timeout, JSON-with-banner/trailer, semaphore throttling, workdir size guard, force override, stop_on_error, numeric ID coercion |
| `test_server.py` | 7 | MCP server | Direct dispatch via `server.request_handlers` — schema validity, unknown tool, additionalProperties=false rejection, end-to-end each tool |
| `test_stdio_integration.py` | 1 | Full E2E | Spawn `python -m hope_mcp` subprocess, mcp `ClientSession` over stdio, initialize → list_tools → call_tool(3×). Highest fidelity. |
| `test_real_hope.py` | 3 | Smoke | Auto-skips if `hope` not on PATH. Runs `hope --help`, `--version`, and a bogus `stop` (proves timeout rail works on real Thrift hangs) |

## Run

```bash
cd /mnt/.../hope-mcp
PYTHONPATH=src python -m pytest tests/ -v
# Fast path (skip real hope that may be slow):
PYTHONPATH=src python -m pytest tests/ --ignore=tests/test_real_hope.py
# Short timeout for real-hope to avoid hangs:
PYTHONPATH=src HOPE_MCP_TIMEOUT=5 python -m pytest tests/
```

## Key quirks discovered during testing

1. **mcp SDK attribute is `request_handlers`** (public), not `_request_handlers`. Keys are `mcp.types.CallToolRequest` / `ListToolsRequest` / `PingRequest` classes.
2. **Build request instances** via `CallToolRequest(method='tools/call', params={'name':..., 'arguments':{...}})`. `params` auto-coerces to `CallToolRequestParams`.
3. **`additionalProperties=false` on tool schemas** means the SDK validates unknown kwargs BEFORE our handler runs — the error text comes back as plain text ("Input validation error: Additional properties..."), not as our JSON `{ok:false, error:...}`. Test must accept both paths.
4. **Fake hope shim in `conftest.py`** appends `CMD_ARGS=[...]` to stdout, which IS valid JSON (an array). Tests that need pure non-JSON output must build their own shim (see `test_json_parse_returns_none_on_pure_text`).
5. **`hope status` / `hope stop --runid=...` can hang** in Thrift on this env. The timeout rail is non-optional — set `HOPE_MCP_TIMEOUT=5` when running real-hope tests to keep suite fast.
6. **Semaphore throttling test**: launch 8 concurrent calls with 0.3s shim sleep, cap=2 → expect elapsed ≥1.0s to prove serialization. Generous bound avoids flakes on slow FS.

## Test-time config knobs

- `HOPE_BIN` — redirect to fake shim (monkeypatch + `importlib.reload(cfg)` + `importlib.reload(cli)` to invalidate module-cached CONFIG)
- `HOPE_FAKE_STDOUT` / `HOPE_FAKE_STDERR` / `HOPE_FAKE_RC` — shim injection
- `HOPE_MCP_MAX_PARALLEL` — semaphore cap for concurrency tests
- `HOPE_MCP_TIMEOUT` — wrapper timeout

