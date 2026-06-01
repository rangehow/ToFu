---
name: swebench-venv-isolation-fix
description: SWE-bench eval uses per-instance venv for Docker-equivalent isolation; fixes shared conda env corruption
enabled: true
tags: [swebench, testing, conda, venv, isolation, docker]
created: 2026-04-14T04:28:08Z
updated: 2026-04-14T04:28:08Z
---

# SWE-bench Evaluation Environment Fix

## Problem
Shared conda environments caused Pass-to-Pass test regressions because `pip install -e .`
wrote `.egg-link` files to the shared env's site-packages, causing parallel evals to
overwrite each other's installs. ~195/500 instances had identical P2P regressions across
all 6 tools, proving it was an environment issue not an agent issue.

## Solution: Per-instance venv
```python
# Create venv on top of shared conda env
_conda_run(env_path, f'python -m venv --system-site-packages {venv_dir}', ...)
# Install package into venv (writes egg-link to venv's site-packages)
_conda_run(venv_dir, 'python -m pip install -e .', cwd=workspace)
# Run tests using venv (inherits conda deps but has its own editable install)
_conda_run(venv_dir, test_cmd, cwd=workspace)
```

Benefits:
- venv inherits ALL deps from shared conda env (numpy, pytest, etc.)
- `pip install -e .` writes to venv's own site-packages
- Parallel evals are completely isolated
- ~1.7s to create a venv (fast enough)

## Docker can't work in this environment
- Running inside a Kubernetes pod (containerd)
- No Docker socket, no sudo, no root
- /etc/subuid and /etc/subgid are empty and owned by root
- Rootless Docker fails: "No subuid ranges found"
- Podman fails: "operation not permitted" on overlay mount
- udocker works for basic containers but image layer extraction also hits remount permission

## Other fixes applied
1. Django log parser: store BOTH docstring name AND method name in status_map
   (SWE-bench dataset uses both formats in P2P/F2P)
2. Test output truncation: don't truncate before log parsing (large test suites lost all results)
3. Venv cleanup after eval to save disk space

