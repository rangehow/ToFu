---
name: swebench-runner-tool-ablation-variants
description: ModelConfig.config_overrides + mcpEnabled/fetchEnabled gates enable SWE-bench tool-ablation variants (e.g. tofu-opus-notool = project tools only)
enabled: true
tags: [swebench, ablation, benchmark, tool-gating, mcp, fetch]
created: 2026-04-19T12:34:41Z
updated: 2026-04-19T12:34:41Z
---

# SWE-bench Tool-Ablation Variants

To A/B test Tofu with different tool subsets on SWE-bench without editing the server or creating one-off scripts:

## Mechanism

`ModelConfig` in `debug/swebench_runner.py` has a `config_overrides: dict` field. These keys are merged into the `/api/chat/start` `config` payload, overriding server defaults per-run.

```python
'tofu-opus-notool': ModelConfig(
    ...,
    config_overrides={
        'searchMode': 'off',      # strip web_search
        'fetchEnabled': False,    # strip fetch_url
        'memoryEnabled': False,   # strip memory tools
        'mcpEnabled': False,      # strip MCP bridge tools
    },
),
```

Run both groups:
```bash
python debug/swebench_runner.py --models tofu-opus,tofu-opus-notool --all \
    --output swebench_workdir/ab_notool.json
```

## Config keys that gate tools (lib/tasks_pkg/model_config.py)

| Key | Default | Tool effect when falsy |
|---|---|---|
| `searchMode` | `'multi'` | `'off'` → no SEARCH_TOOL |
| `fetchEnabled` | `True` | `False` → no FETCH_URL_TOOL (note: search still adds fetch if search_enabled) |
| `memoryEnabled` | `True` | `False` → no memory tools |
| `mcpEnabled` | `True` (added 2026-04-19) | `False` → MCP bridge not queried |
| `browserEnabled` / `desktopEnabled` / `swarmEnabled` / `codeExecEnabled` / `imageGenEnabled` / `humanGuidanceEnabled` / `schedulerEnabled` | `False` | already off by default |
| `projectPath` set | — | enables PROJECT_TOOLS (always wanted for SWE-bench) |
| `keepToolHistory` | `True` | keep ON; does not affect within-turn tool visibility |

## Gotchas

- `fetchEnabled` was hardcoded `True` before 2026-04-19; now honors cfg.
- `mcpEnabled` was ungated; MCP bridge was always loaded if connected. Now respects cfg.
- `fetch_url` is still added if `search_enabled` is true — so to fully strip fetch, must set BOTH `searchMode='off'` AND `fetchEnabled=False`.
- PROJECT_TOOLS includes `run_command`, so `codeExecEnabled` becomes moot when project is set.
- `emit_to_user` is always appended when `tool_list` is non-empty (not ablatable).

