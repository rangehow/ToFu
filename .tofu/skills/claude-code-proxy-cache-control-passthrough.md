---
name: claude-code-proxy-cache-control-passthrough
description: Fix for claude-code-proxy to pass through cache_control markers (strip scope field for Bedrock compat) and extract cache_read_tokens from sankuai gateway response
enabled: true
tags: [claude-code, proxy, caching, cache_control, bedrock, sankuai]
created: 2026-04-07T15:53:15Z
updated: 2026-04-07T15:53:15Z
---

# Claude Code Proxy — Cache Control Passthrough Fix

## Problem
The `claude-code-proxy` (Anthropic→OpenAI format converter) was silently stripping 
`cache_control` markers, causing Claude Code to get 0% cache hits through the proxy.

## Root Causes (3 bugs)

### 1. Pydantic models missing `cache_control` field
`models/claude.py` had no `cache_control: Optional[Dict]` on:
- `ClaudeContentBlockText`
- `ClaudeContentBlockImage`  
- `ClaudeContentBlockToolResult`
- `ClaudeSystemContent`
- `ClaudeTool`

Pydantic silently dropped the field during validation.

### 2. Request converter didn't pass through `cache_control`
`conversion/request_converter.py` converted text blocks without preserving `cache_control`.
Fixed by adding `_sanitize_cache_control()` and attaching it to every content block and tool.

**Critical**: Must strip `scope` field from `cache_control`! Claude Code sends 
`cache_control: {type: "ephemeral", scope: "global"}` on static system blocks,
but Bedrock rejects unknown fields: `Extra inputs are not permitted`.

```python
def _sanitize_cache_control(cc):
    if not cc: return None
    cc_dict = cc if isinstance(cc, dict) else dict(cc)
    sanitized = {'type': cc_dict.get('type', 'ephemeral')}
    if 'ttl' in cc_dict:
        sanitized['ttl'] = cc_dict['ttl']
    return sanitized  # strips 'scope' and other unknown fields
```

### 3. Response converter reading wrong field for cache_read
The sankuai gateway returns cache data as:
```json
{
  "prompt_tokens": 8,        // uncached only (Anthropic convention)
  "cache_read_tokens": 7203, // ← TOP-LEVEL field
  "cache_write_tokens": 0,
  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}
}
```

The proxy was reading `prompt_tokens_details.cached_tokens` (doesn't exist).
Fixed to read `usage.get('cache_read_tokens', 0)`.

## Verification
After fix: Claude Code shows `cache_read=49784, input=6` → 100% cache hit rate.

## Minimum cacheable prefix sizes
- Opus / Haiku 4.5: **4,096 tokens**
- Sonnet: **1,024 tokens**

Claude Code's system prompt is ~27K tokens, well above the minimum.

