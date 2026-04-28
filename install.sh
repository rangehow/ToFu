#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — Conda-based One-Command Installer (Linux / macOS)
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash
#
#  With options:
#    curl -fsSL ... | bash -s -- --port 8080 --api-key sk-xxx
#
#  Options:
#    --dir <path>       Install directory (default: ~/tofu)
#    --env <name>       Conda env name (default: tofu)
#    --port <n>         Server port (default: 15000)
#    --api-key <key>    Pre-configure LLM API key
#    --no-launch        Install only, don't start
#    --skip-playwright  Skip Playwright browser install
#    --no-update-conda  Skip conda self-update
#    --reset-env        Delete the existing conda env and recreate from scratch
#                       (⚠️  DESTRUCTIVE: removes ANY extra packages the user
#                        installed into this env. Only use for your own env.)
#    --force-sqlite     Skip PostgreSQL install + bootstrap entirely and pin
#                       CHATUI_DB_BACKEND=sqlite in .env. Use this when the
#                       host's conda-forge snapshot can't satisfy PG deps
#                       (e.g. icu/libxml2 pin conflicts) — SQLite is fine for
#                       single-user / <100 concurrent use.
#    --pg-major <N>     Force a specific PG major version (e.g. 17). Default
#                       tries 18 → 17 → 16 in order, picking the first one
#                       whose solve succeeds on this host.
#    --reinit-pgdata    If data/pgdata exists but was created by a different
#                       PG major than the one we install, back it up and
#                       re-initdb. WITHOUT this flag we auto-detect the
#                       mismatch and fall back to SQLite (data preserved).
#
#  This script relies ENTIRELY on conda (conda-forge). It:
#    1. Installs Miniforge if no conda is found
#    2. Updates conda itself (outdated conda causes many solver issues)
#    3. Clones the repo if needed
#    4. Creates a fresh conda env with Python 3.10+
#    5. Installs ALL Python dependencies from conda-forge (no pip)
#    6. Installs ripgrep, fd-find, and Chromium shared libs from conda-forge
#    7. Installs PostgreSQL with layered fallback (18 → 17 → 16 → SQLite)
#    8. Validates data/pgdata/ matches installed PG major (auto-heals)
#    9. Installs the Playwright Chromium browser binary
#   10. Launches the server
#
#  For Windows, use install.ps1 instead.
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
step()  { echo ""; echo -e "  ${BOLD}${CYAN}▸${NC}  ${BOLD}$*${NC}"; }

# ── Defaults ────────────────────────────────────────────────
INSTALL_DIR="${HOME}/tofu"
ENV_NAME="tofu"
PY_VER="3.12"
PORT="15000"
API_KEY=""
NO_LAUNCH=0
SKIP_PLAYWRIGHT=0
NO_UPDATE_CONDA=0
RESET_ENV=0
FORCE_SQLITE=0
PG_MAJOR=""         # empty = auto-pick from PG_MAJOR_CANDIDATES
REINIT_PGDATA=0
PG_MAJOR_CANDIDATES=(18 17 16)

# ── Parse arguments ─────────────────────────────────────────
FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)              INSTALL_DIR="$2"; shift 2 ;;
        --env)               ENV_NAME="$2"; shift 2 ;;
        --python)           PY_VER="$2"; shift 2 ;;
        --port)             PORT="$2"; FORWARD_ARGS+=("--port" "$2"); shift 2 ;;
        --api-key)          API_KEY="$2"; FORWARD_ARGS+=("--api-key" "$2"); shift 2 ;;
        --no-launch)        NO_LAUNCH=1; shift ;;
        --skip-playwright)  SKIP_PLAYWRIGHT=1; shift ;;
        --no-update-conda)  NO_UPDATE_CONDA=1; shift ;;
        --reset-env)        RESET_ENV=1; shift ;;
        --force-sqlite)     FORCE_SQLITE=1; shift ;;
        --pg-major)         PG_MAJOR="$2"; shift 2 ;;
        --reinit-pgdata)    REINIT_PGDATA=1; shift ;;
        *)  FORWARD_ARGS+=("$1"); shift ;;
    esac
done

# ── Banner ──────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}🧈 Tofu (豆腐) — Self-Hosted AI Assistant${NC}"
echo -e "  ─────────────────────────────────────────"
echo -e "  Conda-based installer"
echo ""

