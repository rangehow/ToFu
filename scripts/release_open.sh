#!/usr/bin/env bash
# release_open.sh — one-command tofu → tofu-open (GitHub rangehow/ToFu) release.
#
#   scripts/release_open.sh <prepared-source-ref> [--push] [--history-base REF] \
#       [--harbor-repo PATH]
#
# Why this exists (2026-09-04/05 smoke-run postmortems):
#   * export.py resolves its source ref as TOFU_EXPORT_SOURCE_REF >
#     `tofu.exportRef` config > refs/tofu/stable > HEAD — and refs/tofu/stable
#     sat on a 2026-08-27 snapshot, so two "successful" exports shipped a tree
#     with no `run` subcommand and 103/103 trials died in AgentSetupError.
#   * "push succeeded" was confused with "content is fresh"; nobody grepped
#     the exported tree before pushing.
#
# This script therefore:
#   1. pins TOFU_EXPORT_SOURCE_REF EXPLICITLY (required argument; no implicit
#      stable fallback);
#   2. exports (pass 1, no push) and runs freshness gates on the exported
#      tree — blob equality against the source ref plus the four markers that
#      would have caught the stale export;
#   3. runs the exported-tree gates: pinned `ruff check .` (public lint-lane
#      parity) and `pytest --collect-only` over the shipped tests — the two
#      cheapest checks that would have caught every "push first, discover red
#      CI on GitHub later" incident of 2026-08/09 (keep-list drift, sanitizer
#      corruption, conftest import breakage). export.py itself already runs
#      the AST test-surface gate, secret scan and syntax/integrity verifiers;
#   4. only with --push publishes the exact gated index tree, synchronizes
#      sanitized commits and tags its already-prepared version. It never bumps
#      metadata after freezing the source and never re-exports the gated tree.
set -euo pipefail

SOURCE_REF=""
HISTORY_BASE=""
DO_PUSH=0
HARBOR_REPO=""
TOFU_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOFU_OPEN_DIR="${TOFU_OPEN_DIR:-$TOFU_REPO/../tofu-open}"

while [ $# -gt 0 ]; do
    case "$1" in
        --bump) echo 'prepare and commit all release metadata before freezing source; late --bump is forbidden' >&2; exit 2 ;;
        --history-base) HISTORY_BASE="$2"; shift 2 ;;
        --push) DO_PUSH=1; shift ;;
        --harbor-repo) HARBOR_REPO="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "unknown flag: $1" >&2; exit 2 ;;
        *) SOURCE_REF="$1"; shift ;;
    esac
done

if [ -z "$SOURCE_REF" ]; then
    echo "usage: release_open.sh <prepared-source-ref> [--push] [--history-base REF] [--harbor-repo P]" >&2
    echo "refusing to pick a source ref implicitly (stable-ref stale-export trap)" >&2
    exit 2
fi

cd "$TOFU_REPO"
SOURCE_SHA="$(git rev-parse --verify "$SOURCE_REF^{commit}")"
echo ">> source ref: $SOURCE_REF = $SOURCE_SHA"

run_export() {
    local extra=("$@")
    TOFU_EXPORT_SOURCE_REF="$SOURCE_SHA" \
        python3 export.py --mode opensource --dest "$TOFU_OPEN_DIR" ${extra[@]+"${extra[@]}"}
}

echo ">> pass 1: export (no push) → $TOFU_OPEN_DIR"
run_export

GATE_DIR="$(mktemp -d -t tofu-open-gate.XXXXXXXX)"
trap 'rm -rf -- "$GATE_DIR"' EXIT
GATED_TREE="$(python3 - "$TOFU_OPEN_DIR" "$GATE_DIR/tree" <<'PY'
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
from export_pkg._publish import _verify_staged_publish_snapshot

public = Path(sys.argv[1])
if not (public / '.git').exists():
    subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=public, check=True)
subprocess.run(['git', 'add', '-A'], cwd=public, check=True)
with redirect_stdout(sys.stderr):
    tree = _verify_staged_publish_snapshot(public, snapshot_dest=Path(sys.argv[2]))
print(tree)
PY
)"
GATED_DIR="$GATE_DIR/tree"
# In the clean temporary snapshot, preserve tracked files even when the copied
# .gitignore excludes their parent. This is a gate repository, never a push source.
git -C "$GATED_DIR" init -q
git -C "$GATED_DIR" add -f --all

echo ">> freshness gates on immutable tree $GATED_TREE"

fail=0
gate() { # gate <label> <ok:0/1>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi
}

