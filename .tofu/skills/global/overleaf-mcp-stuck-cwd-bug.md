---
name: overleaf-mcp-stuck-cwd-bug
description: Overleaf MCP gets stuck in '[Errno 17] File exists: .' if you delete its cache dir
enabled: true
tags: [overleaf, mcp, bug, workaround]
created: 2026-05-07T02:32:32Z
updated: 2026-05-07T02:32:32Z
---

# Overleaf MCP server stuck CWD bug

## Symptom

After the local cache dir for an Overleaf project (e.g. `/mnt/.../overleaf_cache/<project_id>/`)
is deleted while the MCP server is in chdir'd into it, **all** project-specific tool calls
(`list_files`, `read_file`, `create_file`, `edit_file`, `status_summary`, `sync_project`,
even for OTHER projects) start failing with:

```
Error: [Errno 17] File exists: '.'
```

`list_projects`, `download_source_zip`, `compile_project`, `download_pdf` still work
because they don't depend on the cache dir.

## Root cause

Probably: parallel `upload_file` calls race on the git lock, leaving the cache in a corrupt state.
Then attempting `find <cache>/<id> -delete` while the MCP process has its CWD inside that dir
nukes the dir from under the MCP, but the MCP holds an open dir handle and can't recover.

## Workaround

**Use git directly** instead of the MCP for bulk file operations. The Overleaf project's
git URL with auth token can be read from any healthy cache:

```
cat /mnt/.../overleaf_cache/<healthy_project_id>/.git/config
# extract:  url = https://git:olp_XXXX@git.overleaf.com/<project_id>
```

Then:
```bash
git clone https://git:olp_XXXX@git.overleaf.com/<NEW_project_id> /tmp/work
cd /tmp/work
git config user.email ...; git config user.name ...
# work locally, then
git add -A && git commit -m "..." && git push origin master
```

After the push, `compile_project` and `download_pdf` (which don't go through
the broken cache codepath) work fine.

## Prevention

- Never run more than 1 `upload_file` / `create_file` / `edit_file` in parallel
  (they race on the project's git lock).
- Don't `find <cache_dir> -delete` while the MCP process is alive.
- For batch uploads (>5 files), prefer `git clone` + local work + `git push`.

## Tested 2026-05-07

