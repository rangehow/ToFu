---
name: mcp-tool-name-no-server-prefix-stutter
description: MCP tool naming: never repeat the server name in tool names; chatui's make_namespaced_name dedupes a leading {server}_ prefix as a safety net
enabled: true
tags: [mcp, convention, naming]
created: 2026-05-10T07:20:40Z
updated: 2026-05-10T07:20:40Z
---

# MCP tool name convention: don't stutter the server name

## Problem
The MCP wire format is `mcp__{server}__{tool}`. Authoring an MCP server
with tool names that already start with the server name (e.g. server
`hope` registers tools `hope_login`, `hope_get_status`) yields stuttering
LLM-visible names like `mcp__hope__hope_login` — the server name appears
twice. This makes prompts harder to read and wastes tokens.

## Convention
- In any MCP server WE author (hope-mcp, xuecheng-mcp, …), tool names
  **must NOT** start with the server name. Use action-based names:
  `login`, `read_doc`, `submit_job`, `list_experiments`, etc.
- Don't use jargon platform names that LLMs won't recognize (e.g. "MLP",
  internal product codenames). Pick descriptive verbs the model
  already understands. Prefix only when needed to disambiguate
  collisions inside one MCP (e.g. `experiment_login` vs `login` for
  two different auth subsystems on the same server).

## Safety net in chatui
`lib/mcp/types.py::make_namespaced_name(server_name, tool_name)` strips
a leading `{server_name}_` from `tool_name` automatically. So even if a
server still registers stuttering names, the LLM sees the deduped form.

To make this work end-to-end, `lib/mcp/client.py::call_tool()` looks up
the original (pre-dedupe) `tool_name` from `_tool_index[ns]['tool_name']`
before sending the request — `parse_namespaced_name` only sees the
post-dedupe view.

## When debugging tool-not-found errors
- Check whether the MCP server registered `foo` or `server_foo` —
  `_tool_index` keys are post-dedupe `mcp__server__foo`, but the wire
  call uses the original.
- `'__server__' in fn_name` substring matches still work after dedupe
  (used by `lib/mcp/project_names.py` for ingestion).

## History
- 2026-05: initial dedupe added; hope-mcp and xuecheng-mcp servers
  renamed to drop redundant prefixes. hope-mcp's `mlp_*` family
  renamed to action verbs (`list_experiments`, `get_log_file`, …)
  because LLMs don't recognize "MLP" as a meaningful term.

