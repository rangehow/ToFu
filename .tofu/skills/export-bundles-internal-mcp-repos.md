# export.py bundles sibling internal MCP repos under vendor/

## Why

`hope-mcp` and `xuecheng-mcp` are private Meituan-internal sibling
repos at `<ROOT>/../hope-mcp` and `<ROOT>/../xuecheng-mcp`.  They are
**not on PyPI**, so a fresh personal/internal install otherwise has
no `hope-mcp` / `xuecheng-mcp` on PATH and the MCP tab's Install
button fails with `launcher 'xuecheng-mcp' is not on PATH`.

## How

`export.py::_bundle_internal_mcp_repos(dest, mode)` (~line 1449):
- Runs only for `personal` and `internal` modes (never opensource)
- Copies sibling repos into `<dest>/vendor/<name>/`
- Skips build artifacts, caches, .git, etc.
- Shows a `📦 Bundled internal MCP repos → vendor/` line on success

`install.sh` (~line 879, before the Docling step):
- After the conda env is created and pip-only deps installed, scans
  `vendor/hope-mcp` and `vendor/xuecheng-mcp` for `pyproject.toml`
- If present, runs `python -m pip install --upgrade <path1> <path2>`
- Console scripts `hope-mcp` / `xuecheng-mcp` end up on the env PATH

## Modes

| Mode | Bundled? | Reason |
|---|---|---|
| personal | yes | Re-installing on another machine |
| internal | yes | Colleagues use these MCPs |
| opensource | NO | Internal-only repos; `_sanitize_source_opensource` already strips registry entries |

## Don't regress

- Sibling repos must remain at `<ROOT>/../hope-mcp` and
  `<ROOT>/../xuecheng-mcp` for the bundler to find them
- Bundle skips `.git/__pycache__/dist/build/.venv` etc.
- Bundle is fail-soft: missing sibling repo is logged as INFO, not an
  error
- Layered with the other corp-network fixes: `TOFU_PYPI_INDEX` makes
  pip respect the Sankuai mirror, but `pip install <path>` doesn't
  actually need the index — local install is fine
- File: `lib/mcp/registry.py` already has the `'command': 'hope-mcp'`
  and `'command': 'xuecheng-mcp'` entries; users only need to click
  Install in Settings → MCP

## Verification after export

```
ls ../tofu-<name>/vendor/
  hope-mcp  xuecheng-mcp

ls ../tofu-<name>/vendor/hope-mcp/src/
  hope_mcp
```
