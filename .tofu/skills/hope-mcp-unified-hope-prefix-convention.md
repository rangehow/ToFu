---
name: hope-mcp-unified-hope-prefix-convention
description: v0.6.1: unified hope_ prefix for ALL MCP tools. Bare mlp_ prefix retired to avoid LLM confusion.
enabled: true
tags: [hope-mcp, naming, mlp-port]
created: 2026-05-07T03:15:24Z
updated: 2026-05-07T03:15:24Z
---

# hope-mcp naming: ALL tools start with `hope_` (v0.6.1)

## Why
Two prefixes (`hope_*` vs `mlp_*`) confused the LLM when picking tools —
it had to reason about which backend a task belonged to before it could
even see the candidate list. Since hope-mcp is the sole MCP server
hosting both, they should share one prefix.

## Rule
* `hope_X` — hope-template backend (hope cluster control plane)
* `hope_mlp_X` — MLP platform backend (MLP observation plane)
* NO bare `mlp_` prefix anywhere — that would break the rule again

## What changed in v0.6.1
Mechanical rename across three files:
* `src/hope_mcp/server.py` — 56 tool-name / dispatch-key literals
* `src/hope_mcp/tools/mlp.py` — 20 `_ensure_mlp_logged_in(tool_name=...)` calls
* `tests/test_server.py`, `tests/test_mlp.py` — 30 assertion strings

Python INTERNAL names (functions, modules, file paths) were NOT
touched:
* File `tools/mlp.py` stays (internal)
* Function `mlp.mlp_run_list()` stays (internal)
* Module `hope_mcp.mlp_api` stays (internal)
* Env vars `MLP_TOKEN_FILE`, `MLP_PROJECT`, `MLP_BIN`,
  `MLP_SSO_CLIENT_ID` stay (external convention matches upstream CLI)

## One-liner to verify the invariant
```python
from hope_mcp.server import TOOLS
names = sorted(t.name for t in TOOLS)
assert all(n.startswith("hope_") for n in names)
assert not any(n.startswith("mlp_") and not n.startswith("hope_mlp_") for n in names)
```

## Tool inventory (v0.6.1)
25 + 28 = 53 tools, all `hope_`-prefixed.

* hope-native (25): submit_job, init_job, upload_code, run_job, quickrun,
  stop_session, stop_job, stop_job_verified, stop_jobs_batch, get_status,
  get_status_batch, watch_job, get_task_attempts, list_resource,
  change_priority, get_run_log, get_metadata, list_supported_job_types,
  get_lion_config, fetch_source_code, dfs_ls, check_login, login, logout,
  describe_endpoints
* hope_mlp_* (28): login, whoami, logout, describe_endpoints,
  set_project, config_get, config_set, config_unset, run_list, run_info,
  run_jobs, run_sub_jobs, job_info, job_list, job_attempts,
  job_aggr_info, job_aggr_metric, job_aggr_hyper, job_aggr_custom,
  job_stop, log_files, log_get, job_series_tags, job_series_data,
  queue_detail, queue_applications, codelab_queue_list, project_list

## Collision resolution
Two tools that were previously at risk of confusion now have unambiguous
names:
* `hope_login` (hope cluster SSO) ≠ `hope_mlp_login` (MLP SSO)
* `hope_logout` / `hope_describe_endpoints` similarly paired

