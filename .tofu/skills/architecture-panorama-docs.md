---
name: architecture-panorama-docs
description: Where the canonical architecture diagram / panorama lives, and how to re-generate it when drift occurs
enabled: true
tags: [architecture, docs, convention]
created: 2026-05-03T05:26:52Z
updated: 2026-05-03T05:26:52Z
---

# Architecture Panorama — Source of Truth

Created 2026-05-03 in response to a request for a Claude-Code-style architecture diagram.

## Two authoritative files
1. **`docs/ARCHITECTURE.md`** — Markdown + Mermaid, drift-checked directory map.
   This is the **source of truth** for the structure.  Whenever any sub-package,
   `tasks_pkg/` module, Blueprint, or `static/js/` file is added/removed, this file
   must be updated (see its own §5 "Drift-check protocol").
2. **`docs/architecture.html`** — Single-file HTML panoramic diagram in the style
   of Claude Code's 架构全景 image (layered coloured cards + icon + module name +
   bullet list).  Use for screenshots / social posts / 文生图 model inputs.
   Views best at 1400px wide.

## Layers (for consistency across docs + diagrams)
1. 入口层 Entry — Web UI · Feishu · Browser ext. · Desktop agent · MCP · Agent backends (Claude Code / Codex)
2. routes/ — 27 Blueprints, 243 routes, trading_* gated by TRADING_ENABLED
3. 核心引擎 Core — `tasks_pkg/manager` · `orchestrator` · `endpoint` · `swarm/master`
4. 安全 Safety — `approval.py` · `tool_hooks` · `DANGEROUS_PATTERNS` · `oauth/` · `proxy` · `export.py`
5. 上下文 Context — `system_context` · `compaction` · `memory/` · `conv_message_builder` · `attachments`
6. 工具 Tools — `lib/tools/` definitions → `tool_dispatch` → `executor` → `handlers/`
7. 模型调度 LLM Dispatch — `llm_dispatch/{dispatcher,api,config,discovery,slot,factory}` + `llm_client`
8. 基础设施 Infra — `database/` (PG→SQLite) · `log.py` · `compat` · `cross_dc` · `bootstrap`
9. 运营进化 Ops — `optimizer/` (nightly LLM-driven self-tuning) · `scheduler/` · `daily_report` · audit_log

## CLAUDE.md was drift-corrected in the same turn
- Added `lib/optimizer/` to §1 (it was missing).
- Expanded `tasks_pkg/` inventory — the real package has 26 files, not the
  ~10 previously listed (added cache_tracking, llm_fallback, stream_handler,
  system_context, conv_message_builder, server_message_store, model_config,
  attachments, approval, human_guidance, stdin_handler, tool_display, tool_hooks).
- Added `routes/optimizer.py` to Blueprint list.
- Swarm now has 18 files — previous CLAUDE.md didn't list them.
- Added pointer to `docs/ARCHITECTURE.md` + `docs/architecture.html` at the top of §1.

## When a future session asks "generate an architecture diagram"
Default workflow:
1. Read `docs/ARCHITECTURE.md` first — don't re-derive from scratch.
2. If the request is for an image: regenerate `docs/architecture.html` (fast to screenshot).
3. If the request is for a text/Markdown diagram: use the Mermaid block in `ARCHITECTURE.md`.
4. If the filesystem has drifted (new package, new Blueprint, new `tasks_pkg/` module):
   - Run `list_dir('lib'); list_dir('routes'); list_dir('lib/tasks_pkg')` to re-inventory.
   - Update §3 of `ARCHITECTURE.md` AND the matching card in `architecture.html`.
   - Bump the "Last re-scanned" line at the top of `ARCHITECTURE.md`.

