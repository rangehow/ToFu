---
name: multi-root-cross-project-path-misrouting-fix
description: Multi-root design: absolute paths canonical; rootname: optional; single-root cache-stable. Read-only roots via root_state['access']='ro' + conv readOnlyPaths list, enforced in _resolve_write_path.
enabled: true
tags: [python, javascript, multi-root, project-tools, path-routing, bug-fix, cross-project, conv-switch, state-persistence]
created: 2026-03-31T09:55:17Z
updated: 2026-06-11T06:09:39Z
---

# Multi-Root Workspace — Design & Isolation

## Core design direction (2026-06 — UX smoothing)
LLMs don't reliably carry a synthetic `rootname:` prefix DSL across turns — they parrot the path string they last saw. Fix: **absolute paths are canonical, `rootname:` is optional shorthand.**

- `read_files` always accepts ANY absolute path (unrestricted, bypasses sandbox via `_read_absolute_file`).
- `write_file`/`apply_diff`/`insert_content` accept absolute paths too — `_resolve_write_path` (write_tools.py) auto-registers the nearest existing ancestor dir as an extra root, rejecting only forbidden system paths.
- Prompt block in `lib/project_mod/indexer.py::get_context_for_prompt` lists roots, abs-path examples FIRST, then `rootname:` as "optional convenience". Tool descs in `lib/tools/project.py` (`_MULTIROOT_PATH_HINT`) aligned.

## Single-root NEVER affected — 3 isolation gates
1. **Tool schemas**: `with_multiroot_hint()` only fires when `ctx.multiroot_active` (reads `cfg['projectPaths']` len>1). Single-root → byte-identical cache-stable schema.
2. **System prompt**: MULTI-ROOT block only when `extra_roots` non-empty.
3. **Cross-root auto-route** (`_resolve_base`): gated `len(roots_view)>1`.

## Read-only roots (2026-06)
Goal: attach some roots reference-only (incl. the PRIMARY — "just understand a dir"). Data model chosen for scalability:
- **Authoritative per-root attribute** `root_state['access']` = `'rw'`(default)/`'ro'` set by `_make_root_state(path, access=)`. Travels through BOTH global `_roots` and per-conv `_conv_roots` — same seam as every other root attr.
- **Transport/persistence** = parallel `readOnlyPaths` string list alongside `projectPaths` (NOT objects → existing convs/DB rows deserialize unchanged, empty list = today's all-writable behaviour). `projectPaths` stays the ordering source (cache key untouched).
- Flow: frontend `conv.readOnlyPaths` → `_buildConvSnapshot`/`_saveConvProjectPath` → `cfg['readOnlyPaths']` → orchestrator `_readonly_paths` → `ensure_project_state(..., readonly_paths=)` → `set_conv_roots`/`set_project(_paths)` stamp `access`. REST: PUT `/api/v1/project/paths` body `readOnlyPaths`. `get_state()`/`_get_roots_info()` echo `readOnly` per root + top-level `readOnly` for primary.
- **Enforcement = ONE choke point**: `config.is_readonly_path(abs_target, conv_id)` (deepest-containing-root wins, conv-scoped). Called inside `_resolve_write_path` (covers write_file/apply_diff/insert_content + batches, all path forms) → raises `ReadOnlyRootError(ValueError)` (write tools already `except ValueError` → surfaces to model). Plus explicit guards: `tool_create_project` (refuse RO dest) and `execute_tool` run_command (refuse DESTRUCTIVE cmd whose cwd is RO; read-only cmds like ls/grep still run). READS (read_files/list_dir/grep/find) are NEVER blocked.
- Prompt: indexer tags roots `(READ-ONLY)` + a paragraph telling the model edits will be refused. Frontend modal: per-row lock toggle (`_mpReadOnly` Set parallel to `_mpFolders`), `mp-row-readonly`/`mp-row-badge-ro`/`mp-row-lock` CSS.
- Tests: `tests/test_readonly_roots.py` (config resolve, write/diff/insert/create_project blocked, run_command cwd, all-writable regression). `tests/test_conv_config.py` expected-key sets include `readOnlyPaths`.

IMPORTANT: auto-registering an absolute-write target into global `_roots` does NOT flip a session's *schema* to multi-root (`multiroot_active` driven by `cfg['projectPaths']`). Keep it that way.

## Concurrency / conv-scoping
Path RESOLUTION + prompt ADVERTISEMENT + RO check must ALL source roots from `get_conv_roots(conv_id)` / `_conv_roots[conv_id]`, never global `_roots`. `resolve_namespaced_path(conv_id=)` and `is_readonly_path(conv_id=)` are STRICTLY isolated when the conv has its own registry.

## Verified
Single-root: no MULTI-ROOT prompt block, schema==base. RO: write/diff/insert/create_project/destructive-run_command refused with model-readable msg; read_files/list_dir/grep/ls allowed; primary-RO works; writable sibling unaffected; empty readOnlyPaths = unchanged. 64 tests green (test_readonly_roots + test_conv_config + test_agent_options).

