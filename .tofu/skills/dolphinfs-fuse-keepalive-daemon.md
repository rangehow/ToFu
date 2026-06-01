---
name: dolphinfs-fuse-keepalive-daemon
description: DolphinFS FUSE mount keepalive daemon (lib/fs_keepalive.py) prevents mount staling during idle periods by periodic stat() probes every 15s; auto-activates only on /mnt/ paths; detects stale mounts with timeout-guarded probes
enabled: true
tags: [dolphinfs, fuse, keepalive, network, reliability, postgresql]
created: 2026-04-02T02:01:05Z
updated: 2026-04-02T02:01:05Z
---

# DolphinFS FUSE Keepalive Daemon

## Problem
When user disconnects (VS Code SSH / port-forwarding), DolphinFS (BeeGFS FUSE) mount goes idle.
After enough idle time, the kernel FUSE connection stales, causing ALL I/O on `/mnt/dolphinfs/...`
to block in D-state (uninterruptible sleep) for hours. Since PostgreSQL's `data/pgdata/` is on 
the same mount, task checkpoints and all DB operations freeze.

## Solution
`lib/fs_keepalive.py` — a daemon thread that `os.stat()`s the project directory every 15 seconds,
keeping the FUSE connection warm.

### Key design decisions:
- **15s interval**: Under typical FUSE idle-disconnect thresholds (30-120s)
- **Timeout-guarded probes**: Each `stat()` runs in a sub-thread with 30s timeout — if the mount
  is already frozen, the keepalive detects and logs it instead of hanging itself
- **Auto-activation**: Only starts if `_BASE_DIR.startswith('/mnt/')` — no-op on local disk
- **Consecutive failure tracking**: Logs "mount frozen" on first failure, periodic updates every
  10 failures, and "mount recovered" when it comes back
- **Probes 3 paths**: `data/`, `data/pgdata/`, `logs/` — covers DB + logs

### Wired in server.py:
```python
# After _start_background_workers()
from lib.fs_keepalive import start_fs_keepalive
start_fs_keepalive()
```

## Limitation
This **cannot fix** an already-stale mount. The keepalive is preventive — it keeps the connection
from going idle in the first place. If the underlying network is truly down (not just idle), 
the stat() probes will also block/timeout, but at least the condition is logged.

