#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — One-Command Installer (Linux / macOS)
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash
#
#  With options:
#    curl -fsSL ... | bash -s -- --port 8080 --api-key sk-xxx
#
#  This is a thin wrapper that:
#    1. Ensures Python 3.10+ and Git are available
#    2. Clones the repository if needed
#    3. Delegates to install.py (the cross-platform installer)
#
#  For Windows, use install.ps1 instead:
#    irm https://raw.githubusercontent.com/rangehow/ToFu/main/install.ps1 | iex
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── Color helpers ───────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "  ${CYAN}ℹ${NC}  $*"; }
ok()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()  { echo -e "  ${YELLOW}!${NC}  $*"; }
fail()  { echo -e "  ${RED}✗${NC}  $*"; exit 1; }

# ── Parse --dir from arguments (needed before clone) ────────
INSTALL_DIR="${HOME}/tofu"
ARGS=("$@")

for i in "${!ARGS[@]}"; do
    if [[ "${ARGS[$i]}" == "--dir" ]] && [[ -n "${ARGS[$((i+1))]:-}" ]]; then
        INSTALL_DIR="${ARGS[$((i+1))]}"
    fi
done

# ── Banner ──────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}🧈 Tofu (豆腐) — Self-Hosted AI Assistant${NC}"
echo -e "  ─────────────────────────────────────────"
echo ""

# ── Check Python 3.10+ ─────────────────────────────────────
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PY_VERSION=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
        PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
        if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 10 ]]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    fail "Python 3.10+ is required but not found.

   Install it:
     • macOS:   brew install python@3.12
     • Ubuntu:  sudo apt install python3.12 python3.12-venv
     • Fedora:  sudo dnf install python3.12
     • Any:     https://www.python.org/downloads/
"
fi
ok "Found $PYTHON_CMD ($PY_VERSION)"

# ── Check Git ───────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    fail "Git is required but not found.

   Install it:
     • macOS:   xcode-select --install
     • Ubuntu:  sudo apt install git
     • Fedora:  sudo dnf install git
"
fi

# ── Clone or find repository ───────────────────────────────
if [[ -f "${INSTALL_DIR}/server.py" ]]; then
    ok "Existing installation found at ${INSTALL_DIR}"
elif [[ -f "server.py" ]]; then
    # We're already in the project directory
    INSTALL_DIR="$(pwd)"
    ok "Running from project directory"
else
    info "Cloning repository to ${INSTALL_DIR}..."
    git clone https://github.com/rangehow/ToFu.git "$INSTALL_DIR"
    ok "Repository cloned"
fi

# ── Delegate to install.py ─────────────────────────────────
INSTALL_PY="${INSTALL_DIR}/install.py"
if [[ ! -f "$INSTALL_PY" ]]; then
    fail "install.py not found at ${INSTALL_PY}"
fi

echo ""
info "Launching cross-platform installer..."
echo ""

exec "$PYTHON_CMD" "$INSTALL_PY" --dir "$INSTALL_DIR" "$@"