# ── Tee ALL output (stdout + stderr) into a log file ──
# Everything printed from this point onward ends up in
# <INSTALL_DIR>/logs/install-YYYYMMDD_HHMMSS.log — makes it easy to
# attach the full transcript when reporting an issue.
#
# We respect --dir here but fall back to the CWD if the dir doesn't exist
# yet (e.g. first-ever clone). The log is re-linked to the final path
# once the install directory is known for sure.
_TOFU_LOG_ROOT="${INSTALL_DIR}"
[[ -d "$_TOFU_LOG_ROOT" ]] || _TOFU_LOG_ROOT="$(pwd)"
_TOFU_LOG_DIR="${_TOFU_LOG_ROOT}/logs"
mkdir -p "$_TOFU_LOG_DIR" 2>/dev/null || _TOFU_LOG_DIR="/tmp"
TOFU_INSTALL_LOG="${_TOFU_LOG_DIR}/install-$(date +%Y%m%d_%H%M%S).log"
# Use `tee` via process substitution so the log captures the raw
# (ANSI-coloured) output that the user sees. Colours are fine in the
# log — most tools that read it (pagers, chat UI) handle them, and you
# can strip them later with `sed -r 's/\x1b\[[0-9;]*m//g'` if you want.
# stdbuf -oL keeps stdout line-buffered so progress shows up immediately
# even when piped to tee (solves the "nothing prints for 30s" issue
# during long conda solves).
exec > >(stdbuf -oL tee -a "$TOFU_INSTALL_LOG") 2>&1
# Record key metadata at the top of the log for future debugging.
{
    echo "──────────────────────────────────────────────"
    echo "tofu install.sh — $(date -Iseconds)"
    echo "host:    $(hostname 2>/dev/null || echo unknown)"
    echo "user:    $(whoami 2>/dev/null || echo unknown)"
    echo "args:    $0 $*"
    echo "pwd:     $(pwd)"
    echo "bash:    ${BASH_VERSION:-unknown}"
    echo "which conda (pre-locate): $(command -v conda 2>/dev/null || echo none)"
    echo "──────────────────────────────────────────────"
} >&2
info "Install log: $TOFU_INSTALL_LOG"

# On any non-zero exit (error, Ctrl-C, set -e trigger), remind the user
# where the log is so they can grab it for bug reports.
_tofu_exit_reminder() {
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "" >&2
        echo -e "  ${YELLOW}!${NC}  install.sh exited with code ${rc}" >&2
        echo -e "  ${YELLOW}!${NC}  Full transcript saved to: ${TOFU_INSTALL_LOG}" >&2
        echo -e "  ${YELLOW}!${NC}  Copy it when filing a bug:  cat \"${TOFU_INSTALL_LOG}\"" >&2
    fi
}
trap _tofu_exit_reminder EXIT

# ── Platform check ──────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux)   PLATFORM="Linux" ;;
    Darwin)  PLATFORM="MacOSX" ;;
    *)       fail "Unsupported OS: $OS (use install.ps1 on Windows)" ;;
esac
info "Platform: $OS $ARCH"

# ═══════════════════════════════════════════════════════════════
#  Step 1: Locate or install conda (Miniforge)
# ═══════════════════════════════════════════════════════════════
step "Locating conda"

CONDA_BIN=""
if command -v conda &>/dev/null; then
    CONDA_BIN="$(command -v conda)"
    ok "Found conda at $CONDA_BIN"
elif command -v mamba &>/dev/null; then
    # If mamba is on PATH without conda, find the base conda it shipped with
    CONDA_BIN="$(command -v mamba)"
    ok "Found mamba at $CONDA_BIN (will use with conda fallback)"
elif [[ -x "${HOME}/miniforge3/bin/conda" ]]; then
    CONDA_BIN="${HOME}/miniforge3/bin/conda"
    ok "Found existing Miniforge at ${HOME}/miniforge3"
elif [[ -x "${HOME}/miniconda3/bin/conda" ]]; then
    CONDA_BIN="${HOME}/miniconda3/bin/conda"
    ok "Found existing Miniconda at ${HOME}/miniconda3"
elif [[ -x "${HOME}/anaconda3/bin/conda" ]]; then
    CONDA_BIN="${HOME}/anaconda3/bin/conda"
    ok "Found existing Anaconda at ${HOME}/anaconda3"
else
    info "No conda found — installing Miniforge (conda-forge by default)..."
    MINIFORGE_DIR="${HOME}/miniforge3"
    MF_URL="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-${PLATFORM}-${ARCH}.sh"
    TMP_INSTALLER="$(mktemp -t miniforge.XXXXXX.sh)"
    trap 'rm -f "$TMP_INSTALLER"' EXIT

    info "Downloading $MF_URL"
    if command -v curl &>/dev/null; then
        curl -fsSL "$MF_URL" -o "$TMP_INSTALLER"
    elif command -v wget &>/dev/null; then
        wget -q "$MF_URL" -O "$TMP_INSTALLER"
    else
        fail "Need curl or wget to download Miniforge"
    fi

    bash "$TMP_INSTALLER" -b -p "$MINIFORGE_DIR"
    CONDA_BIN="${MINIFORGE_DIR}/bin/conda"
    [[ -x "$CONDA_BIN" ]] || fail "Miniforge install did not produce $CONDA_BIN"
    ok "Miniforge installed at $MINIFORGE_DIR"
fi

# Activate conda for this shell (needed for `conda activate`)
CONDA_BASE="$("$CONDA_BIN" info --base 2>/dev/null)"
[[ -n "$CONDA_BASE" ]] || fail "Could not determine conda base directory"
# shellcheck disable=SC1091
source "${CONDA_BASE}/etc/profile.d/conda.sh"

