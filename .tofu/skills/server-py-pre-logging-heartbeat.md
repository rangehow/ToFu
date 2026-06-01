---
name: server-py-pre-logging-heartbeat
description: server.py emits earliest possible stderr heartbeat via os.write(2,...) before any import; _BOOT_T0 = _PROC_T0 so cold-FUSE import time is visible in [boot +N.Ns] counter
enabled: true
tags: [server, startup, ux, boot, fuse]
created: 2026-05-06T09:40:43Z
updated: 2026-05-06T09:40:43Z
---

# server.py: Pre-Logging Boot Heartbeat

## Problem
The first `_boot('🫧 Tofu starting up…')` line is around line 374 in
server.py. Before it runs, Python has to:
- run conda env reexec / bootstrap excepthook setup
- `from flask import …` + `from flask_compress import Compress`
- `mimetypes.init()`
- create 4 rotating log handlers (each opens files via FUSE)
- load filters / formatters

On cold FUSE/NFS this can be 10–30s of TOTALLY SILENT terminal time
after the user runs `python server.py`. Users panic and Ctrl-C.

## Fix (2026-05-06)
Two tiny changes near the top of `server.py`:

1. Right after `import os/io/sys/json/logging/time/hashlib`, set
   `_PROC_T0 = time.time()` and emit a stderr ping via raw
   `os.write(2, b'\033[36m[boot +  0.0s]\033[0m …')`. This bypasses
   any stderr buffering, uses zero non-stdlib imports, and prints
   within milliseconds of process start.

2. Later, change `_BOOT_T0 = time.time()` to `_BOOT_T0 = _PROC_T0`
   so subsequent `[boot +N.Ns]` lines from `_boot()` measure from the
   true process start, exposing the cold-FUSE import gap (previously
   hidden — _BOOT_T0 was set AFTER the slow imports finished).

## Why os.write(2, ...) and not print()
- print() goes through sys.stderr which may be wrapped by something
- bytes literal avoids any encoding setup
- only stdlib `os` is needed — works even before any other import

## Where
Top of server.py, right after the stdlib imports block. See the
"Earliest possible heartbeat" comment in the file.

## Related
- `server-boot-progress-stderr` memory — original _boot() helper.
- `pg-schema-version-cache-fast-startup` — the schema-cache miss is
  the OTHER source of slow startup (one-time after schema bumps).

