---
name: write-file-multi-root-stale-prefix
description: write_file silently routes to old workspace root if prefix is stale — always verify location with run_command after writes
enabled: true
tags: [tooling, gotcha, multi-root, write_file]
created: 2026-04-20T03:58:31Z
updated: 2026-04-20T03:58:31Z
---

# write_file routes to stale workspace root after project move

## Symptom
`write_file(path='rootname:foo/bar.py', ...)` reports **"✅ File created"** but the file does not appear in the expected directory. Pytest says "no tests ran" or "file not found".

## Root cause
When a workspace root is **moved or deleted** after registration, the registered root still points to the OLD path. Subsequent writes with that `rootname:` prefix (or even different prefix names created later) can silently fall back to the PRIMARY root — i.e. writes end up in the chatui tree when the user wanted them in hope-mcp.

Observed specifically when:
1. `create_project(path=/home/.../hope-mcp)` registered root "hope-mcp"
2. Directory was later `mv`'d to `/mnt/.../hope-mcp`
3. `create_project` was re-called for the new location with a different name ("hmcp" or "hope-mcp-sibling")
4. Writes using `hope-mcp:` still silently succeeded but landed in chatui/

## Mitigation (always do after writing)
```bash
# Immediately verify the file really exists at the expected absolute path:
ls /mnt/.../hope-mcp/tests/new_file.py
# If not there, check the PRIMARY root:
ls /mnt/.../chatui/tests/new_file.py
```

## Recovery pattern
```bash
mv /primary-root/tests/misdirected_file.py /intended-root/tests/
```

Then re-register the intended root with `create_project(path=..., overwrite=true)` using a fresh name, and use THAT prefix going forward.

## Best practice
- After any `mv` of a project root, prefer **absolute paths** over `rootname:` prefix until you've verified the new root is active.
- Writes inside the registered non-primary root work fine when addressed via its ABSOLUTE path (still needs create_project registration for security, but path resolution is unambiguous).
- For critical writes (tests, configs), pair each `write_file` with a `run_command ls` or `find` to confirm location.

