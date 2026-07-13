#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  supervisor.sh — launch the Tofu process-supervisor daemon
# ════════════════════════════════════════════════════════════════
#
#  The supervisor is the ALWAYS-ON root of the start/stop chain: it must
#  outlive any single Tofu server AND survive its own crashes. The owner-
#  ratified launch method is a systemd USER UNIT with Restart=always. Where
#  systemd user-lingering is unavailable, fall back to nohup.
#
#  Usage:
#    ./supervisor.sh install     # write + enable a systemd --user unit
#    ./supervisor.sh run         # run in the foreground (dev / debugging)
#    ./supervisor.sh nohup       # background via nohup (no-systemd fallback)
#    ./supervisor.sh status      # systemctl --user status
#    ./supervisor.sh uninstall   # disable + remove the unit
#
#  Configuration (env — set these before `install`, they are baked into the
#  unit's Environment= lines):
#    TOFU_SUPERVISOR_TOKEN     Bearer token (MANDATORY — daemon fails closed).
#    TOFU_SUPERVISOR_PROJECTS  ':'-separated ABSOLUTE project paths to allow.
#    TOFU_SUPERVISOR_PORT      default 15001
#    TOFU_SUPERVISOR_HOST      default 127.0.0.1
#    TOFU_SUPERVISOR_PYTHON    interpreter used to launch server.py

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${TOFU_SUPERVISOR_PYTHON:-$(command -v python3)}"
UNIT_NAME="tofu-supervisor.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

_check_token() {
    if [[ -z "${TOFU_SUPERVISOR_TOKEN:-}" ]]; then
        echo "ERROR: TOFU_SUPERVISOR_TOKEN is not set. The daemon fails closed" >&2
        echo "       (503 on every control endpoint) without it. Set it first." >&2
        exit 1
    fi
    if [[ -z "${TOFU_SUPERVISOR_PROJECTS:-}" ]]; then
        echo "WARNING: TOFU_SUPERVISOR_PROJECTS is empty — no project will be" >&2
        echo "         startable/stoppable until you allow-list one." >&2
    fi
}

cmd_run() {
    _check_token
    exec "$PY" "$BASE_DIR/supervisor.py"
}

cmd_nohup() {
    _check_token
    local log="$BASE_DIR/data/supervisor.log"
    mkdir -p "$BASE_DIR/data"
    nohup "$PY" "$BASE_DIR/supervisor.py" >>"$log" 2>&1 &
    echo "Supervisor started via nohup (pid=$!), logging to $log"
    echo "NOTE: nohup does NOT restart on crash. Prefer './supervisor.sh install'"
    echo "      (systemd Restart=always) where user-lingering is available."
}

cmd_install() {
    _check_token
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "systemctl not found — this host has no systemd." >&2
        echo "Use './supervisor.sh nohup' instead." >&2
        exit 1
    fi
    mkdir -p "$UNIT_DIR"
    # Bake the current env into the unit so the daemon starts with the same
    # allow-list / token after a reboot. Only non-empty vars are emitted.
    {
        echo "[Unit]"
        echo "Description=Tofu process supervisor (remote start/stop)"
        echo "After=network.target"
        echo ""
        echo "[Service]"
        echo "Type=simple"
        echo "WorkingDirectory=$BASE_DIR"
        echo "ExecStart=$PY $BASE_DIR/supervisor.py"
        echo "Restart=always"
        echo "RestartSec=2"
        echo "Environment=TOFU_SUPERVISOR_TOKEN=$TOFU_SUPERVISOR_TOKEN"
        [[ -n "${TOFU_SUPERVISOR_PROJECTS:-}" ]] && echo "Environment=TOFU_SUPERVISOR_PROJECTS=$TOFU_SUPERVISOR_PROJECTS"
        [[ -n "${TOFU_SUPERVISOR_PORT:-}" ]] && echo "Environment=TOFU_SUPERVISOR_PORT=$TOFU_SUPERVISOR_PORT"
        [[ -n "${TOFU_SUPERVISOR_HOST:-}" ]] && echo "Environment=TOFU_SUPERVISOR_HOST=$TOFU_SUPERVISOR_HOST"
        [[ -n "${TOFU_SUPERVISOR_PYTHON:-}" ]] && echo "Environment=TOFU_SUPERVISOR_PYTHON=$TOFU_SUPERVISOR_PYTHON"
        echo ""
        echo "[Install]"
        echo "WantedBy=default.target"
    } > "$UNIT_PATH"
    echo "Wrote $UNIT_PATH"
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
    # Keep the unit running after logout (best-effort; needs admin on some hosts).
    loginctl enable-linger "$(whoami)" 2>/dev/null || \
        echo "NOTE: could not enable-linger; the unit stops at logout unless lingering is enabled by an admin."
    echo "Supervisor installed + started. Check: ./supervisor.sh status"
}

cmd_status() { systemctl --user status "$UNIT_NAME" --no-pager || true; }

cmd_uninstall() {
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "Removed $UNIT_PATH"
}

case "${1:-run}" in
    run)       cmd_run ;;
    nohup)     cmd_nohup ;;
    install)   cmd_install ;;
    status)    cmd_status ;;
    uninstall) cmd_uninstall ;;
    *) echo "Usage: $0 {run|nohup|install|status|uninstall}" >&2; exit 1 ;;
esac
