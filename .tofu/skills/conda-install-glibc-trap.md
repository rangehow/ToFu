---
name: conda-install-glibc-trap
description: install.py/install.sh/bootstrap.py must use conda-forge (not pip) for lxml et al. to avoid GLIBC 2.25+ crash on CentOS 7 hosts
enabled: true
tags: [install, conda, glibc, lxml, bootstrap, centos7]
created: 2026-04-22T09:26:40Z
updated: 2026-04-22T09:26:40Z
---

# Conda-based install & the GLIBC trap

## The bug

When users export this project to CentOS 7 / RHEL 7 / any host with glibc < 2.25, they hit:

```
ImportError: /lib64/libc.so.6: version `GLIBC_2.25' not found
  (required by .../site-packages/lxml/etree.cpython-312-x86_64-linux-gnu.so)
```

**Root cause**: `pip install lxml` downloads a manylinux wheel that's compiled against a newer glibc. conda-forge's `lxml` is built against the conda-forge sysroot glibc 2.17 and works on CentOS 7.

## Fix — all three install paths must use conda-forge

1. **`install.sh`** — conda-only installer (installs Miniforge if missing, updates conda, creates env, `conda install -c conda-forge` for ALL deps).
2. **`install.py`** — same flow cross-platform.
3. **`bootstrap.py`** — CRITICAL: auto-repair loop. When `server.py` crashes with a missing module, bootstrap runs `_try_requirements_txt()`. If that uses pip, it reintroduces broken wheels. We added `_try_conda_install_deps()` which is tried first when we detect `CONDA_PREFIX` is set (via `_running_in_conda_env()` + `_find_conda_exe()`).

## Mandatory heal step

Just running `conda install` is NOT enough if pip previously installed the broken wheel — conda happily no-ops when its version is already satisfied by the env metadata. Before conda-installing we MUST:

```bash
# 1. list pip packages in the env
python -m pip list --format=freeze

# 2. pip uninstall any of our managed deps that appear
python -m pip uninstall -y lxml flask-compress trafilatura ...

# 3. then conda install -c conda-forge
```

Both `install.py` (`_purge_pip_installed_deps`), `install.sh` (inline), and `bootstrap.py` (`_try_conda_install_deps`) do this.

## Verification

After install, run:
```python
python -c "import lxml.etree, trafilatura; print(lxml.__version__)"
```
This catches the glibc mismatch immediately instead of at server startup.

## Python deps that MUST come from conda-forge
flask, flask-compress, requests, psutil, trafilatura, playwright, pillow, python-pptx, **lxml** (the main culprit), mcp.

## Chromium shared libs (Linux, rootless)
atk-1.0, at-spi2-atk, at-spi2-core, alsa-lib, xorg-libxcomposite, xorg-libxdamage, xorg-libxfixes, xorg-libxrandr, libxkbcommon, nspr, nss, mesa-libgbm-cos7-x86_64 — all from conda-forge. `lib/fetch/playwright_pool.py` prepends `$CONDA_PREFIX/lib` to LD_LIBRARY_PATH.

## Key env-var detection
`bootstrap.py` detects conda via:
- `CONDA_PREFIX` env var (set by conda activate / install.py's os.execv)
- `CONDA_EXE` / `MAMBA_EXE`
- `sys.executable` path contains `miniforge`/`miniconda`/`anaconda`

