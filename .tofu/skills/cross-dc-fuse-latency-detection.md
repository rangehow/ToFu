---
name: cross-dc-fuse-latency-detection
description: Cross-datacenter FUSE filesystem latency detection: env-var driven auto-benchmark, configurable thresholds, timeout multipliers, integrated into 7 tool dispatch points
enabled: true
tags: [infrastructure, performance, fuse, cross-dc]
created: 2026-04-06T14:17:51Z
updated: 2026-04-06T14:17:51Z
---

# Cross-Datacenter FUSE Latency Detection

## Module: `lib/cross_dc.py`

### How it works
1. Reads cluster mount mappings from env vars (`CROSS_DC_CLUSTER_MOUNTS` or `LIBBGFS_CLUSTERMOUNTPATHS`)
2. Benchmarks each cluster with stat() on non-existent paths (bypasses FUSE cache)
3. Classifies clusters: local (<10ms), slow (10-30ms, 3× timeout), very_slow (>30ms, 5× timeout)
4. All thresholds configurable via `data/config/cross_dc.json`

### Env vars
- `CROSS_DC_CLUSTER_MOUNTS` — format: `cluster1:/path/a,cluster2:/path/b,...`
- `CROSS_DC_LOCAL_IDC` — local datacenter identifier
- Falls back to `LIBBGFS_CLUSTERMOUNTPATHS` and `HULK_IDC`

### Config file: `data/config/cross_dc.json`
```json
{
  "cluster_mounts_env": "MY_CUSTOM_ENV",
  "local_idc_env": "MY_IDC_VAR",
  "slow_threshold_ms": 10,
  "very_slow_threshold_ms": 30,
  "slow_timeout_multiplier": 3,
  "very_slow_timeout_multiplier": 5,
  "enabled": true
}
```

### Integration points (7 places)
1. `server.py` — `init_cross_dc_detection()` in background thread
2. `lib/project_mod/tools.py` — `run_command` timeout adjustment
3. `lib/project_mod/read_tools.py` — `_get_io_timeout()` for grep/find
4. `lib/project_mod/scanner.py` — `set_project()` warning
5. `lib/project_mod/indexer.py` — LLM system prompt warning
6. `lib/project_mod/config.py` — `get_state()` crossDC indicator
7. `routes/common.py` — `/api/health` cross-DC status

### CRITICAL: No hardcoded paths
- Zero hardcoded paths, hostnames, or cluster names in code
- On machines without env vars, module is a complete no-op (all defaults safe)
- Documented in CLAUDE.md §3.5

