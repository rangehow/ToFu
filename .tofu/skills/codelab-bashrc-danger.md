---
name: codelab-bashrc-danger
description: On shared codelab: ~/.bashrc is container-private, /mnt paths are shared. Never run conda init for another user's install from this shell.
enabled: true
tags: [codelab, conda, bashrc, safety]
created: 2026-04-16T09:39:52Z
updated: 2026-04-16T09:39:52Z
---

# Codelab Environment: ~/.bashrc Safety

## Key Architecture
- `~/.bashrc` is container-private (belongs to whoever's codelab session this is)
- `/mnt/dolphinfs/...` paths are shared across containers
- `/home/sankuai/conda` is a system-wide old conda (4.12.0) from 2023
- `/usr/bin/zz_env.sh` is sourced by bashrc and adds system PATH entries

## DANGER: conda init
- Running `conda init` from ANY miniforge/conda install will **modify ~/.bashrc of the current container user**
- If installing miniforge for user B from user A's codelab terminal, `conda init` will pollute user A's bashrc
- **NEVER run `conda init` when installing for another user** — instead tell them to run it themselves, or manually source the conda.sh

## Safe Alternative for Other Users
Instead of `conda init`, tell the target user to add to THEIR bashrc:
```bash
source /mnt/.../their_miniforge3/etc/profile.d/conda.sh
conda activate base
```

## System conda precedence
The codelab may have other condas on PATH via system profiles. The conda init block in bashrc should take precedence if placed at the end of bashrc.

