---
name: install-sh-ignores-tofu-env-marker-creates-second-miniforge
description: install.sh 已修：优先读 .tofu_env.json 找 conda；pip 走 _safe_pip_install 强制 prefix + 拒绝 --user fallback + Permission denied 直接 fail
enabled: true
tags: [install, conda, pip, bug]
created: 2026-05-31T04:41:43Z
updated: 2026-05-31T05:03:38Z
---

# install.sh — `.tofu_env.json` 优先读 + pip 防 ~/.local fallback（已修）

## 历史症状（2026-05 修复前）
重新跑 `bash install.sh`，pip 阶段失败：
```
ERROR: Could not install packages due to an OSError:
[Errno 13] Permission denied:
'/home/<user>/.local/lib/python3.12/site-packages/courlan'
WARNING: Ignoring invalid distribution ~ymupdf (.../envs/tofu/lib/python3.12/site-packages)
```

## 根因链
1. 裸 shell 跑 `bash install.sh`，`which conda` 找不到。
2. install.sh 的 `CONDA_BIN` 搜索只看 `${HOME}/miniforge3/bin/conda` / `/opt/...` / SIBLING_CONDA_DIR，**不读 `.tofu_env.json`**。
3. 找不到 → **新建一个 miniforge** 到一个**和 .tofu_env.json 不同**的位置。
4. 在那个新 env 跑 pip。新 env 的 site-packages 因跨 DC FUSE 短时不可写 → pip 自动 fallback 到 `--user`。
5. `~/.local/lib/python3.12/site-packages/courlan` sticky owner 不一致 → permission denied → install.sh fail。
6. 留下孤儿 `~ymupdf` / `~itz` distribution，后续 pip 一直 `WARNING: Ignoring invalid distribution`。

## 修复（2026-05-31）

### 1. conda 发现优先读 marker
`install.sh` 在 step 1 (existing user conda) **之前** 加了 step 0：
```bash
_TOFU_ENV_MARKER="${INSTALL_DIR}/.tofu_env.json"
if [[ -f "$_TOFU_ENV_MARKER" ]] && command -v python3 &>/dev/null; then
    _MARKER_BASE="$(python3 -c "...json.load(open(...)).get('conda_base','')")"
    if [[ -n "$_MARKER_BASE" && -x "${_MARKER_BASE}/bin/conda" ]] && _conda_version_ok "$_ver_raw"; then
        CONDA_BIN="${_MARKER_BASE}/bin/conda"
        # CONDA_OWNED_BY_US=1 仅当 marker_base == SIBLING_CONDA_DIR
    fi
fi
```
后续 step 1 (`existing_conda_candidates`) 加守卫 `[[ -n "$CONDA_BIN" ]]: ;` 不覆盖。

### 2. pip 强制 prefix + 拒绝 --user fallback
新 helper `_safe_pip_install`（install.sh ~line 880）：
```bash
_safe_pip_install() {
    local _log=$(mktemp ...)
    (
        export PIP_USER=0
        unset PYTHONUSERBASE
        python -m pip install --prefix "$ENV_PREFIX" "$@" 2>&1 | tee "$_log"
        exit "${PIPESTATUS[0]}"
    )
    local _rc=$?
    if [[ $_rc -ne 0 ]] && grep -qE 'Permission denied|\[Errno 13\]' "$_log"; then
        # Don't warn-and-continue: fail loud
        fail "pip install aborted on permission error — see messages above."
    fi
    return $_rc
}
```
应用到所有 4 处 pip：PIP_ONLY_PKGS（`--no-deps` + retry）、auto-heal、bundled MCP、docling。

## 重要不变量
- `_safe_pip_install` 永远 `--prefix "$ENV_PREFIX"` + `PIP_USER=0`，禁止 pip 写到 `~/.local`。
- Permission denied 出现 → `fail`（exit 1），从不 warn-and-continue。
- 任何新 pip 命令必须用 `_safe_pip_install`，禁止裸 `python -m pip install`（除 helper 内部那一次）。

## 孤儿清理
若 site-packages 出现 `~xxx` 目录（pip uninstall 中断留下的），手动清：
```bash
rm -rf <env>/lib/python3.12/site-packages/~*
```

## 相关文件
- `install.sh` (line ~258 marker probe / line ~880 _safe_pip_install)
- `.tofu_env.json` (写入逻辑在 install.sh line ~626)

