---
name: conftest-werkzeug-version-shim
description: tests/conftest.py shims werkzeug.__version__ for older Flask editable installs left in shared conda envs
enabled: true
tags: [testing, infra, flask]
created: 2026-05-09T07:39:28Z
updated: 2026-05-09T07:39:28Z
---

# Werkzeug `__version__` shim in tests/conftest.py

## Symptom
`pytest tests/test_message_queue.py` (or any test using `flask_client`) fails at
test setup with:

```
AttributeError: module 'werkzeug' has no attribute '__version__'
```

The traceback points at `flask/testing.py` building the `HTTP_USER_AGENT`
header.

## Root cause
On shared conda environments, a stray `__editable__.flask-2.3.0.dev0.pth`
(left behind by an older swebench workspace install) loads
`swebench_workdir/workspaces/pallets__flask-5014__tofu-opus/src` as Flask.
That older Flask still references `werkzeug.__version__`, but Werkzeug 3.x
no longer exports it as a module attribute.

You can confirm with:

```python
python -c "import flask, werkzeug; print(flask.__file__); print(getattr(werkzeug, '__version__', 'MISSING'))"
```

## Fix (already in tests/conftest.py)
A module-load shim at the top of `tests/conftest.py` populates
`werkzeug.__version__` from package metadata when missing. Inert on clean
machines where the attribute already exists. Do **not** modify the shared
conda environment.

```python
def _ensure_werkzeug_version():
    try:
        import werkzeug
    except ImportError:
        return
    if getattr(werkzeug, '__version__', None):
        return
    try:
        from importlib.metadata import version as _pkg_version
        werkzeug.__version__ = _pkg_version('werkzeug')
    except Exception:
        werkzeug.__version__ = '0+unknown'

_ensure_werkzeug_version()
```

## Don't try to scrub sys.path instead
The polluted `swebench_workdir/workspaces/.../src` is the ONLY place Flask
lives in this conda env (no `flask/` package in site-packages — only the
`.dist-info` and the `.pth`). Removing those paths from `sys.path` makes
`import flask` fail with `ModuleNotFoundError`. The shim is the right fix.