# ═══════════════════════════════════════════════════════════════
#  Step 2: Update conda FIRST (outdated conda = solver hangs)
#
#  This MUST run before any other conda command touches an env.
#  Classic symptoms of an outdated conda:
#    - "Solving environment: \\ " spinning forever
#    - "PackagesNotFoundError" for packages that clearly exist
#    - libmamba plugin errors
# ═══════════════════════════════════════════════════════════════
if [[ "$NO_UPDATE_CONDA" -eq 0 ]]; then
    step "Updating conda (MUST happen before anything else)"
    OLD_VER="$(conda --version 2>/dev/null || echo unknown)"
    info "Current version: ${OLD_VER}"

    # Always update from conda-forge to get latest solver (libmamba) fixes.
    if conda update -n base -c conda-forge --override-channels -y conda; then
        NEW_VER="$(conda --version 2>/dev/null || echo unknown)"
        if [[ "$OLD_VER" == "$NEW_VER" ]]; then
            ok "conda already up to date (${NEW_VER})"
        else
            ok "conda updated: ${OLD_VER} → ${NEW_VER}"
        fi
    else
        warn "conda self-update failed — this is NOT fatal but may cause solver issues later"
        warn "If the next steps hang on 'Solving environment', re-run with updated conda:"
        warn "  conda update -n base -c conda-forge --override-channels -y conda"
    fi

    # Ensure libmamba solver is installed and set as default — it's 10x faster
    # and avoids many classic-solver hangs/failures. This is CRITICAL on
    # large conda-forge envs (hundreds of packages with interlocking deps).
    info "Ensuring libmamba solver is installed..."
    if conda install -n base -c conda-forge --override-channels -y conda-libmamba-solver >/dev/null 2>&1; then
        conda config --set solver libmamba || true
        ok "libmamba solver active (10x faster than classic)"
    else
        warn "Could not install libmamba solver — using classic (slower)"
    fi
else
    warn "Skipping conda self-update (--no-update-conda)"
    warn "If you hit solver hangs or 'PackagesNotFoundError', remove --no-update-conda and retry."
fi

# ═══════════════════════════════════════════════════════════════
#  Step 3: Check git and clone repo if needed
# ═══════════════════════════════════════════════════════════════
step "Getting Tofu source code"

if ! command -v git &>/dev/null; then
    info "git not found — installing via conda-forge..."
    conda install -n base -c conda-forge --override-channels -y git
fi

if [[ -f "${INSTALL_DIR}/server.py" ]]; then
    ok "Existing installation found at ${INSTALL_DIR}"
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        info "Updating via git pull..."
        (cd "$INSTALL_DIR" && git pull --ff-only) || warn "git pull failed — continuing with existing code"
    fi
elif [[ -f "server.py" ]]; then
    INSTALL_DIR="$(pwd)"
    ok "Running from project directory: $INSTALL_DIR"
else
    info "Cloning https://github.com/rangehow/ToFu.git → ${INSTALL_DIR}"
    git clone https://github.com/rangehow/ToFu.git "$INSTALL_DIR"
    ok "Repository cloned"
fi

REQ_FILE="${INSTALL_DIR}/requirements.txt"
[[ -f "$REQ_FILE" ]] || fail "requirements.txt not found at $REQ_FILE"

# ═══════════════════════════════════════════════════════════════
#  Step 4: Create / reuse conda env
# ═══════════════════════════════════════════════════════════════
step "Creating conda environment: ${ENV_NAME}"

ENV_EXISTS=0
if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
    ENV_EXISTS=1
fi

if [[ "$ENV_EXISTS" -eq 1 && "$RESET_ENV" -eq 1 ]]; then
    warn "--reset-env: removing existing env '${ENV_NAME}' (this deletes ALL packages in it)"
    conda env remove -n "$ENV_NAME" -y
    ENV_EXISTS=0
fi

if [[ "$ENV_EXISTS" -eq 1 ]]; then
    ok "Env '${ENV_NAME}' already exists — will update in place"
    info "(tip: re-run with --reset-env to wipe and rebuild it from scratch)"
else
    info "Creating env '${ENV_NAME}' with Python ${PY_VER}..."
    conda create -n "$ENV_NAME" -c conda-forge --override-channels -y "python=${PY_VER}"
    ok "Env '${ENV_NAME}' created"
fi

# Activate it for subsequent installs
conda activate "$ENV_NAME"
PY="$(command -v python)"
ok "Using Python: $PY ($(python --version 2>&1))"

# ═══════════════════════════════════════════════════════════════
#  Step 5: Install Python dependencies via conda-forge
# ═══════════════════════════════════════════════════════════════
step "Installing Python dependencies from conda-forge"

# Map requirements.txt → conda-forge package names.
#
# IMPORTANT: trafilatura and htmldate are INTENTIONALLY NOT in this list.
# The conda-forge htmldate package (≤1.9.3) pins "lxml<6,>=5.3", which
# forces libxml2<2.14, which forces icu<76. That transitively blocks
# PostgreSQL 18.1+ (needs icu 78) AND blocks lxml 6.x from being installed.
# The upstream htmldate 1.9.4 (released 2025-11-04) already removed the
# "<6" upper bound on lxml, but conda-forge's feedstock hasn't caught up.
# We install both via pip below — they're pure Python and pip is happy to
# install the unpinned latest version, sidestepping the entire icu deadlock.
CONDA_PKGS=(
    # pip itself — conda 'python' packages OMIT pip by default in recent
    # conda-forge builds. Without this, `python -m pip install ...` below
    # fails with "No module named pip" and trafilatura/htmldate never get
    # installed. Install pip explicitly every time.
    "pip>=23"
    "flask>=3.0"
    "flask-compress>=1.14"
    "requests>=2.31"
    "psutil>=5.9"
    "playwright>=1.40"
    "pillow>=10.0"
    "python-pptx>=0.6.21"
    # lxml ≥6 works with libxml2 2.14+ and icu 75 OR 78 — gives the solver
    # maximum freedom. It's ABI-compatible with lxml 5.x at the Python level.
    "lxml>=6"
    # BS4 — HTML fallback parser in lib/fetch/html_extract.py
    "beautifulsoup4>=4.12"
    # python-dateutil — eagerly imported by lib/fetch/html_extract.py
    "python-dateutil>=2.8"
    # Office document parsers for lib/doc_parser.py (upload pipeline)
    "python-docx>=1.0"
    "openpyxl>=3.1"
    "xlrd>=2.0"
    "olefile>=0.46"
    "mcp>=1.0"
    # PDF parsing (fitz) — used in lib/pdf_parser and routes/paper
    "pymupdf>=1.24"
    # uv / uvx — used by lib/mcp/client.py to launch MCP servers
    "uv>=0.4"
)

