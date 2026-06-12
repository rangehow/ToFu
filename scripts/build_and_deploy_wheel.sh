#!/usr/bin/env bash
#
# build_and_deploy_wheel.sh — package chatui (the `tofu` distribution) into a
# wheel so a DOWNSTREAM consumer (e.g. keyan) can `import tofu` / `import lib`
# on a server that does NOT have the chatui source tree.
#
# Why a wheel (not git+pip, not rsync-the-source):
#   * The target server has no chatui checkout and no git remote to pull from.
#   * `import tofu` pulls in the whole orchestrator + ~25 runtime deps; a wheel
#     carries the code and lets pip resolve those deps from your index.
#   * One self-contained artifact (`dist/tofu-<ver>-py3-none-any.whl`) you can
#     scp anywhere, commit to an internal artifact store, or `pip install`.
#
# Usage:
#   # 1. Just build the wheel locally (default):
#   scripts/build_and_deploy_wheel.sh
#
#   # 2. Build, copy to a remote host, and install into its python env:
#   REMOTE=user@server REMOTE_PY=/path/to/venv/bin/python \
#       scripts/build_and_deploy_wheel.sh --deploy
#
#   # 3. Build + install into the CURRENT environment (same-host new venv):
#   scripts/build_and_deploy_wheel.sh --install-here
#
# Env knobs:
#   SRC         chatui project root            (default: this script's repo root)
#   REMOTE      ssh target for --deploy        (e.g. hadoop@10.0.0.5)
#   REMOTE_DIR  staging dir on the remote       (default: ~/tofu_wheels)
#   REMOTE_PY   python on the remote to install into (default: python3)
#   PIP_ARGS    extra args passed to pip install (e.g. "-i https://mirror/simple")
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SRC:-$(cd "$SCRIPT_DIR/.." && pwd)}"
REMOTE_DIR="${REMOTE_DIR:-~/tofu_wheels}"
REMOTE_PY="${REMOTE_PY:-python3}"
PIP_ARGS="${PIP_ARGS:-}"

MODE="build"
case "${1:-}" in
    --deploy)       MODE="deploy" ;;
    --install-here) MODE="install-here" ;;
    --build|"")     MODE="build" ;;
    *) echo "Unknown arg: $1 (use --build | --deploy | --install-here)" >&2; exit 2 ;;
esac

cd "$SRC"
echo "==> chatui source: $SRC"
[[ -f pyproject.toml && -d tofu ]] || {
    echo "ERROR: $SRC does not look like the chatui repo (need pyproject.toml + tofu/)." >&2
    exit 1
}

# ── 1. Build the wheel ────────────────────────────────────────────────────
# Prefer PEP517 `python -m build`; fall back to pip wheel if `build` is absent.
echo "==> Building wheel..."
rm -rf dist/*.whl 2>/dev/null || true
if python -m build --version >/dev/null 2>&1; then
    python -m build --wheel
else
    echo "    (python -m build unavailable; using 'pip wheel' fallback)"
    python -m pip wheel . --no-deps -w dist/
fi

WHEEL="$(ls -t dist/tofu-*.whl 2>/dev/null | head -1 || true)"
[[ -n "$WHEEL" ]] || { echo "ERROR: no wheel produced in dist/." >&2; exit 1; }
echo "==> Built: $WHEEL"

# ── 2a. Install into the current environment ──────────────────────────────
if [[ "$MODE" == "install-here" ]]; then
    echo "==> Installing into current env: $(python -c 'import sys;print(sys.executable)')"
    # shellcheck disable=SC2086
    python -m pip install --force-reinstall $PIP_ARGS "$WHEEL"
    python -c "import tofu; print('OK import tofu', tofu.__api_version__)"
    exit 0
fi

# ── 2b. Ship to a remote host and install there ───────────────────────────
if [[ "$MODE" == "deploy" ]]; then
    [[ -n "${REMOTE:-}" ]] || { echo "ERROR: --deploy needs REMOTE=user@host." >&2; exit 1; }
    echo "==> Deploying to $REMOTE:$REMOTE_DIR (python: $REMOTE_PY)"
    ssh "$REMOTE" "mkdir -p $REMOTE_DIR"
    scp "$WHEEL" "$REMOTE:$REMOTE_DIR/"
    BASENAME="$(basename "$WHEEL")"
    # shellcheck disable=SC2029
    ssh "$REMOTE" "$REMOTE_PY -m pip install --force-reinstall $PIP_ARGS $REMOTE_DIR/$BASENAME && $REMOTE_PY -c 'import tofu; print(\"OK import tofu\", tofu.__api_version__)'"
    echo "==> Remote install verified."
    exit 0
fi

# ── build-only ────────────────────────────────────────────────────────────
echo "==> Wheel ready. Install it with:"
echo "    pip install $SRC/$WHEEL"
echo "  or ship + install on a remote:"
echo "    REMOTE=user@host REMOTE_PY=/path/to/python $0 --deploy"
