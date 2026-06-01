---
name: swebench-isolated-second-server-setup-2026-05
description: Pattern for running SWE-bench without touching the user's main Tofu server: sibling chatui_swebench2/ dir with symlinked code + fresh data/logs/lock; separate port 15001
enabled: true
tags: [swebench, isolation, workflow, tmux]
created: 2026-05-05T13:53:50Z
updated: 2026-05-05T13:53:50Z
---

# Running SWE-bench Without Touching User's Main Tofu Server

## Goal
User's main server on port 15000 must stay alive and unrestarted, but we
need a Tofu instance for SWE-bench that picks up our code changes.

## Blocker
`server.py` has an instance lock (`data/.server.lock`) that BLOCKS a
second server from the SAME project directory, even on a different port.
Also sharing `data/chatui.db` between two servers is documented as
dangerous (PG conflicts, DB races).

## Solution: sibling project dir with symlinked code

```bash
BASE_OLD=/mnt/.../chatui
BASE_NEW=/mnt/.../chatui_swebench2

mkdir -p "$BASE_NEW" && cd "$BASE_NEW"

# Symlink CODE (server.py, lib, debug, routes, static, tests, …)
# Skip mutable dirs (data/, logs/, swebench_*/, .chatui/)
for src in "$BASE_OLD"/*; do
    name=$(basename "$src")
    case "$name" in
        data|logs|swebench_workdir|swebench_rerun_workdir|.chatui|.project_sessions)
            continue ;;
    esac
    ln -sf "$src" "$name"
done

# Fresh mutable dirs
mkdir -p data/config logs
# Copy essential config so new instance has LLM keys + providers
cp "$BASE_OLD/data/config/server_config.json" data/config/
cp "$BASE_OLD/data/config/features.json"      data/config/
cp -r "$BASE_OLD/data/config/daily_reports"   data/config/   # optional

# SWE-bench workdir with symlinked prebuilt assets (save 2h of env rebuilds)
mkdir -p swebench_rerun_workdir/{details,patches,workspaces,eval,runlogs}
ln -sf "$BASE_OLD/swebench_workdir/conda_envs" swebench_rerun_workdir/conda_envs
ln -sf "$BASE_OLD/swebench_workdir/repos"      swebench_rerun_workdir/repos

# Delete any weird symlinks with colons/spaces in names (overleaf-MCP artifacts)
rm -f 'chatui:static' 'overleaf-mcp:cat_dog_manor.png' 'a.md'

# Server launcher (port 15001, different lock file than 15000)
cat > run_server.sh << 'EOF'
#!/bin/bash
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
export PORT=15001
export BIND_HOST=127.0.0.1
export CHATUI_CACHE_MARKERS_NONCLAUDE=1
exec python3 server.py
EOF
chmod +x run_server.sh
```

## Why this works
- `server.py` does `BASE_DIR = os.path.dirname(os.path.abspath(__file__))`.
  `abspath()` does NOT resolve symlinks, so BASE_DIR = chatui_swebench2/.
- `data/.server.lock`, `data/chatui.db`, `logs/*`, `.project_sessions/`
  all live under BASE_DIR → isolated from the main server.
- `lib/`, `routes/`, `debug/` are symlinked → both servers run identical
  code; any fix we push propagates to both on restart.
- Different ports (15000 vs 15001) → no socket conflict.
- Two lock files, different PIDs, both reported in app.log.

## Running the benchmark
Runner script (`swebench_rerun_workdir/run.sh`):
```bash
#!/bin/bash
cd "$(dirname "$0")/.."
export TOFU_BASE_URL=http://127.0.0.1:15001
export CHATUI_CACHE_MARKERS_NONCLAUDE=1
python3 -u debug/swebench_runner.py \
    --models tofu-opus,tofu-minimax,tofu-glm --all \
    --workdir swebench_rerun_workdir \
    --resume \
    --output swebench_rerun_workdir/swebench_results.json 2>&1 \
    | tee -a swebench_rerun_workdir/runlogs/rerun_$(date +%Y%m%d_%H%M%S).log
```

## Two tmux sessions
- `tofu-sw2-server` — the isolated Tofu server on port 15001
- `tofu-sw2-runner` — the swebench_runner pointed at TOFU_BASE_URL

Kill either with `tmux kill-session -t <name>` — won't touch user's
main server.

## Gotchas
- `overleaf-mcp:cat_dog_manor.png`, `chatui:static`, `a.md` and similar
  colon-containing files in the project root break the symlink mirror.
  Delete them from the sibling dir before `ls` inside.
- The sibling's `data/` starts empty — providers + models copied in from
  the main server_config.json. Memory store is empty (expected).
- If a model call from the main server is mid-flight when we start the
  sibling, no cross-talk: they have separate in-memory state, separate
  rate-limiter pools, separate task registries.

## To tear down later
```bash
tmux kill-session -t tofu-sw2-runner
tmux kill-session -t tofu-sw2-server
rm -rf /mnt/.../chatui_swebench2/  # optional
```

