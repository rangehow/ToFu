---
name: cross-dc-dolphinfs-detection-optimization
description: Cross-datacenter DolphinFS FUSE detection: lib/cross_dc.py parses LIBBGFS_CLUSTERMOUNTPATHS, benchmarks via non-existent path stat(), auto-adjusts timeouts 3-5x for remote clusters
enabled: true
tags: [dolphinfs, fuse, cross-dc, latency, performance, beegfs]
created: 2026-04-06T14:13:00Z
updated: 2026-04-06T14:13:00Z
---

# Cross-Datacenter DolphinFS Detection & Optimization

## Problem
DolphinFS (BeeGFS FUSE) mounts paths from multiple BeeGFS clusters across datacenters.
When chatui server is in DC-A but project path is served by DC-B cluster, every file I/O
has cross-DC latency: stat ~20-35ms (vs <1ms local), readdir ~150ms, find ~12s (vs 20ms).

## Architecture
`lib/cross_dc.py` — Fully automatic detection module:

1. Parses `LIBBGFS_CLUSTERMOUNTPATHS` env var for cluster→mount path mapping
2. Reads `HULK_IDC` env var for local datacenter identifier  
3. Benchmarks each cluster using `stat()` on **non-existent paths** (bypasses FUSE cache)
4. Classifies: local (<10ms), slow (10-30ms, ×3 timeout), very_slow (>30ms, ×5 timeout)
5. Provides API: `is_cross_dc(path)`, `get_timeout_multiplier(path)`, `cross_dc_warning(path)`

### Key Design Decisions
- **Non-existent path probing**: `stat(existing_path)` returns ~0ms due to FUSE cache.
  Must use `stat(path/_probe_random123)` which triggers metadata server RPC
- **Median of 3 probes**: Reduces noise from network jitter
- **Background init**: Benchmarking runs in daemon thread to not block server startup
- **1 hour TTL**: Re-benchmarks periodically as network conditions change

## Integration Points
- `server.py`: `init_cross_dc_detection()` started after FS keepalive
- `lib/project_mod/tools.py` → `tool_run_command()`: Auto-adjusts timeout by multiplier
- `lib/project_mod/read_tools.py`: `_get_io_timeout()` adjusts grep/find/fd timeouts
- `lib/project_mod/scanner.py` → `set_project()`: Logs warning for cross-DC paths
- `lib/project_mod/indexer.py`: Injects cross-DC warning into LLM system prompt
- `lib/project_mod/config.py` → `get_state()`: Adds `crossDC` field to project state
- `routes/common.py` → `/api/health`: Includes cross-DC status in health check
- `static/js/project.js`: Shows latency indicator (🐢/⚡) in project bar

## Measured Latencies (from zw05 datacenter)
| Cluster | stat() | listdir() | Classification |
|---------|--------|-----------|----------------|
| training4 (local) | 0.3ms | 1.6ms | LOCAL |
| training3 (local) | 0.4ms | 3.2ms | LOCAL |
| hldy-training | 20ms | 152ms | SLOW (×3) |
| hlsc-training | 22ms | 124ms | SLOW (×3) |
| sh02-training | 35ms | 36ms | VERY SLOW (×5) |

## Test
```bash
python3 debug/test_cross_dc.py
```

