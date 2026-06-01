---
name: cross-platform-installer-architecture
description: Installer architecture: install.sh (Linux/macOS), Tofu-Setup-*.exe (Windows GUI installer), Docker
enabled: true
tags: [installer, cross-platform, docker, deployment]
created: 2026-04-04T11:57:22Z
updated: 2026-06-01T02:20:01Z
---

# Installer Architecture (post-2026-06 reorg)

## Files
| File | Purpose | Platform |
|---|---|---|
| `install.sh` | Standalone bash installer (~1430 lines, full conda-forge install) | Linux, macOS |
| `Tofu-Setup-x.y.z-win64.exe` | Inno Setup GUI installer built by `.github/workflows/build-desktop.yml` | Windows |
| `Dockerfile` + `docker-compose.yml` | Container deployment | Any host with Docker |

## Removed (2026-06)
- `install.py` — was the cross-platform Python installer; deleted.
- `install.ps1` — was a thin PowerShell wrapper that delegated to `install.py`; deleted.

Rationale: too many install paths confused users. Windows users now download
the `.exe` from GitHub Releases (already produced by the existing
`build-desktop.yml` PyInstaller + Inno Setup pipeline). Linux/macOS still
use `install.sh`. Docker still works.

## install.sh Design Principles
1. **Conda-forge only** — avoids manylinux GLIBC trap on CentOS 7 hosts
2. **No `sudo`, no system packages** — everything via conda-forge
3. **Idempotent** — safe to re-run
4. **Sibling Miniforge fallback** — drops a private Miniforge next to the project if the user's conda is broken
5. **`--override-channels` everywhere** — see `conda-override-channels-solver-fix` memory

## User-facing flags (only 4 advertised)
- `--api-key sk-xxx`
- `--port 8080`
- `--dir <path>`
- `--no-launch`

All other flags (`--reset-env`, `--force-sqlite`, `--reinit-pgdata`,
`--force-sibling-conda`, …) are documented in `docs/INSTALL.md` only as
troubleshooting recipes.

## One-liner UX
- Linux/macOS: `curl -fsSL .../install.sh | bash`
- Windows: download `Tofu-Setup-*.exe` from Releases, double-click
- Docker: `git clone ... && docker compose up -d`

## Don't reintroduce
- A separate Python installer file. The desktop bundle pipeline already
  produces a Windows GUI installer; that's the Windows path.
- A PowerShell installer that does conda gymnastics. Same reason.

