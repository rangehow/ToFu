---
name: swebench-tmux-rerun-workflow-2026-05
description: Workflow to rerun SWE-bench in tmux with fresh workdir, sharing prebuilt conda_envs + repos via symlink
enabled: true
tags: [swebench, tmux, workflow, rerun]
created: 2026-05-04T07:56:32Z
updated: 2026-05-04T07:56:32Z
---

# SWE-bench Full Rerun Workflow (tmux, fresh workdir)

## Goal
Rerun all tofu models from scratch without clobbering prior `swebench_workdir/`
results, while avoiding the ~1-2 hour rebuild of 64 conda envs + 9 repo clones.

## Recipe

```bash
# 1. Fresh workdir, symlink the expensive prebuilt dirs
mkdir -p swebench_rerun_workdir
cd swebench_rerun_workdir
ln -sf ../swebench_workdir/conda_envs conda_envs
ln -sf ../swebench_workdir/repos      repos
mkdir -p workspaces eval details patches
cd ..

# 2. Back up authoritative results before touching them
cp swebench_workdir/swebench_results.json \
   swebench_workdir/swebench_results.before_tofu_rerun_$(date +%Y%m%d_%H%M%S).json

# 3. Write a launcher script (swebench_rerun_workdir/run.sh) that:
#    - cds to chatui root
#    - exports CHATUI_CACHE_MARKERS_NONCLAUDE=1
#    - python3 -u debug/swebench_runner.py --models ... --all --workdir ... --resume
#    - tees to swebench_rerun_workdir/runlogs/rerun_$TS.log

# 4. Launch in detached tmux
tmux new-session -d -s tofu-rerun -x 220 -y 50 \
    'bash swebench_rerun_workdir/run.sh; echo "---DONE---"; exec bash'

# 5. Monitor
tmux ls                               # confirm session alive
tmux capture-pane -t tofu-rerun -p | tail -40  # peek at progress
tail -f swebench_rerun_workdir/runlogs/rerun_*.log
tail -f swebench_rerun_workdir/swebench_runner.log   # structured log
```

## Key design choices
- **Symlink conda_envs + repos** — these took ~2h to build the first time; never rebuild.
- **Separate workdir** — keeps historical `swebench_workdir/details/*.json` intact so
  the old report is always recoverable.
- **`--resume` not `--smart-resume`** — see `swebench-smart-resume-vs-resume-semantics`:
  smart-resume strips failed rows and re-runs them, burning money.
- **Detached tmux** — survives SSH disconnect; `tmux attach -t tofu-rerun` to re-enter.
- **Server must already be running on 127.0.0.1:15000** — runner connects to Tofu
  via `/api/chat/start`. Check `/api/health` before launching.

## Concurrency defaults (from MODEL_PRESETS)
- `tofu-opus`: 4 workers (Opus RPM=30 per key × 2 keys = 60; 4 is comfortable)
- `tofu-minimax`: 3 workers (RPM=90)
- `tofu-glm`: 2 workers (RPM=60)

Runtime estimate (from prior run): ~12 hours for all 1236 runs end-to-end.

