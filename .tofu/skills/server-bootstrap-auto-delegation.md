---
name: server-bootstrap-auto-delegation
description: Auto-delegation from server.py to bootstrap.py on ImportError: sys.excepthook guard triggers os.execv to bootstrap.py, _CHATUI_VIA_BOOTSTRAP=1 env var prevents infinite re-exec loop
enabled: true
tags: [python, bootstrap, auto-delegation, ImportError, sys.excepthook, os.execv, dependency-repair]
created: 2026-03-30T06:38:03Z
updated: 2026-03-30T06:38:03Z
---

# server.py ↔ bootstrap.py Auto-Delegation

## Problem
Users naturally run `python server.py` (as documented). In a fresh conda env missing `flask`/`flask-compress`, 
the process dies with a raw traceback at the first third-party import. `bootstrap.py` (the LLM-guided auto-repair 
launcher) was never triggered because there was no bridge.

## Solution: `sys.excepthook` + `os.execv`

### server.py (early, before third-party imports)
```python
_BOOTSTRAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bootstrap.py')

if (os.environ.get('_CHATUI_VIA_BOOTSTRAP') != '1'
        and os.path.isfile(_BOOTSTRAP_PATH)):

    def _bootstrap_excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, ImportError):
            # Print traceback, then replace process with bootstrap.py
            os.execv(sys.executable, [sys.executable, _BOOTSTRAP_PATH])
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _bootstrap_excepthook
```

### bootstrap.py (_try_start_server)
```python
env = os.environ.copy()
env['_CHATUI_VIA_BOOTSTRAP'] = '1'  # prevent infinite loop
proc = subprocess.Popen([sys.executable, 'server.py'], env=env, ...)
```

## Key Design Points
1. **`sys.excepthook`** fires for **unhandled** exceptions — module-level `ImportError` from `from flask import ...` is unhandled
2. **`os.execv`** replaces the current process entirely (PID preserved, no return)
3. **`_CHATUI_VIA_BOOTSTRAP=1`** env var prevents infinite `server.py → bootstrap.py → server.py → ...` loop
4. Only stdlib used before the hook (os, sys) — works even when every pip package is missing
5. If `bootstrap.py` doesn't exist, the guard is skipped — normal Python error handling applies
6. Non-`ImportError` exceptions pass through to the default handler unchanged

