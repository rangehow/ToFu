#!/usr/bin/env bash
# scripts/run_with_watchdog.sh — Launch server.py under a watchdog that records
# how the child exited, including signals like SIGKILL that the child itself
# cannot log (because the kernel terminates it before any handler runs).
#
# When server.py is killed by a signal you'll see a clear line in
# logs/watchdog.log such as:
#
#   [2026-05-14 23:01:17] PID 12345 EXITED: signal=9 (SIGKILL)  rss_peak=1.8GB  elapsed=42m
#
# Usage:
#   ./scripts/run_with_watchdog.sh                # run forever, restart on death
#   ./scripts/run_with_watchdog.sh --no-restart   # run once, exit when child exits
#   RESTART_BACKOFF_S=10 ./scripts/run_with_watchdog.sh
#
# Pass-through environment (all forwarded to server.py):
#   Anything you would normally export before `python server.py` works as-is.
#
# Notes:
#   * SIGKILL (9) and SIGTERM (15) on the child are reported; SIGTERM/SIGINT
#     to the watchdog itself are propagated to the child for graceful shutdown.
#   * The watchdog log is plain text; it does NOT route through Python's
#     logging system, so it survives even if the Python process never started.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
WATCHDOG_LOG="${LOG_DIR}/watchdog.log"

mkdir -p "${LOG_DIR}"

RESTART="1"
if [[ "${1:-}" == "--no-restart" ]]; then
    RESTART="0"
    shift
fi

RESTART_BACKOFF_S="${RESTART_BACKOFF_S:-5}"

PYTHON_BIN="${PYTHON_BIN:-python}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

watchdog_log() {
    # $1 = message
    printf '[%s] %s\n' "$(ts)" "$1" | tee -a "${WATCHDOG_LOG}" >&2
}

# Translate a signal number to a name (best-effort, no `kill -l` parsing tricks).
sig_name() {
    case "$1" in
        1)  echo "SIGHUP"   ;;
        2)  echo "SIGINT"   ;;
        3)  echo "SIGQUIT"  ;;
        6)  echo "SIGABRT"  ;;
        9)  echo "SIGKILL"  ;;
        11) echo "SIGSEGV"  ;;
        13) echo "SIGPIPE"  ;;
        14) echo "SIGALRM"  ;;
        15) echo "SIGTERM"  ;;
        *)  echo "signal-$1" ;;
    esac
}

# Forward SIGTERM/SIGINT to the child so it can shut down gracefully.
CHILD_PID=""
forward_signal() {
    local sig="$1"
    if [[ -n "${CHILD_PID}" ]] && kill -0 "${CHILD_PID}" 2>/dev/null; then
        watchdog_log "Watchdog received ${sig} — forwarding to child PID=${CHILD_PID}"
        kill "-${sig}" "${CHILD_PID}" 2>/dev/null || true
    fi
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT'  INT

cd "${PROJECT_DIR}"

watchdog_log "Watchdog started (pid=$$, project=${PROJECT_DIR}, restart=${RESTART}, backoff=${RESTART_BACKOFF_S}s)"

while true; do
    start_epoch="$(date +%s)"
    watchdog_log "Spawning: ${PYTHON_BIN} server.py $*"

    # Run the child in the foreground but capture its PID so we can forward signals.
    "${PYTHON_BIN}" server.py "$@" &
    CHILD_PID=$!
    watchdog_log "Child PID=${CHILD_PID}"

    # `wait` returns 128+N when the child died from signal N, otherwise the exit code.
    set +e
    wait "${CHILD_PID}"
    rc=$?
    set -e

    end_epoch="$(date +%s)"
    elapsed=$(( end_epoch - start_epoch ))

    # Try to capture peak RSS before the kernel reaps it (best-effort; usually
    # already gone by the time we read /proc, but we try anyway for cases
    # where the parent is stopping the child intentionally).
    rss_info=""
    if [[ -r "/proc/${CHILD_PID}/status" ]]; then
        rss_info=" rss_peak=$(awk '/VmHWM:/ {print $2 $3}' /proc/${CHILD_PID}/status 2>/dev/null || echo unknown)"
    fi

    if (( rc >= 128 )); then
        signum=$(( rc - 128 ))
        sig=$(sig_name "${signum}")
        watchdog_log "PID ${CHILD_PID} EXITED: signal=${signum} (${sig})  elapsed=${elapsed}s${rss_info}"
        if [[ "${signum}" == "9" ]]; then
            watchdog_log "  → SIGKILL is unblockable. Likely causes: \`kill -9\`, \`pkill -9\`, kernel OOM-killer, or cgroup memory limit. Check \`dmesg | grep -i oom\` and \`journalctl -k\` if available."
        fi
    elif (( rc == 0 )); then
        watchdog_log "PID ${CHILD_PID} EXITED: code=0 (clean)  elapsed=${elapsed}s${rss_info}"
    else
        watchdog_log "PID ${CHILD_PID} EXITED: code=${rc} (non-zero)  elapsed=${elapsed}s${rss_info}"
    fi

    CHILD_PID=""

    if [[ "${RESTART}" != "1" ]]; then
        watchdog_log "Watchdog: --no-restart set, exiting with rc=${rc}"
        exit "${rc}"
    fi

    watchdog_log "Restarting in ${RESTART_BACKOFF_S}s…"
    sleep "${RESTART_BACKOFF_S}"
done
