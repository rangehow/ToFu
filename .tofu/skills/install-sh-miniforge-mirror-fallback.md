---
name: Install Sh Miniforge Mirror Fallback
description: Corp-network install: 3-layer mirror fix (Miniforge installer / conda-forge channel via .condarc with proxy_servers:{} / pip index) all routed via mirrors.sankuai.com which bypasses corp proxy that 403s public mirrors
enabled: true
tags: [install, conda, miniforge, ipv6, proxy, logging, ansi]
created: 
updated: 2026-05-07T10:58:46Z
---

# install.sh — Corp-network installation: 3-layer fix

## The wall (Meituan corp proxy 10.229.18.27:8412)

The proxy returns HTTP 403 for ALL of:
- github.com release assets
- conda.anaconda.org/conda-forge/* (used by every `conda install`)
- pypi.org/* (used by `pip install`)
- public mirrors: mirrors.tuna.tsinghua.edu.cn, mirrors.bfsu.edu.cn, mirror.nju.edu.cn

The ONLY route that works is `mirrors.sankuai.com`, which is in the
`.sankuai.com` no_proxy bypass — so traffic to it goes DIRECT, never
touching the 403-ing proxy.

## Three-layer fix (export.py + install.sh, 2026-05-07)

### Layer 1: Miniforge installer download (install.sh `_SKIP_MINIFORGE_DOWNLOAD` block)
Mirror chain in order:
1. github.com/conda-forge/miniforge/.../Miniforge3-...sh
2. mirrors.tuna.tsinghua.edu.cn/github-release/...
3. mirrors.bfsu.edu.cn/github-release/...
4. mirror.nju.edu.cn/github-release/...
5. **mirrors.sankuai.com/conda/miniconda/Miniconda3-latest-...sh** ← this one works on corp hosts
   - Note: it's Miniconda (not Miniforge); fine because we use `--override-channels -c conda-forge` everywhere downstream.
   - Sankuai filename: `Miniconda3-latest-${PLATFORM}-${ARCH}.sh` (different from Miniforge's `Miniforge3-...`)
With `curl -4` / `wget -4` to avoid IPv6 trap on hosts with no v6 routing.

### Layer 2: conda-forge channel redirect (install.sh Step 1.5)
If env var `TOFU_CONDA_MIRROR` is set AND we own the sibling conda,
write `${CONDA_BASE}/.condarc` with:
```yaml
channels: [conda-forge]
custom_channels:
  conda-forge: https://mirrors.sankuai.com/conda/cloud
default_channels:
  - https://mirrors.sankuai.com/conda/cloud/conda-forge
proxy_servers: {}    # ← ignore HTTP_PROXY env vars; mirror in no_proxy goes direct
```
Verified URL: `https://mirrors.sankuai.com/conda/cloud/conda-forge/linux-64/repodata.json` returns 200.
**This MUST run BEFORE Step 2's `conda update` and `conda install`.**

### Layer 3: PyPI index redirect
Env var `TOFU_PYPI_INDEX` exported by install.sh top → propagated as
`PIP_INDEX_URL` and `PIP_TRUSTED_HOST` so all later `pip install`
calls use the Sankuai pypi mirror at
`https://mirrors.sankuai.com/pypi/web/simple/`.

## Wiring (export.py → install.sh)

`_patch_install_sh_proxy()` injects all of these env vars in one block
right after `set -euo pipefail`:
```
export http_proxy=... https_proxy=... no_proxy=...
export TOFU_CONDA_MIRROR=https://mirrors.sankuai.com/conda/cloud
export TOFU_PYPI_INDEX=https://mirrors.sankuai.com/pypi/web/simple/
```
Defaults are hardcoded to Sankuai but read overrides from
`server_config.json`'s `conda_mirror` / `pypi_index` keys if set.

## Modes

- personal / internal → all 3 layers injected
- opensource → none injected (would leak corp infra IPs)

## Don't regress

- Sankuai must stay LAST in mirror chain so public installs still try
  upstream mirrors first.
- `proxy_servers: {}` in .condarc is essential — without it, conda
  picks up `HTTPS_PROXY` env and routes to the 403-ing proxy.
- `Miniconda3-latest-...sh` filename != `Miniforge3-...sh`. Keep `MC_FILE`
  alongside `MF_FILE` in install.sh.
- `TOFU_CONDA_MIRROR` write is gated by `CONDA_OWNED_BY_US=1` so we
  never touch a user's pre-existing conda config.

## Log file readability

ANSI-strip tee for plain-text install logs:
```
exec > >(stdbuf -oL tee >(stdbuf -oL sed -u $'s/\\x1b\\[[0-9;]*[a-zA-Z]//g' >> "$TOFU_INSTALL_LOG")) 2>&1
```

