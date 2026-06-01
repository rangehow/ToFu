---
name: settings-hot-reload-architecture
description: How settings hot-reload works: import lib as _lib pattern, reload_config(), and save_server_config flow for live config changes without restart
enabled: true
tags: [architecture, settings, hot-reload, config]
created: 2026-04-02T15:28:04Z
updated: 2026-04-02T15:28:04Z
---

# Settings Hot-Reload Architecture

## Problem
`lib/__init__.py` defines module-level config variables (LLM_MODEL, FETCH_TOP_N, etc.).
When consuming modules use `from lib import X`, they get a **copy** of the value at import time.
Changing `lib.X` later does NOT update those copies (Python binding semantics for immutables).

## Solution: Module Reference Pattern

### Import Pattern
All consuming files use:
```python
import lib as _lib  # module ref for hot-reload
```
Then reference values as `_lib.LLM_MODEL`, `_lib.FETCH_TOP_N`, etc.
Since `_lib` is a reference to the module object, `_lib.X` always reads the **current** attribute.

### Default Parameter Gotcha
```python
# ❌ BAD — default captured at definition time
def fetch(url, timeout=_lib.FETCH_TIMEOUT): ...

# ✅ GOOD — resolve at call time
def fetch(url, timeout=None):
    if timeout is None: timeout = _lib.FETCH_TIMEOUT
```

### reload_config() in lib/__init__.py
Central function that re-reads `server_config.json` and updates all module-level vars:
- Called by `save_server_config()` in `routes/config.py` after writing config to disk
- Updates: LLM_MODEL, API keys, base URL, all model names, fetch settings, TRADING_ENABLED, etc.
- Logs what changed for debugging

### Save Flow
1. Frontend POSTs to `/api/server-config`
2. `save_server_config()` merges data into existing config and writes to disk
3. Calls `_lib.reload_config()` — updates all module-level variables
4. If provider/model config changed, calls `reset_dispatcher()` — rebuilds LLM slot pool
5. Special handlers: `_hot_reload_feishu()` for Feishu state, content filter flag, proxy config
6. Returns `needs_restart: false` always

### Files Changed to Module-Ref Pattern
- lib/llm_client.py (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL)
- lib/fetch/core.py (FETCH_TIMEOUT, FETCH_MAX_CHARS_*, FETCH_TOP_N)
- lib/fetch/http.py (FETCH_MAX_BYTES)
- lib/fetch/utils.py (FETCH_MAX_CHARS_*, SKIP_DOMAINS, FETCH_TOP_N)
- lib/search/orchestrator.py (FETCH_TOP_N)
- lib/tasks_pkg/executor.py (FETCH_MAX_CHARS_*, FETCH_TIMEOUT)
- lib/tasks_pkg/model_config.py (LLM_MODEL, QWEN_MODEL, GEMINI_MODEL, etc.)
- lib/pricing.py (LLM_MODEL, MODEL_PRICING, DEFAULT_USD_CNY_RATE)
- lib/llm_dispatch/dispatcher.py (all model names, API keys)
- lib/project_mod/config.py (QWEN_MODEL)
- lib/project_mod/indexer.py (QWEN_MODEL, GEMINI_MODEL)
- routes/common.py (TRADING_ENABLED)
- lib/trading/brain/pipeline.py (LLM_MODEL)
- lib/trading_autopilot/cycle.py (LLM_MODEL)
- routes/__init__.py (TRADING_ENABLED)

### Exception: Late Imports Are Fine
Functions that do `from lib import X` inside their body (late imports) are already correct —
they read the current value each time the function runs. No change needed for these.
Example: `lib/llm_dispatch/dispatcher.py._build_slots_from_env()`, `lib/embeddings.py`, etc.

### TRADING_ENABLED Special Case
Blueprint registration happens at import time (Flask limitation).
Hot-reloading `TRADING_ENABLED` updates the API endpoint check but does NOT
register/unregister trading blueprints. Full route changes still need a restart.

