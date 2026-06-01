---
name: hope-mcp-submission-pipeline-design
description: How hope run's 5-step pipeline is reimplemented pure-HTTP in hope-mcp v0.3.0
enabled: true
tags: [hope-mcp, architecture, submission-pipeline]
created: 2026-05-06T10:28:05Z
updated: 2026-05-06T10:28:05Z
---

# hope-mcp submission pipeline — pure-HTTP reimplementation

## The myth we busted (2026-05-06)

The v0.2.0 README claimed `hope run` was impossible to reimplement in
hope-mcp because of a "local packaging pipeline". After reading
`hope.api_template.run_job` + `hope.tools.jobUtils.pack_usercode_and_upload`
carefully: it's a **5-step HTTP pipeline**, nothing exotic.

## The 5 steps (for Python jobs)

1. `POST /hopetemplate/jobs/init` → returns `job_id`, `job_name="<stem>@<uuid>"`.
   (Skippable if the caller already has a job_id from a previous init.)
2. `tar -czf` the workdir (excluding `.hopemeta` + `.hopeignore` entries)
   and compute MD5 of concatenated file contents (alphabetical walk —
   matches `hope.tools.helper.md5_by_walk_dir` byte-for-byte).
3. `POST /hopetemplate/jobs/get_s3_url` with `{job_id, code_md5,
   src_tar, s3_key_name}` → presigned PUT URL.
   `s3_key_name` format: `hope/<job_type>/<mode>/<job_id>/<md5>/<filename>`
   (unless job_type is `spark`/`mapreduce`/`pyspark` in which case just
   `hope/<job_id>/<md5>/<filename>`).
4. `PUT <presigned_url>` with the tarball bytes (single request; no
   multipart). Proxy-bypass is critical — `requests.Session(trust_env=False)`.
5. `POST /hopetemplate/jobs/save_code_version` → returns `code_id`.
6. `POST /hopetemplate/jobs/run_job` → returns `run_id`.

## The Scala/Spark branch we intentionally skip

`pack_usercode_and_upload` for Scala jobs runs `mvn clean package` or
`sbt clean` inline. hope-mcp declines: that's an environment problem,
not a protocol one.

## What we deliberately DON'T do

* `.hopemeta` mutation — the CLI writes `code_id`, `s3_key_name`,
  `run_id` into `.hopemeta` in the caller's workdir. hope-mcp keeps
  the workdir untouched. Backend de-dups by `code_md5` anyway, so
  re-uploads of identical content are cheap.
* `give_second_thought('Continue anyway? [y]/n')` — the CLI prompts
  stdin if tarball >= 1 GiB. hope-mcp just logs a warning and
  continues. MCP tools must never block on stdin.
* `afo.engine.wait_for_job_finished` polling. Return immediately
  with run_id; let the LLM poll `hope_get_status` or `hope_get_run_log`.

## Backend endpoint verification (2026-05-06)

Only 4 endpoints actually return HTTP 500:
* `/get_job_list` — was `hope ls --history`.
* `/get_task_attempts` — was `hope fetch --log`.
* `/update_template_version` — was `hope ls --history` update path.
* `/legacy/login/check` — internal only.

Everything else in `hope.tools.settings.URLs` is alive. The v0.2.0
"drop them all" stance was over-conservative; v0.3.0 wraps 13 new
tools on top of the existing 9.

## Token payload shape (all endpoints)

Every request sends form-encoded body (NOT JSON):
```
service=HopeClient
user=<misid>
token=<from .token file>
hostname=<local hostname>
hope_version=Hope Client 3.6.6
... (endpoint-specific fields)
```

The hope CLI duplicates arg-style keys (`--runid=X` alongside
`run_id=X`); we follow the same convention for compat.

## Token file quirks (shared FUSE mounts)

`.token` is a JSON dict of `{misid: {token, expire_time, [pids]}}`.
On shared mounts (`HOPE_HOME_DIR=/mnt/dolphinfs/.../hope/.hope`)
multiple misids coexist. `hope_api._select_token_entry` picks by
priority: explicit arg → `HOPE_USERNAME` env → current process
user → unique non-expired → latest expire_time. See also the
`hope-mcp-dx-confirm-pid-allowlist` memory for the `pids` field gotcha.

