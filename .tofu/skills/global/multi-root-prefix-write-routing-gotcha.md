---
name: multi-root-prefix-write-routing-gotcha
description: Multi-root workspace: write_file with non-primary root prefix silently routes to primary; apply_diff reports file not found
enabled: true
tags: [tools, workspace, multi-root, gotcha]
created: 2026-04-20T06:28:59Z
updated: 2026-04-20T06:28:59Z
---

# Multi-Root Workspace Prefix Routing Bug

In a multi-root workspace (e.g. `chatui` primary + `overleaf-mcp` extra),
the `rootname:path` prefix is **not reliably honored** by all tools:

## Symptoms observed (2026-04)
- `write_file(path="overleaf-mcp:promo/foo.html", ...)` → reports success
  but file actually lands in **primary root** (`chatui/promo/foo.html`),
  not `overleaf-mcp/promo/foo.html`.
- `apply_diff(path="overleaf-mcp:promo/foo.html", ...)` → fails with
  `File not found: promo/foo.html` (same relative path, under primary).
- `list_dir(path="overleaf-mcp:promo")` → lists **primary** root's subdir.
- `run_command(working_dir="overleaf-mcp:")` → runs in **primary** root.
- `read_files` with `rootname:path` sometimes works, sometimes not.

## Working workaround
Use **absolute paths** for every cross-root operation:
- `write_file(path="overleaf-mcp:relative", ...)` still works for CREATION
  if the primary root doesn't also contain that path. But verify with
  `run_command ls <absolute_path>`.
- For edits: use `run_command` with `sed -i` / `python3 -c` against
  the absolute target path. `apply_diff` with rootname prefix is unreliable.
- Always follow up with `ls <absolute_path>` to confirm the file is where
  you think it is. If it landed in the primary root, `mv` it over.

## Reliable pattern
```bash
# Edit via heredoc + abs path — always works
python3 << 'EOF'
p = "/abs/path/to/file"
s = open(p).read()
open(p, 'w').write(s.replace("old", "new"))
EOF
```

## Rendering pipeline note
When rendering HTML→PNG with Playwright, if the HTML sits in the wrong
root, the browser will `ERR_FILE_NOT_FOUND` the missing page. After any
`write_file` to a non-primary root, verify with `ls` and `mv` as needed
before running the renderer.

