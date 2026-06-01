---
name: chatui-test-suite-architecture
description: ChatUI test suite: 525+ tests (unit+API+visual), GitHub Actions CI with PG service container, mock LLM server, ruff lint, dedicated cache breakpoint regression tests
enabled: true
tags: [testing, pytest, playwright, visual, mock-server, e2e, architecture]
created: 2026-03-24T08:44:40Z
updated: 2026-04-06T07:50:33Z
---

# ChatUI Test Suite & CI Architecture

## Test Suite
- **525+ tests** across 3 categories: `unit`, `api`, `visual`
- Tests live in `tests/` with markers in `pyproject.toml`
- Run: `python -m pytest -m unit`, `python -m pytest -m api`, `python -m pytest -m visual`
- All tests use a **mock LLM server** (no real API keys needed)

## Key Test Files
| File | What it tests |
|---|---|
| `test_cache_breakpoints.py` | **32 tests** — BP4 tail placement, empty-content skip, backward scan, phase-0 stripping, multi-round simulation, breakpoint limit, regression guard for mnk84kthdr2x08 bug |
| `test_cache_improvements.py` | **28 tests** — TTL detection fix, concurrent conv tracking, cache stats logging, TTL latch, tool result ordering, cleanup |
| `test_new_features.py` | Session memory, attachments, cache break detection, tool hooks, tool spec, dynamic deferral, partial compaction |
| `test_cc_alignment.py` | Claude Code alignment features incl. per-block cache control |
| `test_compaction_improvements.py` | Micro-compact, reactive compact, budget thresholds |
| `test_endpoint_messages.py` | API endpoint request/response validation |
| `test_streaming_and_prefetch.py` | SSE streaming, prefetch, retry logic |

## Key Fixtures
- `_disable_extended_ttl` (autouse in test_cache_breakpoints, test_cc_alignment, test_cache_improvements) — sets `CACHE_EXTENDED_TTL = False` so cache_control assertions are stable
- `_clean_cache_state` (autouse in test_cache_improvements) — clears `_cache_states` and `_ttl_latch` between tests

## CI Pipeline (`.github/workflows/ci.yml`)
```
lint (ruff) ─┬─► test-unit (Python 3.10 + 3.12, with coverage)
             ├─► test-api (PostgreSQL service container)
             ├─► healthcheck
             └─► test-visual (E2E, main-only, non-blocking)
```

## Critical: PostgreSQL in CI
- API tests need PostgreSQL — CI uses `services: postgres:17` Docker container
- **Must set env vars**: `CHATUI_PG_HOST=localhost`, `CHATUI_PG_PORT=5432`, `CHATUI_PG_USER`, `CHATUI_PG_PASSWORD`, `CHATUI_PG_DBNAME`
- The bootstrap module (`lib/database/_bootstrap.py`) detects explicit `CHATUI_PG_PORT` env var and connects via psycopg2 directly (no `pg_isready`/`initdb` binaries needed)

## Critical: Test Model Names
- **NEVER use** `from lib import QWEN_MODEL, DOUBAO_MODEL, LLM_MODEL` in tests
- These are env-dependent and empty in CI (no `data/config/server_config.json`)
- **Always use hardcoded model names**: `'qwen-plus'`, `'doubao-seed-1-6'`, `'claude-sonnet-4-20250514'`

## Critical: Export → Push Workflow
- **Always re-export BEFORE committing** — `python export.py --mode opensource --dest ../tofu-open`
- After export, verify key files: `grep "is_explicit_external" ../tofu-open/lib/database/_bootstrap.py`
- The export pipeline now runs `ruff --fix` automatically on exported code
- **Bug pattern**: `git reset --soft` squash uses working tree state, NOT latest source — must re-export first!

## Ruff Config
- Modern Python style: `X | None` not `Optional[X]`, `dict` not `Dict`
- UP007, UP035, UP045 are NOT in the ignore list (violations are auto-fixed on export)

