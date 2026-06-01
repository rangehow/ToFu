---
name: install-sh-conda-activate-set-u
description: install.sh: wrap every conda activate/deactivate with set +u/-u; --reset-env does NOT fix it when deactivate-gxx_linux-64.sh lives in BASE miniforge's etc/conda/deactivate.d/
enabled: true
tags: [install.sh, conda, bash]
created: 2026-05-24T09:36:45Z
updated: 2026-05-24T10:08:20Z
---

# install.sh — `conda activate` is NOT compatible with `set -u`

## The trap

`install.sh` starts with `set -euo pipefail` (line 85). Conda's own
activation/deactivation scripts (installed by packages like
`gxx_linux-64`, `gcc_linux-64`, `binutils_linux-64`, etc.) look like:

```bash
# deactivate-gxx_linux-64.sh
export CXX="$CONDA_BACKUP_CXX"
unset CONDA_BACKUP_CXX
```

`CONDA_BACKUP_CXX` is only set if a matching `activate-gxx_linux-64.sh`
ran first. When `conda activate <env>` deactivates the previous env
(or the base env) without that prior activate, the variable is unset →
under `set -u` the script aborts with:

```
.../deactivate.d/deactivate-gxx_linux-64.sh: line 68: CONDA_BACKUP_CXX: unbound variable
install.sh exited with code 1
```

## ⚠️ `--reset-env` does NOT fix this

The offending script can live in **either**:
- `<env>/etc/conda/deactivate.d/deactivate-gxx_linux-64.sh` — `--reset-env` removes it.
- `<conda_base>/etc/conda/deactivate.d/deactivate-gxx_linux-64.sh` ← **common case**, comes from the base miniforge having `gxx_linux-64` installed. `--reset-env` does NOT touch this.

When `conda activate tofu` runs, it first deactivates `base`, which sources the
base `deactivate.d/*.sh` scripts → triggers the unbound-var crash. Removing the
`tofu` env is irrelevant.

**The only real fix is the `set +u` patch in install.sh.**

## Fix (applied to install.sh 2026-05-24)

Wrap ALL `conda activate` / `conda deactivate` calls with
`set +u ... set -u`:

```bash
set +u
conda activate "$ENV_NAME"
set -u
```

Locations patched:
- ~line 597 — main env activation
- ~line 810 — fallback `conda deactivate` before env-remove rebuild
- ~line 813 — re-activation after env rebuild

## Recovery on a destination still running the old install.sh

The patched `install.sh` MUST be copied to the destination first:

```bash
rsync -avh /path/to/patched/install.sh dest:/tofu/install.sh
# then
cd dest/tofu && bash install.sh
```

Or re-export and re-rsync the whole tree.

## Why we don't just `|| true`

`conda activate` with errors hidden would leave the rest of install.sh
running against the WRONG python (likely the base env). `set +u` is
the targeted fix — keep `set -e` so real failures still abort.

