---
name: playwright-chromium-rootless-conda-libs
description: Rootless Chromium deps via conda-forge + runtime LD_LIBRARY_PATH augmentation — fixes libatk/libgbm errors on CentOS 7 HPC nodes
enabled: true
tags: [playwright, chromium, centos, conda, install, bug-fix]
created: 2026-04-17T10:56:44Z
updated: 2026-04-17T10:56:44Z
---

# Playwright Chromium Without Sudo (CentOS 7 / HPC / Shared Nodes)

## Symptom
`logs/error.log` spammed with:
```
[pid=...][err] chrome-headless-shell: error while loading shared libraries:
libatk-1.0.so.0: cannot open shared object file
```
`lib.fetch.playwright_pool` logs `Playwright launch failed: TargetClosedError`.

## Root cause
`playwright install --with-deps chromium` uses apt/dnf and needs sudo.
On HPC/shared nodes (CentOS 7 here) users have no sudo, so the Chromium
binary is downloaded but its 10+ shared-lib deps (libatk, libgbm,
libXcomposite, libasound, libxkbcommon, libatspi, libXdamage, libXfixes,
libXrandr, libatk-bridge) are never installed.

## Fix (two-part)
### 1. Install libs via conda-forge into the active env
Not every lib has a straightforward conda-forge name — note these:
- `atk-1.0`, `at-spi2-atk`, `at-spi2-core`, `alsa-lib`
- X libs use `xorg-` prefix: `xorg-libxcomposite`, `xorg-libxdamage`,
  `xorg-libxfixes`, `xorg-libxrandr`
- `libxkbcommon`, `nspr`, `nss`
- **libgbm has no direct conda-forge package** — use `mesa-libgbm-cos7-x86_64`
  (installs to `$CONDA_PREFIX/x86_64-conda-linux-gnu/sysroot/usr/lib64`)

### 2. Runtime LD_LIBRARY_PATH augmentation
Chromium is spawned as a subprocess; it only finds conda libs if
`LD_LIBRARY_PATH` includes `$CONDA_PREFIX/lib` AND the cos7 sysroot.
`lib/fetch/playwright_pool.py` has `_ensure_chromium_library_path()`
which runs at module import and prepends both paths when `CONDA_PREFIX`
is set. Escape hatch: `CHROMIUM_EXTRA_LIB_DIRS` (colon-separated).

## Install script integration
`install.py::install_playwright()` flow on Linux:
1. Try `playwright install --with-deps chromium` (works on Ubuntu w/ sudo)
2. If --with-deps fails → plain `playwright install chromium` (browser only)
3. If step 2 succeeded AND step 1 didn't → call
   `_install_chromium_deps_via_conda()` which runs
   `conda install -n $(basename $CONDA_PREFIX) -c conda-forge -y <packages>`

## Verification
```bash
LD_LIBRARY_PATH=$CONDA_PREFIX/lib:$CONDA_PREFIX/x86_64-conda-linux-gnu/sysroot/usr/lib64 \
  ldd ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell \
  | grep "not found"   # must be empty
```

