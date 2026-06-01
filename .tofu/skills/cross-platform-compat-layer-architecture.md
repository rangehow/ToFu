---
name: cross-platform-compat-layer-architecture
description: Cross-platform (Linux/macOS/Windows) compat layer architecture: lib/compat.py helpers, platform guards, PG binary finder, fs_keepalive skip, shlex.split posix mode
enabled: true
tags: [cross-platform, windows, macos, compat, architecture]
created: 2026-04-02T03:10:35Z
updated: 2026-04-02T06:45:35Z
---

# Cross-Platform Compatibility Layer

## Architecture

All platform-specific code is centralized in `lib/compat.py`. Other modules import helpers from there.

### Platform Detection Flags
```python
from lib.compat import IS_WINDOWS, IS_MACOS, IS_LINUX, HAS_PROCFS
```

### Key Helpers
| Helper | Purpose |
|---|---|
| `get_shell_args(cmd)` | Returns `['/bin/sh', '-c', cmd]` or `['cmd.exe', '/c', cmd]` |
| `get_username()` | Cross-platform username lookup |
| `get_temp_dir()` | Platform-appropriate temp directory |
| `is_process_alive(pid)` | Works on all platforms |
| `set_pipe_nonblocking(fd)` | Uses `fcntl` on Unix, no-op on Windows |
| `safe_select_pipes(fds, timeout)` | Uses `select` on Unix, polling on Windows |
| `safe_signal(signum, handler)` | Silently skips unavailable signals |
| `is_network_mount(path)` | Detects FUSE/network mounts per platform |
| `safe_shlex_split(cmd)` | Uses `posix=False` on Windows for correct path handling |

### Rules
1. **Never `import fcntl` or `import select` outside `lib/compat.py`**
2. **Never access `/proc/` without `HAS_PROCFS` guard**
3. **Never use bare `/bin/sh` — use `get_shell_args()`**
4. **Never use `shlex.split()` directly — use `safe_shlex_split()` or pass `posix=not IS_WINDOWS`**
5. **PG binaries** use `_find_pg_binary()` in `lib/database/_bootstrap.py` (checks PATH + Windows install dirs)
6. **`lib/fs_keepalive.py`** is a no-op on non-Linux (graceful debug-level skip)
7. **`DANGEROUS_PATTERNS`** in config.py include both Unix (`rm -rf`) and Windows (`del /s /q`, `format C:`) equivalents

### Smoke Test
```bash
python debug/test_cross_platform.py  # 29 checks, any platform
```

