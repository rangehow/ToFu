---
name: file-changes-bar-multi-root-prefix
description: File-changes bar now shows rootname: prefix in multi-root workspaces via mod['root'] stored at record time
enabled: true
tags: [javascript, python, multi-root, ui, file-changes-bar, undo, modifications]
created: 2026-04-21T03:59:20Z
updated: 2026-04-21T03:59:20Z
---

# File-changes bar — multi-root workspace prefix

## Problem
In multi-root workspaces (primary + extras via `create_project` or
multi-path setup), the `.file-changes-bar` showed only relative paths
like `foo.py`, with no indication of which project root the file was in.
Two different files `projectA/foo.py` and `projectB/foo.py` collapsed
into one entry.

## Fix (3 layers)

### 1. Backend — record root name per modification
`lib/project_mod/modifications.py` `_record_modification()` now imports
`_roots` and does a reverse lookup: for the given `base_path` (which is
the *resolved* root absolute path from `_resolve_base` /
`resolve_namespaced_path`), find the matching root name in `_roots` and
store it as `mod['root']`.

### 2. Orchestrator — include `root` in modifiedFileList
`lib/tasks_pkg/orchestrator.py` `_emit_done_event` (around line 340–380)
now keys the dedup map by `(root, path)` instead of just `path`, and
emits `{path, action, root}` entries. Checkpoint merge (_cp_mod_list)
also keys by `(root, path)` to avoid collapsing across roots.

### 3. Frontend — render `rootname:` prefix when multi-root active
`static/js/ui.js`:
- `renderFileChangesBar` propagates `f.root` from `modifiedFileList`.
- `_renderFileChangesHtml` detects multi-root mode via
  `projectState.extraRoots.length > 0` OR multiple distinct roots seen
  in the files list, and prefixes each file with
  `<span class="fc-root">rootname:</span>`.
- `_extractFileChangesFromRounds` parses `rootname:rel/path` from
  `toolArgs.path` so live streaming view also carries root info. Dedup
  keyed by `"root|path"`. Returns `root` field on each entry.

### 4. CSS
`static/styles.css` — added `.fc-root` style (blue pill, similar to
tool-display colors).

## Multi-root detection
`_showRootPrefix = (projectState.extraRoots.length > 0) || (distinct root names in files > 1)`
This means in single-root projects, paths remain unprefixed (backward-compatible).

## Testing
- `ast.parse` on both .py files — OK
- `new Function(...)` parse on ui.js — OK

