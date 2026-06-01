---
name: xuecheng-mcp-proxy-auto-detect
description: xuecheng-mcp picks proxy vs direct purely by DNS-probing km.sankuai.com on first call. No env knobs.
enabled: true
tags: [xuecheng-mcp, proxy, convention]
created: 2026-05-08T08:04:28Z
updated: 2026-05-08T08:04:28Z
---

# xuecheng-mcp proxy mode — DNS auto-detect, no env knobs

## Behavior

`xuecheng_mcp.client._resolve_proxy_mode()` runs once on first HTTP use
and caches its verdict for the process lifetime. Decision tree:

1. `getaddrinfo("km.sankuai.com")` succeeds within 2 s → `bypass`
   (build httpx with `proxy=None, trust_env=False` — direct).
2. DNS fails AND `HTTPS_PROXY` env var set → `force`
   (build httpx with explicit proxy, `trust_env=False` — defeats stale
   `NO_PROXY=.sankuai.com`).
3. DNS fails AND no `HTTPS_PROXY` → `bypass` + WARNING log
   (request will likely fail; nothing smarter we can do).

## Why DNS probe?

- Single best signal distinguishing the two real-world topologies:
  corp laptop (resolves) vs stripped GPU container (doesn't).
- 2 s `socket.getaddrinfo` is much cheaper than an HTTP roundtrip and
  won't itself 403 if it accidentally hits a proxy.

## Removed env knobs (do NOT re-introduce)

- `XUECHENG_FORCE_PROXY` — gone.
- `XUECHENG_DISABLE_PROXY` — gone (also dropped from
  `chatui/lib/mcp/registry.py` UI form).

The `xuecheng` MCP catalog entry now lists only `XUECHENG_MIS` (required)
and `XUECHENG_ENV` (optional). If a user reports proxy issues, point
them at `xuecheng_diagnose` which surfaces `proxy.mode` and `probe_host`.

## Reset hook

`_reset_proxy_mode_cache()` clears the cached verdict — useful in tests
and only there.

## Verified 2026-05-08

- dolphinfs container with no DNS for km.sankuai.com: auto → `force`,
  search returns 512 hits via `10.229.18.27:8412`.
- Corp laptop topology (DNS resolves): auto → `bypass` (covered by
  earlier ad-hoc tests; logic path unchanged).