src_blob="$(git rev-parse "$SOURCE_SHA:tofu_agent/cli.py")"
out_blob="$(git hash-object "$GATED_DIR/tofu_agent/cli.py")"
gate "cli.py blob == source blob ($src_blob)" "$([ "$src_blob" = "$out_blob" ] && echo 0 || echo 1)"

n="$(grep -c 'run_parser' "$GATED_DIR/tofu_agent/cli.py" || true)"
gate "run subcommand present (run_parser x$n)" "$([ "$n" -ge 1 ] && echo 0 || echo 1)"

n="$(grep -c 'atif' "$GATED_DIR/lib/trajectory.py" 2>/dev/null || true)"
gate "atif trajectory format present (x$n)" "$([ "$n" -ge 1 ] && echo 0 || echo 1)"

if grep -A30 '^dependencies' "$GATED_DIR/pyproject.toml" | grep -q 'pymupdf-layout'; then
    gate "pyproject slim defaults (pymupdf-layout NOT in default deps)" 1
else
    gate "pyproject slim defaults (pymupdf-layout NOT in default deps)" 0
fi

n="$(grep -rc 'TOFU_LOOP_EXTENDED' "$GATED_DIR/lib/" 2>/dev/null | awk -F: '{s+=$2} END {print s+0}' || true)"
gate "TOFU_LOOP_EXTENDED present (x$n)" "$([ "$n" -ge 1 ] && echo 0 || echo 1)"

if [ "$fail" -ne 0 ]; then
    echo ">> freshness gates FAILED — not pushing. The exported tree is stale or wrong." >&2
    exit 1
fi

echo ">> exported-tree gates: pinned ruff + pytest collection"
echo "   (the public CI lints the whole tree and collects every shipped test;"
echo "    a failure here is a red GitHub lane discovered BEFORE push, not after)"
(
    cd "$GATED_DIR"
    echo "  [1/2] uv run --frozen --extra dev ruff check ."
    uv run --frozen --extra dev ruff check .
    # Collection must mirror the public unit lane: its Makefile ignores the
    # node/jsdom harness files (no npm ci on that lane), so a bare pytest
    # collect over tests/ would report false collection errors.
    echo "  [2/2] make test-collect (CI-visible unit surface, collection only)"
    uv run --frozen --extra test make test-collect
) || {
    echo ">> exported-tree gates FAILED — not pushing." >&2
    echo "   Fix the source or the export keep-list, then re-run." >&2
    exit 1
}

if [ "$DO_PUSH" -eq 0 ]; then
    echo ">> gates green; re-run with --push to publish (dry run ends here)"
    exit 0
fi

echo ">> publish the exact gated candidate (no re-export or version bump)"
TOFU_EXPORT_SOURCE_REF="$SOURCE_SHA" \
    python3 - "$TOFU_OPEN_DIR" "$SOURCE_SHA" "$HISTORY_BASE" "$GATED_TREE" <<'PY'
from pathlib import Path
import subprocess
import sys
from export_pkg import _state
from export_pkg._history import synchronize_public_history
from export_pkg._lists import ROOT
from export_pkg._publish import _git_push

public, source_sha, history_base = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
_state._EXPORT_SOURCE_SHA = source_sha
_state._EXPORT_VERSION = subprocess.check_output(
    ['git', 'show', source_sha + ':VERSION'], cwd=ROOT, text=True).strip()

def history_builder(destination, verified_tree, message, *, published_tip=None):
    return synchronize_public_history(
        ROOT, destination, source_sha, verified_tree, message,
        history_base=history_base or None, published_tip=published_tip).public_tip

_git_push(public, 'opensource', 'Release ' + _state._EXPORT_VERSION + ' (opensource)',
          is_release=True, history_builder=history_builder, expected_tree=sys.argv[4])
PY

tag="v$(git show "$SOURCE_SHA:VERSION" | tr -d '[:space:]')"
pin="git+https://github.com/rangehow/ToFu.git@$tag"
echo ">> released tag: $tag"
echo ">> harbor pin:   DEFAULT_TOFU_INSTALL_SPEC = \"$pin\""

if [ -n "$HARBOR_REPO" ]; then
    kernel="$HARBOR_REPO/src/harbor/agents/installed/agentix/kernels/tofu.py"
    sed -i "s|^DEFAULT_TOFU_INSTALL_SPEC = .*|DEFAULT_TOFU_INSTALL_SPEC = \"$pin\"|" "$kernel"
    echo ">> updated $kernel — review + commit + push the harbor branch yourself:"
    git -C "$HARBOR_REPO" diff -- src/harbor/agents/installed/agentix/kernels/tofu.py
fi
