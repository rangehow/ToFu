---
name: conda-forge-htmldate-locks-icu-chain
description: conda-forge htmldate ≤1.9.3 pins lxml<6, which cascades to lock icu ≤75 and block PG 18+
enabled: true
tags: [conda, install.sh, postgresql, icu, htmldate, trafilatura]
created: 2026-04-27T23:17:53Z
updated: 2026-04-27T23:17:53Z
---

# conda-forge htmldate ≤1.9.3 blocks PG 18 / icu 78 / lxml 6

## Symptom
`conda install postgresql>=18 trafilatura` fails with solver errors about
icu 75 vs 78, libxml2-16 2.14 vs 2.15, or libpq h5c52fec_* vs hb80d175_3.

## Root cause chain
1. `conda-forge::htmldate` is stuck at 1.9.3 (as of 2026-04)
2. htmldate 1.9.3 pyproject.toml has `lxml<6,>=5.3`
3. `lxml<6` (conda-forge) forces `libxml2<2.14`
4. `libxml2<2.14` only has `icu 73/75` builds
5. `postgresql=18.2+` and many `18.1` builds need `icu 78` (libpq h9abb657/hb80d175 builds)
6. Solver sees incompatible icu demands → "uninstallable"

## Fix
Move `trafilatura` + `htmldate` from conda_pkgs to pip_pkgs in install.sh.
Pin `htmldate>=1.9.4` (first version with the `<6` upper bound removed).
They're pure-Python, pip install is identical to conda install at runtime.

## Verification command
```bash
conda create -n _test --dry-run -c conda-forge --override-channels -y \
  'python=3.12' 'postgresql=18' 'psycopg2>=2.9' 'lxml>=6'
```
If this succeeds → the chain is free. Then `pip install trafilatura 'htmldate>=1.9.4'`.

## What NOT to do
- Don't pin `icu=75.*` / `libxml2=2.14.*` in install.sh (works but forces
  old icu everywhere — needless downgrade of other packages).
- Don't downgrade to PG 17 (PG 17.9 has the same libpq-icu-78 split).
- Don't set `CHATUI_DB_BACKEND=sqlite` just to dodge the install error.

## Upstream watching
- conda-forge/htmldate-feedstock: watch for 1.9.4 bump; once it lands
  we can move htmldate back to conda_pkgs if we want.

