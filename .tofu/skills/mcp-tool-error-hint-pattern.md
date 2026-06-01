---
name: mcp-tool-error-hint-pattern
description: MCP wrapper pattern: detect auth-expired centrally, emit login_required + error_hint so LLM stops retrying
enabled: true
tags: [mcp, hope-mcp, ux, pattern, tool-design]
created: 2026-04-22T12:07:43Z
updated: 2026-04-22T12:07:43Z
---

# Pattern: centralize auth-expired detection in MCP CLI wrappers

## Problem
When an MCP tool wraps a CLI that requires a live session (e.g. `hope`,
`aws`, `kubectl`), calling the CLI without a session fails with a
generic non-zero exit + stderr like "Please login first". The LLM sees a
plain `{ok:false, stderr:"..."}` dict and has no idea this is a
structural auth problem — so it **retries the same call forever**, or
silently gives up.

## Fix
Detect the auth-expired signal in the **central subprocess wrapper**
(not in every tool) and attach TWO machine-readable fields to the result:

1. `login_required: true` — boolean flag the LLM can check
2. `error_hint: "<crisp actionable remediation>"` — tells the LLM
   *exactly* which tool to call next and that **retrying won't help**

Because every tool already does `**result.to_dict()`, enriching
`to_dict()` once makes every tool surface the hint for free.

## Reference implementation
`hope-mcp/src/hope_mcp/cli.py`:

```python
_LOGIN_REQUIRED_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"please\s+login", r"not\s+logged\s+in", r"session\s+expired",
    r"token\s+(expired|invalid)", r"unauthori[sz]ed",
    r"authentication\s+failed", r"hope\s+login",
    r"请.*登录", r"登录.*已.*过期", r"登录.*失效",   # Chinese variants
))

def _looks_like_login_required(stdout, stderr, rc):
    if rc == 0:  # Critical: only flag non-zero; don't false-positive on
        return False  # job names like "prod-login-service" in a rc=0 JSON payload.
    return any(p.search((stderr or "") + "\n" + (stdout or "")) for p in _LOGIN_REQUIRED_PATTERNS)
```

`HopeResult.to_dict()` emits:
```python
if self.login_required:
    d["login_required"] = True
    d["error_hint"] = ("hope reports this session is invalid... "
                       "Retrying the same call WILL NOT help. "
                       "Call hope_login(username=...) then re-run.")
```

## Gotchas
- **Only flag on rc != 0.** A successful `hope ls --json` whose output
  happens to contain a job named `prod-login-service` must not be
  misclassified.
- **Don't flag on timeout.** A hung cluster is a timeout, not auth.
  Keep the two classes disjoint.
- **Include both EN and 中文 patterns** for any Chinese-localized CLI.
- Tests should cover: each tool surfaces the flag, rc=0 never triggers,
  timeout never triggers, generic non-auth errors pass through clean.

