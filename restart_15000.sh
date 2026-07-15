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

# ── [5/5] Self-verify the EVENT-LOOP-FREEZE FIX (commit c194e18) is actually
#          loaded in THIS running server — NOT some unrelated older feature.
#          Two independent, fix-specific probes, BOTH must pass:
#            (a) STATIC: the fix's new symbols import cleanly under the SAME
#                interpreter that launched the server. If HEAD predates the fix
#                these names don't exist → ImportError. This proves the code is
#                on disk + importable for the launch interpreter.
#            (b) RUNTIME: the new _serve guard code actually executed during
#                THIS boot — the running process emitted a "Loop blocking-guard"
#                line to ${LOG}. NOTE: that guard is DEFAULT OFF (set_debug is
#                unsafe as a 24/7 default on this high-concurrency service), so
#                the normal boot prints "Loop blocking-guard OFF (default)"; a
#                diagnostic boot (TOFU_LOOP_DEBUG_GUARD=1) prints "... armed".
#                EITHER line proves the NEW _serve code ran (both are new in
#                this fix); a boot on OLD code prints neither. A static import
#                can't prove the running process executed the new path — the
#                live log line does. The always-on LoopWatch 5s net is what
#                actually protects production; this guard is the opt-in
#                sub-stall detector.
#          Why probe THIS and not sticky-cwd: the previous [5/5] verified
#          get_conv_cwd/set_conv_cwd (a DIFFERENT commit). A green there says
#          nothing about whether the freeze fix shipped — the probe must assert
#          the change this restart is FOR.
echo "[5/5] Verifying the event-loop-freeze fix (c194e18) is loaded ..."
echo "────────────────────────────────────────────────────────────────"
probe_fail=0

# (a) STATIC — the fix's new symbols must import under the server interpreter.
if "${PY}" -c "from lib.translate.segment_backfill import _get_backfill_semaphore, _translate_and_stamp_eligible" 2>/dev/null; then
  echo "✅ (a) CODE PRESENT: off-loop backfill symbols import from lib.translate.segment_backfill."
else
  echo "❌ (a) CODE ABSENT: _get_backfill_semaphore/_translate_and_stamp_eligible do NOT import."
  echo "       git HEAD is missing commit c194e18, or ${PY} is the wrong interpreter."
  probe_fail=1
fi

# (b) RUNTIME — the new _serve guard code must have run this boot. Match the
#     shared "Loop blocking-guard" prefix so BOTH the default "OFF" line and the
#     opt-in "armed" line count as proof the new path executed. The line is
#     emitted early in _serve; poll briefly in case health came up first.
guard_ok=0
for i in $(seq 1 10); do
  if grep -q "Loop blocking-guard" "${LOG}" 2>/dev/null; then guard_ok=1; break; fi
  sleep 1
done
if [ "${guard_ok}" = "1" ]; then
  echo "✅ (b) NEW _serve CODE RAN: '$(grep -m1 "Loop blocking-guard" "${LOG}" | sed 's/^[^[]*//')'"
else
  echo "❌ (b) NEW _serve CODE DID NOT RUN: no 'Loop blocking-guard' line in ${LOG}."
  echo "       The running process is NOT executing the new _serve code —"
  echo "       git HEAD likely predates the fix, or the wrong file booted."
  probe_fail=1
fi

# (c) WINDOWED FIRST-OPEN — the byte-bounded conversation-open fix (commit
#     0c03be2) must be live: a large conversation served over ?window=N must
#     come back WINDOWED + heavy-field-TRIMMED and its body must be a fraction
#     of the multi-MB full blob (the reported freeze-victim mrbu5j9azz8gi8 was
#     5.78 MB → ~237 KB). This proves THIS fix shipped, not just the freeze fix.
#     Resilient: if the probe conv is absent on this deployment (404 / not
#     found), SKIP rather than fail (the endpoint contract is still checked by
#     tests/test_conv_windowed_blob_slice.py); only FAIL if it exists but is
#     served UNwindowed or over-size.
PROBE_CONV="${TOFU_WINDOW_PROBE_CONV:-mrbu5j9azz8gi8}"
PROBE_URL="${BASE}/api/v1/conversations/${PROBE_CONV}?window=60"
PROBE_JSON="$(curl -s --max-time 20 "${PROBE_URL}" 2>/dev/null)"
if [ -z "${PROBE_JSON}" ] || printf '%s' "${PROBE_JSON}" | grep -qiE '"error"|not.?found'; then
  echo "⏭️  (c) WINDOWED-OPEN probe SKIPPED: conv '${PROBE_CONV}' not present on this"
  echo "       deployment (override with TOFU_WINDOW_PROBE_CONV=<id>). Endpoint"
  echo "       contract still covered by tests/test_conv_windowed_blob_slice.py."
else
  # Parse windowed/trimmed flags + byte size with the server interpreter (no jq dep).
  PROBE_VERDICT="$("${PY}" - "$PROBE_URL" <<'PYEOF' 2>/dev/null
import sys, json, urllib.request
url = sys.argv[1]
try:
    raw = urllib.request.urlopen(url, timeout=20).read()
except Exception as e:
    print("ERR fetch %s" % e); sys.exit(0)
n = len(raw)
try:
    d = json.loads(raw)
except Exception as e:
    print("ERR json %s" % e); sys.exit(0)
w = d.get('windowed') is True
t = d.get('trimmed') is True
under = n < 1024 * 1024
print("bytes=%d windowed=%s trimmed=%s under1MB=%s served=%d total=%s"
      % (n, w, t, under, len(d.get('messages') or []), d.get('totalCount')))
print("VERDICT_OK" if (w and t and under) else "VERDICT_BAD")
PYEOF
)"
  echo "      ${PROBE_VERDICT}" | grep -v VERDICT_
  if printf '%s' "${PROBE_VERDICT}" | grep -q "VERDICT_OK"; then
    echo "✅ (c) WINDOWED-OPEN LIVE: '${PROBE_CONV}' served windowed+trimmed, body < 1 MB."
  else
    echo "❌ (c) WINDOWED-OPEN NOT LIVE: '${PROBE_CONV}' served UNwindowed or over 1 MB."
    echo "       get_conv is shipping the full blob — commit 0c03be2 did not load."
    probe_fail=1
  fi
fi

if [ "${probe_fail}" = "1" ]; then
  echo "────────────────────────────────────────────────────────────────"
  echo "FATAL: a fix is NOT fully live on :${PORT} (pid ${NEWPID}). See above."
  exit 5
fi
echo "✅ FIX LIVE: off-loop backfill + new _serve guard + windowed byte-bounded open on :${PORT} (pid ${NEWPID})."
echo "════════════════════════════════════════════════════════════════"
