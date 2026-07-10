#!/usr/bin/env bash
#
# restart_15000.sh — reliably reload the Tofu server on :15000.
#
# ┌─────────────────────────────────────────────────────────────────────────┐
# │ RUN THIS FROM A TERMINAL THAT IS **NOT** A CHILD OF THE :15000 SERVER.    │
# │ A plain VS Code terminal is fine. Do NOT run it from inside a Tofu agent  │
# │ shell — that shell is a child of the :15000 process, so killing the       │
# │ server would also kill the shell running this script (self-plug-pull).    │
# └─────────────────────────────────────────────────────────────────────────┘
#
# WHY THIS SCRIPT WAS REWRITTEN (2026-07-10):
#   The live server is launched as `python server.py` with NO `--port` argument
#   (the port defaults to $PORT / 15000 inside server.py). The previous version
#   matched `pkill -f "server.py --port 15000"`, which matched NOTHING, so the
#   old process was never killed; the relaunch then either shifted to :15001 via
#   server.py's _find_free_port fallback OR aborted on the instance lock
#   ("Another server instance is already running"). Root fix: kill the EXACT PID
#   that is actually listening on :15000 (from `ss -ltnp`), escalate SIGTERM →
#   SIGKILL if the port doesn't free, and only then relaunch — with the SAME
#   command the process really uses (`python server.py`, no --port).
#
# Safe to re-run (idempotent): if nothing is on :15000 it just launches one.
# No `set -e` on the whole script so "nothing to kill" is not fatal.

PROJ="/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/chatui"
PY="/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/miniforge3/envs/tofu/bin/python"
PORT=15000
LOG="server_${PORT}.log"

echo "════════════════════════════════════════════════════════════════"
echo "[0/5] restart_15000.sh — reloading Tofu server on :${PORT}"
echo "      project: ${PROJ}"
cd "${PROJ}" || { echo "FATAL: cannot cd into project dir"; exit 1; }