# Pip-installed deps.
#
# trafilatura + htmldate are pure-Python packages; installing them via pip
# lets us get htmldate 1.9.4+ (no "lxml<6" upper bound), which in turn lets
# the conda env install PG 18 + icu 78 + lxml 6 cleanly. This is NOT a
# downgrade — it's the opposite: pip gives us NEWER htmldate than conda has.
#
# We ALSO list trafilatura's other pure-Python deps explicitly here
# (justext, courlan, dateparser, charset-normalizer) because we install
# with --no-deps below (to prevent pip from pulling an old lxml that
# shadows our conda lxml 6). Without these, importing trafilatura fails
# with "ModuleNotFoundError: No module named 'justext'" etc.
PIP_ONLY_PKGS=(
    "pymupdf4llm>=0.0.17"
    "trafilatura>=1.6"
    "htmldate>=1.9.4"
    # trafilatura's pure-Python deps (from its pyproject.toml).
    # certifi/urllib3 are already pulled in by requests via conda.
    "justext>=3.0.1"
    "courlan>=1.3.2"
    "charset-normalizer>=3.4.0"
    # htmldate's pure-Python deps.
    "dateparser>=1.1.2"
)

# ── Heal broken envs: remove any pip-installed versions of these deps ──
# A common failure mode on older hosts (CentOS 7 / glibc 2.17) is that an
# earlier run left pip's manylinux wheel of lxml in the env. That wheel
# links to GLIBC_2.25+ and crashes at import. We uninstall any pip copies
# first so conda-forge's (sysroot-linked) version is the one used.
info "Purging any pip-installed copies that would shadow conda-forge..."
# Note: trafilatura + htmldate are INTENTIONALLY kept in pip (we WANT
# pip versions of those — conda-forge's htmldate ≤1.9.3 has the
# lxml<6 pin that locks us out of modern icu/PG). So we DON'T include
# them in this purge list.
PIP_NAMES=(flask flask-compress Flask-Compress requests psutil
           playwright pillow Pillow python-pptx lxml beautifulsoup4 bs4
           python-dateutil dateutil python-docx docx openpyxl xlrd olefile
           mcp pymupdf PyMuPDF uv)
PIP_LIST="$(python -m pip list --format=freeze 2>/dev/null || true)"
TO_UNINSTALL=()
for name in "${PIP_NAMES[@]}"; do
    if echo "$PIP_LIST" | grep -iq "^${name}=="; then
        TO_UNINSTALL+=("$name")
    fi
