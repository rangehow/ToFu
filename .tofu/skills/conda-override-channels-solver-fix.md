---
name: conda-override-channels-solver-fix
description: install.sh/install.py/bootstrap.py must use --override-channels to avoid user's ~/.condarc internal mirrors polluting solves
enabled: true
tags: [install, conda, bootstrap, solver, bug-fix]
created: 2026-04-22T10:32:55Z
updated: 2026-04-22T10:32:55Z
---

# Conda `--override-channels` required in installers

## Symptom
On hosts with `~/.condarc` containing internal/vendor mirrors (e.g. Meituan
`http://data-source-conda.sankuai.com/pkgs/free`), `install.sh` / `install.py`
would fail with `LibMambaUnsatisfiableError` during the "Installing Python
dependencies from conda-forge" step. Typical error chain:

```
trafilatura-2.0.0 requires htmldate >=1.9.2
htmldate needs lxml >=5.3
lxml 5.3 needs libxml2 >=2.13.5 → needs newer icu
...but internal pkgs/free mirror pins icu <74 → unsolvable
```

## Root cause
Even though installer commands pass `-c conda-forge`, conda **still considers
channels in the user's `~/.condarc`** unless you also pass
`--override-channels`. Old-ICU packages from the internal mirror get pulled in
and lock the solve graph.

## Fix applied (2026-04-22)
Added `--override-channels` to **every** `conda install / create / update`
invocation in:
- `install.sh`
- `install.py` (both `run()` calls and banners)
- `bootstrap.py` (`_try_conda_install_deps`, `_try_conda_install_postgresql`,
  conda self-update, libmamba-solver install)

Also bumped `lxml>=4.9` → `lxml>=5.3` in all three files' package lists
(CONDA_PKGS / CONDA_PYTHON_DEPS / _CONDA_PYTHON_DEPS) so the solver doesn't
waste time reconciling lxml 4 vs 5 when trafilatura 2.0 transitively needs 5.3+.

## Rule for the future
**Any new `conda install` added to these installers MUST include
`--override-channels` + `-c conda-forge`.** Never rely on `~/.condarc` channel
config because exported copies of the project run on other people's machines
with unknown channel setups.

