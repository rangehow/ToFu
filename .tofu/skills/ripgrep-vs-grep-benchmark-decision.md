---
name: ripgrep-vs-grep-benchmark-decision
description: Ripgrep 5x faster than GNU grep on our NFS codebase (1357 files, 16MB); auto-detected at module load with grep/python fallback chain
enabled: true
tags: [performance, grep, ripgrep, tools]
created: 2026-04-06T09:35:58Z
updated: 2026-04-06T09:35:58Z
---

# Ripgrep vs GNU Grep — A/B Test Results (2026-04-06)

## Benchmark Data (20 iterations each, interleaved, OS cache warm)

| Test | grep (ms) | rg (ms) | Speedup |
|---|---|---|---|
| Simple literal | 311.54 | 50.05 | 6.22x |
| Function name lookup | 344.20 | 49.18 | 7.00x |
| Include filter (*.py) | 170.57 | 42.62 | 4.00x |
| Regex pattern | 174.60 | 42.38 | 4.12x |
| Rare pattern | 334.01 | 48.55 | 6.88x |
| Context lines (-C 3) | 176.06 | 42.71 | 4.12x |
| Wide pattern (many matches) | 171.40 | 43.03 | 3.98x |
| Full project scan | 331.83 | 49.37 | 6.72x |
| Non-existent pattern | 255.63 | 50.05 | 5.11x |
| JS file search | 69.00 | 35.89 | 1.92x |
| **TOTAL** | **2338.84** | **453.83** | **5.15x** |

ripgrep wins **10/10** tests. In-tool measurement: **190ms vs 1294ms (6.8x)**.

## Key Behavioral Differences
- rg auto-respects `.gitignore` → skips `.project_sessions/`, caches, etc. (actually better)
- rg auto-skips binary files (no `-I` flag needed)
- rg does NOT follow symlinks by default (use `-L` if needed)
- Match counts differ slightly due to .gitignore respect (desirable for project search)

## Implementation
- `lib/project_mod/read_tools.py`: `_HAS_RG = shutil.which('rg')` at module load
- Fallback chain: `rg` → `grep` → `_python_grep`
- `pip install ripgrep` provides the binary
- Already in `_READONLY_COMMANDS` whitelist in `tools.py`

## Benchmark script
- `debug/bench_grep_vs_rg.py` — 10 test patterns, 20 iterations each, interleaved order

