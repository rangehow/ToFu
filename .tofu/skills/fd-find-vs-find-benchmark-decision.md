---
name: fd-find-vs-find-benchmark-decision
description: fd-find 3-4x faster than GNU find in raw benchmark, 1.3x in tool_find_files (getsize overhead); auto-detected with os.walk fallback
enabled: true
tags: [performance, tools, benchmark]
created: 2026-04-06T11:06:46Z
updated: 2026-04-06T11:06:46Z
---

# fd-find vs GNU find vs Python os.walk — Benchmark Decision

## Raw Benchmark (NFS-backed SSD, 3462 files)
| Tool | Median (*.py search) | Notes |
|------|---------------------|-------|
| fd-find | 31ms | 3-4x faster than find, parallel dir walking |
| Python os.walk | 40ms | No subprocess overhead, but single-threaded |
| GNU find | 125ms | Slowest in all tests |

## In-tool Measurement (tool_find_files with getsize)
- fd: 19ms, Python: 26ms → **1.3x speedup** (getsize per file eats the margin)
- Small dirs (<100 files): Python os.walk wins (no subprocess overhead)

## Architecture
- `_FD_BIN` detected at module load via `shutil.which('fd') or shutil.which('fdfind')`
- fd → Python os.walk fallback chain
- fd uses `--max-results N` for native early termination
- `fdfind` is the Debian package name (added to READONLY_COMMANDS)

## Key differences from ripgrep adoption
- ripgrep gave 5x speedup because grep is CPU-bound (regex matching)
- fd gives less speedup because find is I/O-bound (directory traversal) and getsize adds per-file stat calls
- On larger projects (10k+ files) fd's parallel walking should shine more

## Installation
- conda: `conda install -c conda-forge fd-find`
- brew: `brew install fd`
- apt: `sudo apt install fd-find` (binary is `fdfind`, not `fd`)
- cargo: `cargo install fd-find`

