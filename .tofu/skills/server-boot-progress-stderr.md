---
name: server-boot-progress-stderr
description: server.py prints boot progress directly to stderr via _boot() since console log handler is WARNING-only
enabled: true
tags: [server, startup, logging, ux]
created: 2026-04-23T03:33:21Z
updated: 2026-04-23T03:33:21Z
---

# server.py Boot Progress

The console log handler (`_console_handler`) is set to WARNING in
server.py, so `logger.info(...)` calls during boot are NOT visible in
the terminal — they only land in `logs/app.log`.

This previously made `python3 server.py` look frozen for 10–30s during
DB init on FUSE, critical-import validation (trafilatura/pymupdf), and
cross-DC probing.

Fix (2026-04-23): added a `_boot(msg, *args)` helper right after the
logging setup block. It writes to `sys.stderr` in cyan with an elapsed
timestamp AND mirrors to `server.boot` logger so the audit trail stays
in `logs/app.log`. Instrumented at each major stage:

- module load start (`🫧 Tofu starting up…`)
- DB init begin/end
- critical-imports begin + per-module line + end
- lock acquired
- background workers
- MCP auto-connect config
- FS keepalive / cross-DC detection
- Feishu bot check
- final "Ready — handing off to Werkzeug"

The banner now also includes `⏱  Boot time: N.Ns`.

If you add a new slow startup stage, sprinkle a `_boot(...)` before it
so users don't think the server is hung.

