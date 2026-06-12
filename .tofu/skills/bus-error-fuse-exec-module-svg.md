---
name: bus-error-fuse-exec-module-svg
description: png2svg deps: vtracer (pip) + numpy/scipy (conda) now HARD deps in install.sh; generate_image(svg=true) silent-failure fixed; module cached to avoid FUSE SIGBUS
enabled: true
tags: [crash, sigbus, fuse, importlib, svg, image-gen]
created: 2026-05-28T03:51:49Z
updated: 2026-06-04T06:48:53Z
---

## SIGBUS from `exec_module(png_to_svg)` on FUSE (Fixed 2026-05-28)

### Symptom
`Bus error (core dumped)` — entire Python process killed. Looks sudden, can
coincide with unrelated SSE streaming errors (different threads, same process).

### Root cause
`lib/tasks_pkg/executor_image.py::_convert_to_svg()` used to reload
`scripts/png_to_svg.py` via `importlib.util.exec_module()` on EVERY
`generate_image(svg=True)` call. That script imports `xml.etree.ElementTree`
→ loads the `_elementtree` C-extension .so. On a dolphinfs FUSE mount, a
transient hiccup during `create_module()` → SIGBUS.

### Fix
Cache the module at module-level with a `threading.Lock()` double-check
(`_PNG_TO_SVG_MOD` / `_load_png_to_svg()` in executor_image.py). Never
`exec_module()` a script on a FUSE mount in a hot path.

## png2svg dependency stack (made HARD deps 2026-06)
`generate_image` ALWAYS advertises the `svg` boolean param, so the stack must
always work — it is NOT an optional capability anymore.

Two tiers, both wired into `install.sh`:
- **vtracer** (raster→vector tracer) — `PIP_ONLY_PKGS` (`vtracer>=0.6.11`).
  Self-contained Rust wheel, no Python deps, doesn't touch lxml/icu → safe
  under `--no-deps`. Has its own import probe ("Verifying PNG→SVG stack")
  that self-heals via plain `pip install vtracer` (NO lxml constraint needed).
- **numpy + scipy** (background-removal flood-fill / connected-components in
  `scripts/png_to_svg.py::_remove_background`) — `CONDA_PKGS` + added to the
  conda import-check list. Without them bg-removal silently degrades.

If vtracer/numpy/scipy missing, `scripts/png_to_svg.py` logs and returns
False/skips — it does NOT crash.

## Silent-failure fix (2026-06)
Previously: `generate_image(svg=true)` where `_convert_to_svg` returned
('','') told the model "Image generated successfully" with NO mention the SVG
failed. Now `register_image_gen_handler` computes
`svg_failed = not (svg_saved_url or svg_project_path)` and:
- appends a "Note: SVG conversion was requested but failed" line to the
  model-facing `_text_fallback`, and
- adds " ⚠ svg failed" to the result badge.

## Test
`python3 scripts/png_to_svg.py --all` converts static/icons/*.png. WARNING:
this OVERWRITES tracked .svg files in static/icons/ — `git checkout --` them
after testing so you don't commit re-traced assets.

