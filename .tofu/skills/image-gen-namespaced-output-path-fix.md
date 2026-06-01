---
name: image-gen-namespaced-output-path-fix
description: generate_image's _save_image_to_project must run output_path through _resolve_base or it creates dirs whose name contains 'rootname:'
enabled: true
tags: [image-gen, multi-root, bug, executor_image]
created: 2026-05-10T16:59:57Z
updated: 2026-05-10T16:59:57Z
---

# `generate_image`'s output_path must go through `_resolve_base`

## Symptom (2026-05-05)
`chatui:static/posters/` directory appeared at the project root with the
literal multi-root prefix `chatui:` baked into the directory name. Three
real PNG files inside (~5.7 MB).

## Cause
`lib/tasks_pkg/executor_image.py::_save_image_to_project` was calling
`_safe_path(project_path, output_path)` directly. `_safe_path` does a
literal `os.path.join`, so when the model passes
`output_path="chatui:static/posters/x.png"` (a valid multi-root prefix),
the colon stays in the filename and a top-level dir named `chatui:static/`
gets silently created under the primary root.

All other write tools (`tool_write_file`, `tool_apply_diff`,
`tool_insert_content`, batch versions) go through `_resolve_base` /
`_resolve_write_path` which understand the `name:rel` namespace prefix.
`_save_image_to_project` was the only write path that didn't.

## Fix
`_save_image_to_project` now calls `_resolve_base(project_path,
output_path, conv_id=conv_id)` before `_safe_path`, returns
`(display_path, eff_base, eff_rel)`, and `_record_modification` /
`_convert_to_svg` use the resolved base + rel so undo and SVG conversion
target the correct root.

## Lesson
Any new tool that takes a path arg and writes to disk MUST run the path
through `_resolve_base` first (or `_resolve_write_path` for absolute
paths). Direct `_safe_path` is OK only if the caller has already resolved
the namespaced prefix. Add a unit-test or smoke test that passes
`'name:rel/x'` and asserts no top-level `name:rel` directory is created.

