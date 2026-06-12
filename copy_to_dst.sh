#!/usr/bin/env bash
#
# copy_to_dst.sh — Copy the exported Tofu project onto an EXISTING older
# copy at DST, overwriting code but PRESERVING the destination's chat
# history.
#
# Why this is safe for chat records:
#   - The chat history lives in DST/data/ (PostgreSQL pgdata/ or SQLite *.db).
#   - We NEVER `rm -rf "$DST"` and we EXCLUDE data/ from the copy, so the
#     destination's data/ is left completely untouched.
#   - We do NOT pass rsync --delete, so files removed upstream are NOT
#     pruned from DST. Conservative choice: "don't break what's there".
#   - We also skip .tofu and .git (per-host agent state / git history).
#
# Speed: rsync is preferred. On RE-RUNS it only transfers changed files
# (the DST already has an older copy), which is the big win over re-taring
# everything. Falls back to a streaming tar pipe if rsync is unavailable.
#
set -euo pipefail

SRC="${SRC:-/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/tofu-meituan}"
DST="${DST:-/mnt/dolphinfs/ssd_pool/docker/user/hadoop-autoresearch/chenxin/tofu-chenxin}"

[[ -d "$SRC" ]] || { echo "ERROR: SRC not found: $SRC" >&2; exit 1; }

echo "SRC: $SRC"
echo "DST: $DST"

# Sanity: refuse to run if SRC == DST.
if [[ "$(cd "$SRC" && pwd)" == "$(cd "$DST" 2>/dev/null && pwd || echo /nonexistent)" ]]; then
    echo "ERROR: SRC and DST resolve to the same directory — aborting." >&2
    exit 1
fi

mkdir -p "$DST"

if [[ -e "$DST/data" ]]; then
    echo "NOTE: preserving existing DST/data (chat history) — not overwritten."
else
    echo "NOTE: DST has no data/ yet — a fresh DB will bootstrap on first run."
fi

# Paths to skip (relative to the copy root). data/ is the critical one.
EXCLUDES=( data .tofu .git '*.pyc' __pycache__ )

if command -v rsync >/dev/null 2>&1; then
    echo "Using rsync (incremental; only changed files transfer on re-runs)..."
    RSYNC_EXCLUDES=()
    for e in "${EXCLUDES[@]}"; do RSYNC_EXCLUDES+=( --exclude="$e" ); done

    # -a archive, -h human sizes, --info=progress2 = single live progress bar.
    # Trailing slash on "$SRC/" copies CONTENTS of SRC into DST.
    # No --delete → DST/data and any extra DST files are preserved.
    rsync -ah --info=progress2 "${RSYNC_EXCLUDES[@]}" "$SRC/" "$DST/"
else
    echo "(rsync not found; using tar pipe fallback)"
    TAR_EXCLUDES=()
    for e in "${EXCLUDES[@]}"; do
        case "$e" in
            *'*'*) TAR_EXCLUDES+=( --exclude="$e" ) ;;   # glob, no ./ anchor
            *)     TAR_EXCLUDES+=( --exclude="./$e" ) ;; # path, anchor at root
        esac
    done

    if command -v pv >/dev/null 2>&1; then
        echo "Estimating size..."
        SIZE_BYTES="$(du -sb --exclude=data --exclude=.tofu --exclude=.git \
                         --exclude='*.pyc' --exclude=__pycache__ "$SRC" 2>/dev/null \
                      | awk '{print $1}')"
        if [[ -n "${SIZE_BYTES:-}" && "$SIZE_BYTES" -gt 0 ]]; then
            tar -C "$SRC" "${TAR_EXCLUDES[@]}" -cf - . | pv -s "$SIZE_BYTES" | tar -C "$DST" -xf -
        else
            tar -C "$SRC" "${TAR_EXCLUDES[@]}" -cf - . | pv | tar -C "$DST" -xf -
        fi
    else
        echo "(install 'pv' or 'rsync' for a progress bar; using tar checkpoints)"
        tar -C "$SRC" "${TAR_EXCLUDES[@]}" \
            --checkpoint=1000 --checkpoint-action=echo='  copied %u files...' \
            -cf - . | tar -C "$DST" -xf -
    fi
fi

echo "Done. Code synced to $DST; DST/data/ left intact."
