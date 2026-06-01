---
name: hope-mcp-three-stop-namespaces
description: Hope/MLP has 3 "runId" namespaces — use hope_mlp_run_stop, not the low-level stops
enabled: true
tags: [hope-mcp, mlp, gotcha, api]
created: 2026-05-07T17:01:48Z
updated: 2026-05-07T17:01:48Z
---

# hope-mcp: three ID namespaces collide visually — always route via `hope_mlp_run_stop`

## The trap

`hope_mlp_run_list` returns three IDs for each entry. They all look
numeric or `psx…`, but they live in three different namespaces:

| Field               | Example          | Backend that owns it              |
|---------------------|------------------|-----------------------------------|
| `runId` (MLP)       | `779125233`      | `/mlapi/kub/run/…`                |
| `activeAppId` / `jobs[].appId` | `psx5caxrlanapk9` | MLP `/mlapi/kub/job/<jobId>/…` AND hope `/hopetemplate/jobs/get_yarn_apps` |
| hope `run_id` (inside `apps_info[0].run_id`) | `48024626` | `/hopetemplate/jobs/stop_job/<run_id>` |

The MLP `runId` and the hope `run_id` are BOTH numeric but have
different values and different owners.

## Why each stop tool fails when fed the wrong ID

* `hope_stop_job(s_batch)` on an MLP runId → `Job does not exist` (hope
  backend has never heard of it).
* `hope_mlp_job_stop` on an `activeAppId` of a Hope-owned run
  (runName `[hope/…]`) → HTTP 500 `ClassCastException` from
  `/mlapi/kub/job/<jobId>/stop`. MLP refuses because the job actually
  lives on hope-template.

## The fix: `hope_mlp_run_stop`

Always prefer this when working from `hope_mlp_run_list`. It:

1. Resolves `activeAppId` from either `run_id` (via
   `/mlapi/kub/run/<rid>/info/base`) or `app_id` (via
   `/mlapi/kub/job/basic`).
2. Sniffs whether the job is Hope-owned via the `[hope/…]` runName
   prefix (neither endpoint exposes a real `clientType` field).
3. For Hope jobs: pulls hope `run_id` via cached
   `/hopetemplate/jobs/get_yarn_apps` (IMPORTANT: pass `nocache=False`
   — the `nocache=True` payload strips `run_id`!), then POSTs
   `/hopetemplate/jobs/stop_job/<hope_run_id>`.
4. For MLP-native jobs: POSTs `/mlapi/kub/job/<jobId>/stop`.

## Practical cavets

* Stopping Hope jobs needs BOTH logins: `hope_mlp_login` (MLP SSO) +
  `hope_login` (hope mobile-push). `hope_mlp_run_stop` will surface a
  clear "login_required" hint if only one is done.
* The hope-template `/stop_job/<rid>` endpoint returns an HTML-500 for
  very old (21+ day) run_ids. The upstream `mlp hope stop` CLI hits
  the exact same error — it's a backend metadata-GC issue, not a
  client-side bug. Don't loop on it.
* Terminal states (`KILLED`, `SUCCEEDED`, `FAILED`, `FINISHED`,
  `STOPPED`) are detected and short-circuited with `verdict=already_terminal`.

## Files touched in v0.6.2

* `src/hope_mcp/tools/mlp.py` — added `mlp_run_stop()` + dispatch logic.
* `src/hope_mcp/server.py` — registered `hope_mlp_run_stop` in TOOLS
  + TOOL_HANDLERS; clarified the three low-level stop tools'
  descriptions to spell out the ID namespace each one expects.

