#!/usr/bin/env bash
#
# setup_bashrc.sh — Persist the corp proxy + auto-activate the Tofu conda env
# in ~/.bashrc for every new interactive shell.
#
# Idempotent: re-running strips the previous block (between the BEGIN/END
# markers) before re-injecting, so it never accumulates duplicates.
#
# Usage:
#   bash setup_bashrc.sh                 # uses defaults below
#   ENV_NAME=tofu bash setup_bashrc.sh   # override env name
#   INSTALL_DIR=/path/to/chatui bash setup_bashrc.sh  # where .tofu_env.json lives
#
set -euo pipefail

# ── Config (override via env vars) ───────────────────────────────────────────
HTTP_PROXY_URL="${HTTP_PROXY_URL:-http://10.213.87.132:8080}"
HTTPS_PROXY_URL="${HTTPS_PROXY_URL:-http://10.213.87.132:8080}"
NO_PROXY_LIST="${NO_PROXY_LIST:-localhost,127.0.0.1,::1,.sankuai.com,yeysai.com}"
ENV_NAME="${ENV_NAME:-tofu}"
INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BASHRC="${BASHRC:-$HOME/.bashrc}"

BEGIN_MARKER="# ── BEGIN tofu setup_bashrc ──"
END_MARKER="# ── END tofu setup_bashrc ──"

# ── Resolve conda base from the install marker (.tofu_env.json) ──────────────
CONDA_BASE=""
MARKER="${INSTALL_DIR}/.tofu_env.json"
if [[ -f "$MARKER" ]] && command -v python3 &>/dev/null; then
    CONDA_BASE="$(python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1])).get('conda_base',''))
except Exception:
    pass" "$MARKER" 2>/dev/null || true)"
fi

# Fallback: probe well-known locations if marker missing/unreadable.
if [[ -z "$CONDA_BASE" || ! -x "${CONDA_BASE}/bin/conda" ]]; then
    for cand in \
        "${INSTALL_DIR}/../miniforge3" \
        "${HOME}/miniforge3" \
        "${HOME}/miniconda3" \
        "${HOME}/anaconda3" \
        "/opt/conda" \
        "/opt/miniforge3"; do
        if [[ -x "${cand}/bin/conda" ]]; then
            CONDA_BASE="$(cd "$cand" && pwd)"
            break
        fi
    done
fi

if [[ -z "$CONDA_BASE" || ! -x "${CONDA_BASE}/bin/conda" ]]; then
    echo "ERROR: could not locate conda. Set INSTALL_DIR to the dir holding .tofu_env.json," >&2
    echo "       or ensure conda is installed in a standard location." >&2
    exit 1
fi
echo "Using conda base: $CONDA_BASE"
echo "Activating env:   $ENV_NAME"

# ── Strip any previous block, then append the fresh one ──────────────────────
touch "$BASHRC"
if grep -qF "$BEGIN_MARKER" "$BASHRC"; then
    echo "Removing previous tofu block from $BASHRC"
    # Delete everything between the markers, inclusive.
    sed -i "/$(printf '%s' "$BEGIN_MARKER" | sed 's/[][\.*^$/]/\\&/g')/,/$(printf '%s' "$END_MARKER" | sed 's/[][\.*^$/]/\\&/g')/d" "$BASHRC"
fi

cat >> "$BASHRC" <<EOF
${BEGIN_MARKER}
# Corp proxy (default for all new shells)
export http_proxy="${HTTP_PROXY_URL}"
export https_proxy="${HTTPS_PROXY_URL}"
export HTTP_PROXY="${HTTP_PROXY_URL}"
export HTTPS_PROXY="${HTTPS_PROXY_URL}"
export no_proxy="${NO_PROXY_LIST}"
export NO_PROXY="${NO_PROXY_LIST}"

# Initialize conda and auto-activate the Tofu env
__conda_setup="\$('${CONDA_BASE}/bin/conda' shell.bash hook 2>/dev/null)"
if [ \$? -eq 0 ]; then
    eval "\$__conda_setup"
else
    if [ -f "${CONDA_BASE}/etc/profile.d/conda.sh" ]; then
        . "${CONDA_BASE}/etc/profile.d/conda.sh"
    else
        export PATH="${CONDA_BASE}/bin:\$PATH"
    fi
fi
unset __conda_setup
conda activate ${ENV_NAME} 2>/dev/null || true
${END_MARKER}
EOF

echo "Done. Updated $BASHRC"
echo "Apply now with:  source $BASHRC"
