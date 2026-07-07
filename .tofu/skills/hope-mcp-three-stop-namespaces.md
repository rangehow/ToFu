---
name: hope-mcp-three-stop-namespaces
description: Hope/MLP 3 runId namespaces + route via mlp_run_stop; BOTH stop paths 500 on 12+ day-old hope run_ids (backend GC, unkillable from client)
enabled: true
tags: [hope-mcp, mlp, gotcha, api]
created: 2026-05-07T17:01:48Z
updated: 2026-07-01T08:55:10Z
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

* `hope_stop_job(s_batch)` on an MLP runId → `Job does not exist`.
* `hope_mlp_job_stop` on an `activeAppId` of a Hope-owned run
  (runName `[hope/…]`) → HTTP 500 `ClassCastException` from
  `/mlapi/kub/job/<jobId>/stop`. MLP refuses because the job lives on
  hope-template.

## The fix: `hope_mlp_run_stop`

Prefer this when working from `hope_mlp_run_list`. It:
1. Resolves `activeAppId` from `run_id` (`/mlapi/kub/run/<rid>/info/base`)
   or `app_id` (`/mlapi/kub/job/basic`).
2. Sniffs Hope-ownership via `[hope/…]` runName prefix.
3. Hope jobs: pulls hope `run_id` via cached `/get_yarn_apps`
   (pass `nocache=False` — `nocache=True` STRIPS `run_id`!), then POST
   `/hopetemplate/jobs/stop_job/<hope_run_id>`.
4. MLP-native jobs: POST `/mlapi/kub/job/<jobId>/stop`.

## ★ OLD run_ids are UNKILLABLE from any client (confirmed 2026-07-01)

For hope-owned jobs older than ~12 days, **BOTH** stop paths 500 —
there is NO client-side workaround:

* `/hopetemplate/jobs/stop_job/<hope_run_id>` → HTTP 500, Django
  `Server Error (500)` HTML page (no JSON).
* `/mlapi/kub/job/<activeAppId>/stop` → HTTP 500
  `java.lang.ClassCastException` (NestedServletException).

Evidence: a batch of 14 ml-easy-job runs — the ONE submitted ~1 day
prior (runId 820873682 / hope_run_id 53519325) stopped cleanly; all 13
submitted 12–26 days prior 500'd on BOTH endpoints. Root cause = backend
metadata GC / state inconsistency for old run_ids; `stop_job`
deserialization throws ClassCastException. The upstream `mlp hope stop`
CLI hits the identical error. **Don't loop; escalate to hope-platform**
(force-kill the app + fix stop endpoints to gracefully reject or kill
when metadata is GC'd, instead of 500). The MLP web UI stop path
(`http://mlp.sankuai.com/ml/#/job/<appId>`) is the only remaining
user-facing option and was NOT verified to work.

## Practical caveats

* Stopping Hope jobs needs BOTH logins: `hope_mlp_login` (MLP SSO, now
  native CIBA) + `hope_login` (hope mobile-push).
* Terminal states (`KILLED/SUCCEEDED/FAILED/FINISHED/STOPPED`) short-
  circuit with `verdict=already_terminal`.

## Files (v0.6.2)
* `src/hope_mcp/tools/mlp.py` — `mlp_run_stop()` + dispatch.
* `src/hope_mcp/server.py` — registered `hope_mlp_run_stop`; clarified
  the three low-level stop tools' ID-namespace docs.

