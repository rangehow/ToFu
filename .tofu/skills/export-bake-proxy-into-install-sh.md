# Export bakes corp proxy into install.sh (personal + internal)

## Why

`install.sh` runs BEFORE `server.py` ever boots, so it cannot read
`data/config/server_config.json` for `proxy_config`.  Yet its first
acts (`git clone`, `conda install`, `pip install`, `playwright
install`) all need outbound HTTPS — which on the corp network requires
the internal proxy.

## How

`export.py` → `_patch_install_sh_proxy(dest, mode)` reads the source
`server_config.json`'s `proxy_config` + `proxy_bypass_domains` and
prepends an `export http_proxy=… ; export https_proxy=… ; export
no_proxy=…` block in the destination's `install.sh`, anchored right
after `set -euo pipefail`.

## Modes

- `personal`  → injected (you may install on a fresh machine that needs proxy)
- `internal`  → injected (colleagues install behind the corp firewall)
- `opensource` → NEVER injected (would leak the internal proxy IP)

## Idempotency

Block is wrapped in
`# ── BEGIN tofu-export injected proxy ──` / `# ── END tofu-export injected proxy ──`
markers.  Re-exports strip the previous block before re-injecting,
so it never accumulates duplicates.

## Source of values

`proxy_config.http_proxy` / `https_proxy` from
`data/config/server_config.json` (already in `_INTERNAL_CONFIG_KEYS`).
`no_proxy` is `localhost,127.0.0.1,::1` + `proxy_bypass_domains`
(deduped, order-preserved).

## Don't forget

If proxy IP changes in the future, just edit
`data/config/server_config.json` `proxy_config` — next export auto-
picks it up.  Don't hardcode IPs in `export.py`.