done
if [[ ${#TO_UNINSTALL[@]} -gt 0 ]]; then
    info "Removing pip copies: ${TO_UNINSTALL[*]}"
    python -m pip uninstall -y "${TO_UNINSTALL[@]}" || warn "pip uninstall had issues"
else
    ok "No pip-installed deps to purge"
fi

info "Solving and installing: ${CONDA_PKGS[*]}"
# ── Pre-emptive conflict heal ──
# Some packages from previous install runs (e.g. an older postgresql pulled
# in a pinned icu/libxml2 that blocks newer trafilatura/lxml). Before the
# main solve, purge known conflict sources so the solver has a clean slate.
# All removes are best-effort — missing packages are fine.
info "Purging potentially conflicting conda packages (best-effort)..."
# trafilatura + htmldate removed from conda (we install via pip — see
# PIP_ONLY_PKGS above for rationale). If a previous run installed them
# via conda, nuke them here so their stale 'lxml<6' pin doesn't fight us.
CONDA_CONFLICT_PKGS=(
    postgresql psycopg2
    trafilatura htmldate courlan
    lxml libxml2 libxml2-16 libxslt
    icu
)
conda remove -n "$ENV_NAME" -y --force "${CONDA_CONFLICT_PKGS[@]}" >/dev/null 2>&1 || true
ok "Conflict-prone packages cleared (will reinstall below)"

# Also purge any pip-installed trafilatura/htmldate from prior runs so
# pip's own install below is clean.
python -m pip uninstall -y trafilatura htmldate courlan >/dev/null 2>&1 || true

# --force-reinstall: make sure conda actually re-lays-down the files even if
# its metadata still thinks the package is satisfied (common right after a
# pip-uninstall — conda's view of the env can be stale).
_install_main_deps() {
    conda install -n "$ENV_NAME" -c conda-forge --override-channels -y --force-reinstall "${CONDA_PKGS[@]}"
}

if ! _install_main_deps; then
    warn "First solve failed — doing a deeper reset of the conflicting packages and retrying"
    # Deeper reset: also strip libs that often pin icu/libxml2, then retry.
    conda remove -n "$ENV_NAME" -y --force \
        postgresql psycopg2 libpq \
        trafilatura htmldate courlan \
        lxml libxml2 libxml2-16 libxslt \
        icu \
        >/dev/null 2>&1 || true
    if ! _install_main_deps; then
        # ── Last-resort: nuke the env and rebuild from scratch ──
        # The env's conda-meta/history still pins old specs (e.g. postgresql>=18)
        # that --force removes don't clear. Only `env remove` truly resets it.
        warn "Deep reset still failed — conda env history has stale pins."
        warn "Auto-rebuilding env '${ENV_NAME}' from scratch (one-time, ~2 min)..."
        conda deactivate >/dev/null 2>&1 || true
        conda env remove -n "$ENV_NAME" -y
        conda create -n "$ENV_NAME" -c conda-forge --override-channels -y "python=${PY_VER}"
        conda activate "$ENV_NAME"
        PY="$(command -v python)"
        ok "Env '${ENV_NAME}' rebuilt with fresh Python ${PY_VER}"
        _install_main_deps
    fi
fi
ok "Python dependencies installed"

# ── Post-install import check: conda's metadata occasionally says a
#    package is installed when the actual files are missing (happens when
#    a prior run did `conda remove --force` and cache got confused).
#    Verify each critical package imports; if any fail, force a
#    --force-reinstall targeted at just those.
info "Verifying critical conda packages import correctly..."
_IMPORT_CHECK_PKGS=(
    "flask:flask"
    "flask_compress:flask-compress"
    "requests:requests"
    "psutil:psutil"
    "playwright:playwright"
    "PIL:pillow"
    "pptx:python-pptx"
    "lxml:lxml"
    "bs4:beautifulsoup4"
    "dateutil:python-dateutil"
    "docx:python-docx"
    "openpyxl:openpyxl"
    "mcp:mcp"
    "fitz:pymupdf"
)
_MISSING_PKGS=()
for _spec in "${_IMPORT_CHECK_PKGS[@]}"; do
    _mod="${_spec%%:*}"
    _conda_name="${_spec##*:}"
    if ! python -c "import ${_mod}" 2>/dev/null; then
        warn "  ${_mod} (conda pkg '${_conda_name}') imports missing"
        _MISSING_PKGS+=("$_conda_name")
    fi
done
if [[ ${#_MISSING_PKGS[@]} -gt 0 ]]; then
    warn "Conda metadata inconsistent — force-reinstalling: ${_MISSING_PKGS[*]}"
    conda install -n "$ENV_NAME" -c conda-forge --override-channels -y \
        --force-reinstall "${_MISSING_PKGS[@]}" || \
        warn "Force-reinstall failed — env may need a full rebuild (re-run with --reset-env)"
fi

# ── Install pip-only deps (e.g. pymupdf4llm) into the conda env ──
# pymupdf4llm is not shipped on conda-forge; it's a thin LLM-oriented Markdown
# extractor built on top of pymupdf (which we just installed via conda).
if [[ ${#PIP_ONLY_PKGS[@]} -gt 0 ]]; then
    info "Installing pip-only deps (not on conda-forge): ${PIP_ONLY_PKGS[*]}"

    # Defensive: ensure pip is actually importable in this env. Recent
    # conda-forge 'python' no longer bundles pip automatically; if the
    # main deps install above didn't pull it in, install it now so the
    # pip commands below don't fail with "No module named pip".
    if ! python -c "import pip" 2>/dev/null; then
        warn "pip not found in env — installing it from conda-forge now"
        if ! conda install -n "$ENV_NAME" -c conda-forge --override-channels -y 'pip>=23'; then
            warn "Could not install pip via conda — trying ensurepip as fallback"
            python -m ensurepip --upgrade 2>/dev/null || true
        fi
    fi

    if ! python -c "import pip" 2>/dev/null; then
        warn "pip STILL not available — skipping pip installs (trafilatura/htmldate/pymupdf4llm)"
        warn "Manual recovery: conda install -n ${ENV_NAME} -c conda-forge pip && \\"
        warn "                 pip install ${PIP_ONLY_PKGS[*]}"
    elif python -m pip install --no-deps --upgrade "${PIP_ONLY_PKGS[@]}"; then
        ok "Pip-only deps installed"
    else
        warn "pip install --no-deps failed — retrying with dependency resolution"
        if python -m pip install --upgrade "${PIP_ONLY_PKGS[@]}"; then
            ok "Pip-only deps installed (with dependency resolution)"
        else
            warn "Pip-only deps install failed — some PDF features may be degraded"
        fi
    fi
fi

# ── Install PostgreSQL + psycopg2 from conda-forge (optional but recommended) ──
# tofu uses PG for better concurrency (100+ concurrent users), auto-falls back
# to SQLite if PG is missing.
#
# Layered fallback: try PG 18 → 17 → 16 → SQLite. Different conda-forge
# snapshots pin icu/libxml2 in ways that conflict with trafilatura/lxml
# (we saw this on hosts where PG 18 requires icu>=78 but trafilatura needs
# icu<76). Trying older majors often succeeds because their icu pins are
# looser. The first major whose solve succeeds wins.
PG_INSTALLED_MAJOR=""   # set to the major we successfully installed, empty if we gave up
if [[ "$FORCE_SQLITE" -eq 1 ]]; then
    info "--force-sqlite: skipping PostgreSQL install entirely"
else
    # If user pinned a specific major, only try that one.
    if [[ -n "$PG_MAJOR" ]]; then
        _PG_TRY=("$PG_MAJOR")
    else
        _PG_TRY=("${PG_MAJOR_CANDIDATES[@]}")
    fi

    info "Installing PostgreSQL + psycopg2 from conda-forge (trying majors: ${_PG_TRY[*]})"
    # ── Pre-clean prior PG remnants from the env ──
    # A previous run may have left a different PG major installed. Its
    # history pin will fight any attempt to install a different major.
    # --force remove clears the package files; the history pin is cleared
    # later by --prune-deps if needed.
    conda remove -n "$ENV_NAME" -y --force postgresql libpq psycopg2 >/dev/null 2>&1 || true

    _PG_BIN_DIR="${CONDA_BASE}/envs/${ENV_NAME}/bin"
    _PG_LAST_LOG=""

    # Install strategy: try the requested major with a plain spec first.
    # Since trafilatura/htmldate are now pip-installed (see Step 5 above),
    # nothing in the env forces a libxml2 version, so the solver is free to
    # pick whichever icu/libxml2 combination matches the PG major chosen.
    #
    # If the first attempt still fails (e.g. conda-forge snapshot is mid-
    # migration and PG's icu-78 libpq build isn't fully propagated to this
    # arch yet), we fall back to the next major in the list.
    for _try_major in "${_PG_TRY[@]}"; do
        info "  Trying PostgreSQL ${_try_major}.x ..."
        _PG_LAST_LOG="/tmp/tofu_pg_install_${_try_major}.log"

        set +e
        conda install -n "$ENV_NAME" -c conda-forge --override-channels -y \
            "postgresql=${_try_major}" 'psycopg2>=2.9' 2>&1 | tee "$_PG_LAST_LOG"
        _rc="${PIPESTATUS[0]}"
        set -e

        if [[ "$_rc" -eq 0 && -x "${_PG_BIN_DIR}/postgres" ]]; then
            _got_major="$("${_PG_BIN_DIR}/postgres" --version 2>/dev/null \
                | awk '{print $3}' | cut -d. -f1)"
            if [[ "$_got_major" == "$_try_major" ]]; then
                PG_INSTALLED_MAJOR="$_got_major"
                ok "PostgreSQL ${PG_INSTALLED_MAJOR}.x installed + psycopg2"
                break
            fi
            warn "  Installed postgres reports major=${_got_major}, expected ${_try_major}"
        elif [[ "$_rc" -ne 0 ]]; then
            warn "  PG ${_try_major}.x solve failed (rc=${_rc}) — see ${_PG_LAST_LOG}"
        else
            warn "  conda returned 0 but ${_PG_BIN_DIR}/postgres missing"
        fi

        # Ensure next attempt starts clean (important: leftover libpq/history
        # pins can make the next major fail for unrelated reasons).
        conda remove -n "$ENV_NAME" -y --force postgresql libpq psycopg2 >/dev/null 2>&1 || true
    done

    if [[ -z "$PG_INSTALLED_MAJOR" ]]; then
        warn "All PG majors failed to install on this host"
        [[ -n "$_PG_LAST_LOG" ]] && warn "Last conda log: ${_PG_LAST_LOG}"
        warn ""
        warn "Diagnosis checklist (from the conda solver output above):"
        warn "  1. Is the conda-forge snapshot mid-migration for your arch?"
        warn "     → Run: conda search -c conda-forge --override-channels 'postgresql=18' --info | head -40"
        warn "       and check whether libpq-18.x builds exist for your platform."
        warn "  2. Does something in the env still pin icu/libxml2 to an old side?"
        warn "     → Run: conda list -n ${ENV_NAME} | grep -E '(icu|libxml2|lxml)'"
        warn "     → If you see 'icu 75' but PG needs 78 (or vice-versa), inspect the"
        warn "       'history' file: \$CONDA_PREFIX/conda-meta/history"
        warn "  3. Is conda itself outdated?"
        warn "     → Re-run WITHOUT --no-update-conda"
        warn ""
        warn "Last-resort: re-run with --force-sqlite if you just want to get running (SQLite"
        warn "                 is fine for single-user / <100 concurrent and is bit-for-bit"
        warn "                 compatible with the same app code)."
    fi
fi

# ── Verify the full HTML-fetch stack imports (no hidden missing deps) ──
# This runs the same chain that server.py will run at startup, so any
# ModuleNotFoundError here surfaces BEFORE the user hits it.
info "Verifying lxml + trafilatura + htmldate + justext import correctly..."
if python -c "import lxml.etree, trafilatura, htmldate, justext, courlan, dateparser; print('lxml', lxml.__version__, 'trafilatura', trafilatura.__version__, 'htmldate', htmldate.__version__, 'justext', justext.__version__)"; then
    ok "Import check passed"
else
    warn "One of lxml/trafilatura/htmldate/justext/courlan/dateparser failed to import."
    warn "If you see 'GLIBC_2.xx not found', a pip wheel is still shadowing conda's copy."
    warn "Try: conda activate ${ENV_NAME} && pip uninstall -y lxml && conda install -c conda-forge --force-reinstall lxml"
    warn "If you see 'No module named X', run: pip install X"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 6: Verify SQLite (built into Python)
# ═══════════════════════════════════════════════════════════════
step "Checking SQLite"
SQLITE_VER="$(python -c 'import sqlite3; print(sqlite3.sqlite_version)')"
ok "SQLite $SQLITE_VER (built into Python)"

# ═══════════════════════════════════════════════════════════════
#  Step 7: Install ripgrep & fd-find from conda-forge (fast search)
# ═══════════════════════════════════════════════════════════════
step "Installing ripgrep + fd-find (fast code/file search)"
if conda install -n "$ENV_NAME" -c conda-forge --override-channels -y ripgrep fd-find; then
    ok "ripgrep + fd-find installed"
else
    warn "ripgrep/fd-find install failed — code search will fall back to grep / os.walk"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 8: Playwright — Chromium browser + shared libs (rootless)
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_PLAYWRIGHT" -eq 0 ]]; then
    step "Installing Playwright Chromium"

    # On Linux, install Chromium's shared libs from conda-forge so that no
    # sudo / system packages are required. lib/fetch/playwright_pool.py
    # auto-prepends $CONDA_PREFIX/lib to LD_LIBRARY_PATH at runtime.
    if [[ "$OS" == "Linux" ]]; then
        info "Installing Chromium shared-lib deps from conda-forge (rootless)..."
        CHROMIUM_LIBS=(
            atk-1.0
            at-spi2-atk
            at-spi2-core
            alsa-lib
            xorg-libxcomposite
            xorg-libxdamage
            xorg-libxfixes
            xorg-libxrandr
            libxkbcommon
            nspr
            nss
            mesa-libgbm-cos7-x86_64
        )
        if ! conda install -n "$ENV_NAME" -c conda-forge --override-channels -y "${CHROMIUM_LIBS[@]}"; then
            warn "Some Chromium shared-lib deps failed to install — browser may not launch"
            info "You can retry manually: conda install -n ${ENV_NAME} -c conda-forge <packages>"
        else
            ok "Chromium shared libs installed into conda env"
        fi
    fi

    info "Downloading Chromium browser binary via playwright..."
    if python -m playwright install chromium; then
        ok "Playwright Chromium installed"
    else
        warn "Playwright Chromium install failed (non-critical — fetching still works via requests)"
    fi
else
    info "Skipping Playwright (--skip-playwright)"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 8.5: Validate data/pgdata/ matches installed PG major
#
#  Catches: "unrecognized configuration parameter 'autovacuum_worker_slots'"
#  (PG 18 data dir running under PG 17 binary) and similar version skews
#  that make the scheduler spin forever on "connection refused".
#
#  Policy:
#    - No pgdata/ yet           → nothing to check, PG bootstrap will initdb later.
#    - pgdata major == installed major → OK, reuse.
#    - mismatch + --reinit-pgdata     → back up pgdata, let PG bootstrap re-initdb.
#    - mismatch without --reinit-pgdata → pin CHATUI_DB_BACKEND=sqlite (data preserved).
#    - pgdata exists but no PG installed locally → pin CHATUI_DB_BACKEND=sqlite.
# ═══════════════════════════════════════════════════════════════
step "Validating data/pgdata/ (version compatibility)"

PGDATA_DIR="${INSTALL_DIR}/data/pgdata"
PGDATA_MAJOR=""
if [[ -f "${PGDATA_DIR}/PG_VERSION" ]]; then
    PGDATA_MAJOR="$(tr -d '[:space:]' < "${PGDATA_DIR}/PG_VERSION" | cut -d. -f1)"
    info "Found existing pgdata (PG ${PGDATA_MAJOR})"
fi

# Default: whatever we installed wins.
DB_BACKEND_CHOICE=""   # empty = auto (let server.py decide), 'sqlite' = force

if [[ "$FORCE_SQLITE" -eq 1 ]]; then
    DB_BACKEND_CHOICE="sqlite"
    if [[ -n "$PGDATA_MAJOR" ]]; then
        info "--force-sqlite: leaving pgdata in place but using SQLite"
    fi
elif [[ -z "$PG_INSTALLED_MAJOR" ]]; then
    # PG never got installed
    if [[ -n "$PGDATA_MAJOR" ]]; then
        warn "pgdata exists (PG ${PGDATA_MAJOR}) but no PG binaries installed in env"
        warn "Would cause scheduler/db retry storms \u2014 pinning CHATUI_DB_BACKEND=sqlite"
    else
        info "No PG installed \u2014 tofu will use SQLite"
    fi
    DB_BACKEND_CHOICE="sqlite"
elif [[ -n "$PGDATA_MAJOR" && "$PGDATA_MAJOR" != "$PG_INSTALLED_MAJOR" ]]; then
    warn "pgdata major (${PGDATA_MAJOR}) differs from installed PG (${PG_INSTALLED_MAJOR})"
    warn "Running pgdata under a mismatched major will cause FATAL config-param errors"
    if [[ "$REINIT_PGDATA" -eq 1 ]]; then
        _BAK="${PGDATA_DIR}.bak.$(date +%Y%m%d_%H%M%S)"
        info "--reinit-pgdata: backing up existing pgdata \u2192 ${_BAK}"
        mv "$PGDATA_DIR" "$_BAK"
        ok "pgdata moved aside; PG bootstrap will initdb fresh under PG ${PG_INSTALLED_MAJOR}"
        # Also nuke the SQLite db if we want a totally clean slate? No \u2014
        # SQLite is independent, leave it alone.
    else
        warn "Re-run with --reinit-pgdata to auto-initdb (existing PG data will be backed up)"
        warn "For now, pinning CHATUI_DB_BACKEND=sqlite so scheduler doesn't spin"
        DB_BACKEND_CHOICE="sqlite"
    fi
elif [[ -n "$PGDATA_MAJOR" ]]; then
    ok "pgdata (PG ${PGDATA_MAJOR}) matches installed PG (${PG_INSTALLED_MAJOR}) \u2014 reusing"
else
    ok "PG ${PG_INSTALLED_MAJOR} ready; bootstrap will initdb on first server.py run"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 8.6: Smoke-test PG startup (best-effort, don't block install)
#
#  If we chose to use PG, try `pg_ctl start` once under a timeout so
#  config-file errors surface NOW instead of during first /api call.
# ═══════════════════════════════════════════════════════════════
if [[ -z "$DB_BACKEND_CHOICE" && -n "$PG_INSTALLED_MAJOR" && -d "$PGDATA_DIR" ]]; then
    step "Smoke-testing PostgreSQL startup"
    _PG_CTL="${CONDA_BASE}/envs/${ENV_NAME}/bin/pg_ctl"
    _PG_LOG_DIR="${INSTALL_DIR}/logs"
    mkdir -p "$_PG_LOG_DIR"
    # Stop any stale process first (best-effort), then try to start.
    "$_PG_CTL" -D "$PGDATA_DIR" stop -m fast >/dev/null 2>&1 || true
    # Remove stale pidfile left by killed/crashed prior runs
    rm -f "${PGDATA_DIR}/postmaster.pid" 2>/dev/null || true
    if "$_PG_CTL" -D "$PGDATA_DIR" -l "${_PG_LOG_DIR}/postgresql.log" -w -t 15 start >/dev/null 2>&1; then
        ok "PostgreSQL started successfully (smoke test)"
        "$_PG_CTL" -D "$PGDATA_DIR" stop -m fast >/dev/null 2>&1 || true
    else
        warn "PG failed to start during smoke test \u2014 see ${_PG_LOG_DIR}/postgresql.log"
        warn "Pinning CHATUI_DB_BACKEND=sqlite to avoid scheduler retry storms"
        warn "Re-run with --reinit-pgdata after moving ${PGDATA_DIR} aside if you want fresh PG"
        DB_BACKEND_CHOICE="sqlite"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 9: Configure .env
# ═══════════════════════════════════════════════════════════════
step "Configuring .env"

ENV_FILE="${INSTALL_DIR}/.env"
ENV_EXAMPLE="${INSTALL_DIR}/.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$ENV_EXAMPLE" ]]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        info "Created .env from template"
    else
        cat > "$ENV_FILE" <<EOF
PORT=${PORT}
BIND_HOST=0.0.0.0
EOF
        info "Created minimal .env"
    fi
fi

# Update/insert a key in .env
_set_env_var() {
    local key="$1" value="$2" file="$3"
    if grep -qE "^[#[:space:]]*${key}=" "$file" 2>/dev/null; then
        # Portable sed -i (macOS requires a backup ext)
        if [[ "$OS" == "Darwin" ]]; then
            sed -i '' -E "s|^[#[:space:]]*${key}=.*|${key}=${value}|" "$file"
        else
            sed -i -E "s|^[#[:space:]]*${key}=.*|${key}=${value}|" "$file"
        fi
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

_set_env_var "PORT" "$PORT" "$ENV_FILE"
if [[ -n "$API_KEY" ]]; then
    _set_env_var "LLM_API_KEYS" "$API_KEY" "$ENV_FILE"
    ok "API key configured"
fi

# Write DB backend decision into .env so server.py knows exactly which
# backend to use (no silent PG-then-fallback retry storms at startup).
if [[ "$DB_BACKEND_CHOICE" == "sqlite" ]]; then
    _set_env_var "CHATUI_DB_BACKEND" "sqlite" "$ENV_FILE"
    info "CHATUI_DB_BACKEND=sqlite pinned in .env"
elif [[ -n "$PG_INSTALLED_MAJOR" ]]; then
    _set_env_var "CHATUI_DB_BACKEND" "postgres" "$ENV_FILE"
    info "CHATUI_DB_BACKEND=postgres pinned in .env (PG ${PG_INSTALLED_MAJOR})"
fi

ok ".env ready (PORT=${PORT})"

# ═══════════════════════════════════════════════════════════════
#  Step 10: Launch or print completion
# ═══════════════════════════════════════════════════════════════
echo ""
ok "Installation complete!"
echo ""
echo "  To activate this env later:"
echo "    conda activate ${ENV_NAME}"
echo "    cd ${INSTALL_DIR}"
echo "    python server.py"
echo ""
info "Full install log: $TOFU_INSTALL_LOG"
echo ""

if [[ "$NO_LAUNCH" -eq 1 ]]; then
    info "Install-only mode — not launching server."
    exit 0
fi

step "Starting Tofu server"
echo ""
echo -e "  ${BOLD}🧈 Tofu is starting on port ${PORT}...${NC}"
echo -e "  Open ${BOLD}http://localhost:${PORT}${NC} in your browser"
echo ""
echo "  Press Ctrl+C to stop the server"
echo ""

cd "$INSTALL_DIR"
exec python server.py