# ── Helper: PIDs currently LISTENING on :PORT (the authoritative kill target). ──
# `ss -ltnp` Local Address:Port column ($4) looks like 127.0.0.1:15000 or
# *:15000 — match a literal ":PORT" at end of field, then pull pid=NNN.
listener_pids() {
  ss -ltnp 2>/dev/null \
    | awk -v pat=":${PORT}\$" '$4 ~ pat {print}' \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

# ── Guard: refuse to run if THIS shell is a descendant of a :PORT listener. ──
# Killing that PID would terminate this very shell (self-plug-pull).
LPIDS_INIT="$(listener_pids)"
if [ -n "${LPIDS_INIT}" ]; then
  up=$$
  for _ in 1 2 3 4 5 6 7 8; do
    { [ -z "${up}" ] || [ "${up}" = "1" ]; } && break
    for lp in ${LPIDS_INIT}; do
      if [ "${up}" = "${lp}" ]; then
        echo "FATAL: this shell (pid $$) is a DESCENDANT of the :${PORT} server"
        echo "       (pid ${lp}). Killing it would terminate this shell."
        echo "       Re-run from a plain VS Code terminal, not a Tofu agent shell."
        exit 2
      fi
    done
    up="$(ps -o ppid= -p "${up}" 2>/dev/null | tr -d ' ')"
  done
fi

# ── [1/5] Stop whatever is listening on :PORT (by exact PID). ──
echo "[1/5] Stopping current server on :${PORT} ..."
LPIDS="$(listener_pids)"
if [ -z "${LPIDS}" ]; then
  # Fallback: no listener socket found (e.g. mid-crash) — match the real
  # launch command. NOTE: matches `python server.py`, NOT a --port substring.
  LPIDS="$(pgrep -f 'server\.py' 2>/dev/null | tr '\n' ' ')"
fi
if [ -n "${LPIDS}" ]; then
  echo "      Target PID(s): ${LPIDS}"
  for lp in ${LPIDS}; do kill "${lp}" 2>/dev/null && echo "      SIGTERM -> ${lp}"; done
else
  echo "      No process found listening on :${PORT} — nothing to stop."
fi

# ── [2/5] Wait for the port to free (up to ~20s); escalate to SIGKILL. ──
echo "[2/5] Waiting for :${PORT} to free ..."
freed=0
for i in $(seq 1 20); do
  if [ -z "$(listener_pids)" ] && ! ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    freed=1
    echo "      Port :${PORT} is free (after ${i}s)."
    break
  fi
  sleep 1
done
if [ "${freed}" != "1" ]; then
  echo "      WARNING: :${PORT} still bound after 20s — escalating to SIGKILL."
  KPIDS="$(listener_pids)"; [ -z "${KPIDS}" ] && KPIDS="$(pgrep -f 'server\.py' 2>/dev/null | tr '\n' ' ')"
  for lp in ${KPIDS}; do kill -9 "${lp}" 2>/dev/null && echo "      SIGKILL -> ${lp}"; done
  sleep 2
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    echo "      FATAL: :${PORT} STILL bound after SIGKILL. Aborting to avoid a"
    echo "             stray second instance / port shift. Investigate manually."
    exit 3
  fi
  echo "      Port :${PORT} freed after SIGKILL."
fi

# ── [3/5] Relaunch EXACTLY as the process is really started (no --port). ──
#   Port comes from $PORT (server.py default 15000). We export it explicitly so
#   the bind is deterministic and never drifts via _find_free_port.
echo "[3/5] Relaunching: PORT=${PORT} nohup ${PY} server.py > ${LOG} 2>&1 &"
PORT="${PORT}" BIND_HOST="${BIND_HOST:-127.0.0.1}" nohup "${PY}" server.py > "${LOG}" 2>&1 &
NEWPID=$!
echo "      Launched pid ${NEWPID}; logging to ${LOG}"

# ── [4/5] Wait for the server to accept connections (up to ~40s; boot does PG
#          bootstrap + blueprint registration). ──
echo "[4/5] Waiting for the server to come up on :${PORT} ..."
BASE="http://127.0.0.1:${PORT}"
up_ok=0
for i in $(seq 1 40); do
  if curl -s --max-time 2 "${BASE}/api/health" >/dev/null 2>&1; then
    up_ok=1
    echo "      Server responding (after ${i}s)."
    break
  fi
  # If the launched process already died (e.g. lock abort), fail fast.
  if ! kill -0 "${NEWPID}" 2>/dev/null; then
    echo "      ERROR: launched pid ${NEWPID} exited during startup. Tail of ${LOG}:"
    tail -n 30 "${LOG}" 2>/dev/null
    echo "      If this is a stale instance lock, last resort:"
    echo "         PORT=${PORT} TOFU_SKIP_LOCK=1 nohup ${PY} server.py > ${LOG} 2>&1 &"
    exit 4
  fi
  sleep 1
done
if [ "${up_ok}" != "1" ]; then
  echo "      ERROR: server did not respond within 40s. Tail of ${LOG}:"
  tail -n 30 "${LOG}" 2>/dev/null
  exit 4
fi

# ── [5/5] Self-verify the sticky-cwd CODE is present in the interpreter this
#          server runs under. NOTE: we do NOT grep /api/v1/capabilities — that
#          endpoint emits only each tool's truncated top-level `description`, not
#          parameter-level fields, so `working_dir`/"STICKY" is ABSENT from the
#          JSON regardless of the running code (a guaranteed false-negative). The
#          valid probe is that the sticky-cwd symbols import cleanly from
#          lib.project_mod under the SAME interpreter used to launch the server.
#          (Full behavioral proof is the two-call test: working_dir on call 1,
#          bare `pwd` on call 2 resumes in that dir — see the JOURNAL entry.)
echo "[5/5] Verifying the sticky-cwd code is importable under the server interpreter ..."
echo "────────────────────────────────────────────────────────────────"
if "${PY}" -c "from lib.project_mod import get_conv_cwd, set_conv_cwd" 2>/dev/null; then
  echo "✅ CODE PRESENT: get_conv_cwd/set_conv_cwd import from lib.project_mod."
  echo "   Server is up on :${PORT} (pid ${NEWPID})."
  echo "   Behavioral proof: run two run_command calls in one conversation —"
  echo "   call 1 with working_dir=<subdir>, call 2 with none + \`pwd\` resumes there."
else
  echo "❌ CODE ABSENT: get_conv_cwd/set_conv_cwd do NOT import from lib.project_mod."
  echo "   git HEAD is missing the sticky-cwd commit (expected 1e8ba20), or ${PY}"
  echo "   is the wrong interpreter. The server started but WITHOUT the feature."
  exit 5
fi
echo "════════════════════════════════════════════════════════════════"
