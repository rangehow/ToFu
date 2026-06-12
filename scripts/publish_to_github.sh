#!/usr/bin/env bash
#
# publish_to_github.sh — push the in-process façade + installable-package work
# to GitHub so a fresh server can `pip install git+https://…/ToFu.git`.
#
# Run this ON THE DEV MACHINE (this chatui checkout). It is deliberately
# CONSERVATIVE: this working tree has unrelated stray deletions under
# `.tofu/file-history/`, so we DO NOT `git add -A`. We add only the curated
# set of paths that make up this feature.
#
# Requires: a GitHub remote you can push to (HTTPS token or SSH key already
# configured in your shell). This script does NOT store credentials.
#
# Usage:
#   GIT_REMOTE=git@github.com:rangehow/ToFu.git scripts/publish_to_github.sh
#   # optional: TAG=v0.5.1 BRANCH=master
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(cd "$SCRIPT_DIR/.." && pwd)"

GIT_REMOTE="${GIT_REMOTE:-https://github.com/rangehow/ToFu.git}"
BRANCH="${BRANCH:-master}"
TAG="${TAG:-}"

echo "==> Repo: $(pwd)"
echo "==> Remote target: $GIT_REMOTE   branch: $BRANCH   tag: ${TAG:-<none>}"

# ── 1. Ensure an 'origin' remote exists and points where we expect ────────
if git remote get-url origin >/dev/null 2>&1; then
    CUR="$(git remote get-url origin)"
    if [[ "$CUR" != "$GIT_REMOTE" ]]; then
        echo "    origin currently = $CUR"
        echo "    (leaving it as-is; pass GIT_REMOTE to match if this is wrong)"
    fi
else
    echo "==> No 'origin' remote — adding it."
    git remote add origin "$GIT_REMOTE"
fi

# ── 2. Stage ONLY the feature paths (never `git add -A` here) ─────────────
PATHS=(
    pyproject.toml
    tofu/
    lib/tasks_pkg/entry.py
    lib/tasks_pkg/model_config.py
    lib/tasks_pkg/orchestrator.py
    lib/tasks_pkg/llm_fallback.py
    lib/llm/body.py
    lib/compat/openai.py
    routes/api_v1/chat.py
    docs/HEADLESS_API.md
    docs/COMPAT_OPENAI.md
    docs/proposals/IN_PROCESS_FACADE.md
    tests/test_inprocess_facade.py
    tests/test_backend_unit.py
    tests/test_compat_openai.py
    tests/test_cc_alignment.py
    scripts/build_and_deploy_wheel.sh
    scripts/publish_to_github.sh
)
echo "==> Staging curated paths…"
for p in "${PATHS[@]}"; do
    if [[ -e "$p" ]]; then git add -- "$p"; else echo "    skip (absent): $p"; fi
done

echo "==> Staged diff stat:"
git diff --cached --stat

# ── 3. Commit (skip cleanly if nothing changed) ───────────────────────────
if git diff --cached --quiet; then
    echo "==> Nothing staged to commit — already up to date."
else
    git commit -m "feat: installable package + in-process tofu façade + response_format wiring

- Make chatui pip-installable (standard setuptools backend, package
  discovery for tofu/lib/routes, deps from requirements.txt).
- Add top-level \`tofu\` in-process façade (chat/stream/capabilities) over a
  shared kernel lib/tasks_pkg/entry.py; HTTP route reuses build_chat_config.
- Wire response_format (JSON mode) end-to-end through dispatch + fallback.
- Document error taxonomy (HEADLESS_API §3.8) and the façade (§4.5)."
fi

# ── 4. Push branch, then tag ──────────────────────────────────────────────
echo "==> Pushing $BRANCH to origin…"
git push -u origin "$BRANCH"

if [[ -n "$TAG" ]]; then
    if git rev-parse "$TAG" >/dev/null 2>&1; then
        echo "==> Tag $TAG already exists locally — pushing it."
    else
        git tag "$TAG"
    fi
    git push origin "$TAG"
    echo "==> Pushed tag $TAG"
fi

echo
echo "==> DONE. On the new server run:"
REF="${TAG:-$BRANCH}"
echo "    scripts/install_on_server.sh   # (copy it over, or paste the one-liner)"
echo "    # or directly:"
echo "    pip install \"git+$GIT_REMOTE@$REF\""
