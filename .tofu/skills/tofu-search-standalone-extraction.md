---
name: tofu-search-standalone-extraction
description: Architecture of tofu-search standalone library: extracted from chatui's lib/search+lib/fetch, all lib.* deps replaced with config.py+llm_adapter.py, browser fallback removed
enabled: true
tags: [architecture, tofu-search, extraction, standalone]
created: 2026-04-06T08:20:23Z
updated: 2026-04-06T08:20:23Z
---

# tofu-search Standalone Library Architecture

## Location
`/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/tofu-search/`
(sibling to chatui/)

## Key Design Decisions

### Dependency Decoupling
- `lib.__init__` config constants → `tofu_search/config.py` (dataclass-based `SearchConfig` with `get_config()` singleton)
- `lib/log.py` → `tofu_search/log.py` (thin wrapper around `logging.getLogger`)
- `lib/llm_dispatch/api.py:dispatch_chat()` → `tofu_search/llm_adapter.py:call_llm()` (supports custom callable OR OpenAI-compatible API)
- `lib/compat.py:IS_LINUX` → inline `sys.platform == 'linux'` in playwright_pool.py
- `lib/pdf_parser/` → `tofu_search/fetch/pdf_extract.py` (self-contained with pymupdf)
- `lib/_pkg_utils.py` (chatui facade pattern) → simple explicit `__init__.py` imports

### Removed Features (chatui-specific)
- Browser extension fallback (`lib/browser/`) — requires WebSocket browser extension
- `lib/search/browser_fallback.py` — depends on `lib.browser`
- `lib/fetch/http.py:try_browser_fetch()` — removed entirely
- `lib/llm_client.py:ContentFilterError` handling — simplified to generic Exception

### Public API
```python
from tofu_search import search, fetch_url, configure, format_results
```

### LLM Integration Pattern
- `configure(llm_function=callable)` — custom callable: `(messages, **kwargs) -> str`
- `configure(llm_api_key=..., llm_base_url=..., llm_model=...)` — OpenAI-compatible
- No LLM configured → content filter silently skipped (raw text returned as-is)

### File Count: 27 files across tofu_search/, examples/, and root config

