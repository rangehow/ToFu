---
name: xuecheng-doc-title-cache-and-batch-display
description: lib/mcp/project_names.py now caches xuecheng contentId→title; tool_display batch lines render every query in full (newline-separated)
enabled: true
tags: [mcp, tool-display, xuecheng]
created: 2026-05-08T11:43:09Z
updated: 2026-05-08T11:43:09Z
---

# Xuecheng Doc-title Cache + Batch Display Fix (2026-05)

## Problem
1. `🔌 xuecheng/xuecheng_read_doc — 2761323464` — humans can't read content IDs.
2. Batch `web_search` / `fetch_url` panels truncated each candidate to 80 chars
   and joined them with `|`, hiding what the model is actually searching.

## Fix

### Cache (lib/mcp/project_names.py)
The module that already cached `overleaf project_id → name` now also caches
`xuecheng contentId → title`. Two independent dicts behind one lock; same
file because both are MCP-id-to-name mappings and `_ingest_obj` walks any
JSON for either shape.

API:
- `get_doc_title(content_id)` / `set_doc_title(content_id, title)`
- `ingest_tool_result()` early-exits unless server is `overleaf` or
  `xuecheng`; harvests:
  - `read_doc` `{title, …}` paired with the request's `doc` arg
    (extracts numeric ID from `https://km.sankuai.com/collabpage/<id>`)
  - `get_doc_meta` `{meta: {contentId, contentTitle}}`
  - `create_document` `{contentId, title, url}`
  - `search` `{items: [{contentId, title}…]}` — searching pre-populates
    the cache for any subsequent read_doc on those hits
- `get_project_name` / `set_project_name` unchanged

### Display (lib/tasks_pkg/tool_display.py)
- `_short_doc_id(val)` now consults `get_doc_title()` first; falls back
  to numeric id when cache miss. Accepts both bare id and full
  collabpage URL.
- `_tool_display_web_search` batch mode: renders every query on its own
  line as `• <full text>` — no per-query truncation, no `…` middle elision.
- `_tool_display_fetch_url` batch mode: same — every URL in full, one per line.

### Frontend (static/js/ui.js + static/styles.css)
- `_renderUnifiedToolLine` substitutes `\n → <br>` in `round.query`
  AFTER `escapeHtml` so newlines actually render.
- `.ptool-line` switched to `align-items: flex-start` with small icon/badge
  top-padding so the icon stays at the top of multi-line tool entries.

## Cache eviction
Both dicts cap at `_MAX_ENTRIES = 2000` with FIFO eviction. Process-local;
cleared on restart.

## Test it
```python
from lib.mcp.project_names import ingest_tool_result, clear_cache
from lib.tasks_pkg.tool_display import _tool_display_mcp
clear_cache()
ingest_tool_result('mcp__xuecheng__xuecheng_read_doc',
                   {'doc': 'https://km.sankuai.com/collabpage/2761323464'},
                   {'ok': True, 'title': 'xuecheng-mcp 搜索文档接口'})
print(_tool_display_mcp('mcp__xuecheng__xuecheng_read_doc',
                        {'doc': '2761323464'}, 't', '{}')[0])
# → 🔌 xuecheng/xuecheng_read_doc — xuecheng-mcp 搜索文档接口
```

