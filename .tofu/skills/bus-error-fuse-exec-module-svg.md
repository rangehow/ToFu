---
name: bus-error-fuse-exec-module-svg
description: SIGBUS crash root cause: repeated importlib.exec_module of png_to_svg.py on FUSE mount triggering C-extension mmap failures; fixed by caching module on first load
enabled: true
tags: [crash, sigbus, fuse, importlib, svg, image-gen]
created: 2026-05-28T03:51:49Z
updated: 2026-05-28T03:51:49Z
---

# SIGBUS from `exec_module(png_to_svg)` on FUSE (Fixed 2026-05-28)

## Symptom
`Bus error (core dumped)` — entire Python process killed. Looks like it
happens "suddenly" and can coincide with unrelated SSE streaming errors in
the logs because both run in different threads under the same process.

## Root cause
`lib/tasks_pkg/executor_image.py::_convert_to_svg()` used to reload
`scripts/png_to_svg.py` via `importlib.util.exec_module()` on EVERY
`generate_image(svg=True)` call. That script imports
`xml.etree.ElementTree` which triggers loading of the `_elementtree`
C-extension shared object. When the project lives on a dolphinfs FUSE
mount, a transient hiccup (truncated `.pyc`, interrupted mmap) during
the `create_module()` call → SIGBUS.

## faulthandler dump signature
```
Fatal Python error: Bus error
Current thread …:
  File "<frozen importlib._bootstrap>" … in create_module
  File ".../xml/etree/ElementTree.py" …
  File ".../scripts/png_to_svg.py", line 24
  File ".../executor_image.py" … in _convert_to_svg
```

## Fix
Cache the module at module-level with a `threading.Lock()` double-check:
```python
_PNG_TO_SVG_MOD = None
_PNG_TO_SVG_LOCK = threading.Lock()

def _load_png_to_svg():
    global _PNG_TO_SVG_MOD
    if _PNG_TO_SVG_MOD is not None:
        return _PNG_TO_SVG_MOD
    with _PNG_TO_SVG_LOCK:
        if _PNG_TO_SVG_MOD is not None:
            return _PNG_TO_SVG_MOD
        ...exec_module...
        _PNG_TO_SVG_MOD = mod
    return _PNG_TO_SVG_MOD
```

## Lesson
Never `exec_module()` a script on a FUSE mount in a hot path — any
C-extension in its import chain can crash the whole process. Load once
and cache.

