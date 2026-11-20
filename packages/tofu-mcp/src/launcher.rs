//! Launcher resolution and install jobs (`lib/mcp/client/_install.py`, the
//! Python oracle): the interpreter-adjacent self-heal (`_resolve_launcher`,
//! `_prepend_interpreter_bin_to_path`), the user-facing install hint, and the
//! non-blocking install-job registry. An install job is a task on the host's
//! runtime whose uv warm is an owned child process group (`vendor.rs`); the
//! call that starts it returns at once and the caller polls the job.
//!
//! Host protocol: `launcher.<op>` methods (see [`dispatch`]); every op reads
//! the binding's launcher facts first (`vendor::LauncherContext::fetch`).

use crate::callback::BindingCalls;
use crate::errors::CallError;
use crate::pyjson::py_repr;
use crate::pyos;
use crate::rmcp_session::{resolve_executable, MCP_CONNECT_TIMEOUT};
use crate::stdio_env::{self, Env};
use crate::vendor::{self, abspath, dirname, is_dir, is_file, Fallback, LauncherContext};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock};
use tokio::task::JoinHandle;

/// `_LAUNCHER_HINTS`.
fn launcher_hint(base: &str) -> Option<&'static str> {
    Some(match base {
        "uvx" => {
            "Install uv (provides uvx): `curl -LsSf https://astral.sh/uv/install.sh | sh` \
             (or install uv in a project .venv). Tofu discovers ~/.local/bin on the next call."
        }
        "npx" => {
            "Install Node.js (provides npx): https://nodejs.org/ (LTS recommended). After install, restart Tofu."
        }
        "pipx" => {
            "Install pipx: `python3 -m pip install --user pipx && pipx ensurepath`. After install, restart Tofu."
        }
        "node" => "Install Node.js: https://nodejs.org/ (LTS recommended).",
        "python3" => "Python 3 is missing from PATH — very unusual. Check your shell PATH.",
        _ => return None,
    })
}

fn interpreter_dir(ctx: &LauncherContext) -> String {
    if ctx.interpreter.is_empty() {
        String::new()
    } else {
        dirname(&abspath(&ctx.interpreter))
    }
}

/// `_resolve_launcher`: a bare launcher next to the running interpreter.
pub fn resolve_launcher(ctx: &LauncherContext, command: &str) -> Option<String> {
    if command.is_empty() || command.contains('/') {
        return None;
    }
    let mut candidates: Vec<String> = Vec::new();
    for base in [interpreter_dir(ctx), ctx.base_prefix.clone()] {
        if base.is_empty() {
            continue;
        }
        let bin_dirs = if matches!(pyos::basename(&base), "bin" | "Scripts") {
            vec![base.clone()]
        } else {
            vec![pyos::join(&base, "bin"), pyos::join(&base, "Scripts")]
        };
        for bin_dir in bin_dirs {
            let candidate = pyos::join(&bin_dir, command);
            if !candidates.contains(&candidate) {
                candidates.push(candidate);
            }
        }
    }
    candidates
        .into_iter()
        .find(|c| is_file(c) && vendor::access_x(c))
}

/// `_prepend_interpreter_bin_to_path`.
pub fn prepend_interpreter_bin_to_path(ctx: &LauncherContext, env: &mut Env) {
    let exe_dir = interpreter_dir(ctx);
    if exe_dir.is_empty() || !is_dir(&exe_dir) {
        return;
    }
    let current = env.get("PATH").cloned().unwrap_or_default();
    let parts: Vec<&str> = if current.is_empty() {
        Vec::new()
    } else {
        current.split(':').collect()
    };
    if parts.first() == Some(&exe_dir.as_str()) {
        return;
    }
    let mut path = vec![exe_dir.as_str()];
    path.extend(parts.into_iter().filter(|p| *p != exe_dir));
    env.insert("PATH".into(), path.join(":"));
}

/// `_launcher_install_hint`: an actionable hint for a missing launcher.
pub fn launcher_install_hint(ctx: &LauncherContext, command: &str) -> String {
    let base = command.rsplit('/').next().unwrap_or(command);
    if let Some((source, _editable)) = vendor::find_vendored_source(ctx, base) {
        let why = vendor::last_error(base);
        let warm = format!(
            "uv run --no-project --with-editable {source} python -c 'import {}'",
            base.replace('-', "_")
        );
        let mut message = format!(
            "{} is a bundled MCP server launched isolated from {} via `uv run --with-editable`, \
             but the env could not be resolved. ",
            py_repr(base),
            py_repr(&source)
        );
        if !why.is_empty() {
            message.push_str(&format!("Reason: {why}. "));
        }
        message.push_str(&format!(
            "Verify the source is intact and the warm succeeds manually:\n    {warm}\n\
             Also check that `uv` itself is on PATH, then click Reinstall."
        ));
        return message;
    }
    let vendor_path = pyos::join(&pyos::join(&ctx.source_root, "vendor"), base);
    if is_file(&pyos::join(&vendor_path, "pyproject.toml")) {
        return format!(
            "This project bundles {} under {}. It launches isolated via `uv run --no-project \
             --with-editable {vendor_path} {base}`; check that `uv` is on PATH and that the source \
             tree is intact, then restart Tofu.",
            py_repr(base),
            py_repr(&vendor_path)
        );
    }
    launcher_hint(base).map(str::to_string).unwrap_or_else(|| {
        format!(
            "Install {} via your package manager, or make sure it is on PATH.",
            py_repr(command)
        )
    })
}

/// `apply_tool_paths(environment, interpreter_first=True, cache_root=tool_cache_root())`.
fn install_environment(ctx: &LauncherContext) -> Result<Env, String> {
    let mut env = stdio_env::inherited_child_environment(&ctx.process_env);
    let cache_root = ctx.tool_cache_root();
    ctx.with_env_context(false, |c| {
        stdio_env::apply_tool_paths(&mut env, c, Some(&cache_root))
    })?;
    if !ctx.interpreter.is_empty() {
        let mut front = Vec::new();
        let bundled = format!("{}/.tofu/node-toolchain/bin", ctx.source_root);
        if is_dir(&bundled) {
            front.push(bundled);
        }
        front.push(interpreter_dir(ctx));
        front.push(pyos::join(&ctx.base_prefix, "bin"));
        let existing = env.get("PATH").cloned().unwrap_or_default();
        let mut path: Vec<String> = Vec::new();
        for entry in front
            .into_iter()
            .chain(existing.split(':').map(str::to_string))
        {
            if !path.contains(&entry) {
                path.push(entry);
            }
        }
        env.insert("PATH".into(), path.join(":"));
    }
    stdio_env::validate_environment(&env)?;
    Ok(env)
}

// ── install jobs ──────────────────────────────────────────────────────

#[derive(Clone)]
struct Job {
    state: &'static str,
    detail: String,
    started: f64,
    ended: f64,
}

impl Job {
    fn snapshot(&self) -> Value {
        json!({"state": self.state, "detail": self.detail, "started": self.started, "ended": self.ended})
    }
}

fn jobs() -> &'static Mutex<BTreeMap<String, Job>> {
    static CELL: OnceLock<Mutex<BTreeMap<String, Job>>> = OnceLock::new();
    CELL.get_or_init(|| Mutex::new(BTreeMap::new()))
}

fn workers() -> &'static Mutex<Vec<JoinHandle<()>>> {
    static CELL: OnceLock<Mutex<Vec<JoinHandle<()>>>> = OnceLock::new();
    CELL.get_or_init(|| Mutex::new(Vec::new()))
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or_default()
}

/// `get_install_job`.
pub fn get_install_job(command: &str) -> Option<Value> {
    jobs()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .get(command)
        .map(Job::snapshot)
}

/// `start_install_job`: start (or re-attach to) the uv warm for `command`; never waits for it.
pub fn start_install_job(ctx: LauncherContext, command: &str) -> Result<Value, String> {
    let environment = install_environment(&ctx)?;
    let warmable = !command.is_empty()
        && !command.contains('/')
        && vendor::vendored_launch_argv(&ctx, command)?.is_some();
    if command.is_empty()
        || command.contains('/')
        || (!warmable && resolve_executable(command, &environment).is_some())
    {
        let job = Job {
            state: "ready",
            detail: String::new(),
            started: now(),
            ended: now(),
        };
        let snapshot = job.snapshot();
        jobs()
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .insert(command.into(), job);
        return Ok(snapshot);
    }
    let started = {
        let mut registry = jobs().lock().unwrap_or_else(|p| p.into_inner());
        if let Some(existing) = registry.get(command).filter(|j| j.state == "installing") {
            return Ok(existing.snapshot());
        }
        let job = Job {
            state: "installing",
            detail: String::new(),
            started: now(),
            ended: 0.0,
        };
        let started = job.started;
        registry.insert(command.into(), job);
        started
    };
    let name = command.to_string();
    let worker = tokio::spawn(async move {
        let (ready, detail) =
            match vendor::prewarm_vendored_launcher(&ctx, &name, vendor::shutdown_token()).await {
                Ok(outcome) => outcome,
                Err(error) => (false, format!("install crashed: {error}")),
            };
        jobs().lock().unwrap_or_else(|p| p.into_inner()).insert(
            name,
            Job {
                state: if ready { "ready" } else { "error" },
                detail,
                started,
                ended: now(),
            },
        );
    });
    let mut running = workers().lock().unwrap_or_else(|p| p.into_inner());
    running.retain(|w| !w.is_finished());
    running.push(worker);
    drop(running);
    Ok(get_install_job(command).unwrap_or(Value::Null))
}

/// Stop running warms (each retires its process group) and wait for their jobs.
pub async fn shutdown() {
    vendor::shutdown_token().cancel();
    let running = std::mem::take(&mut *workers().lock().unwrap_or_else(|p| p.into_inner()));
    for worker in running {
        let _ = worker.await;
    }
}

// ── host protocol ─────────────────────────────────────────────────────

fn param<'a>(params: &'a Value, key: &str) -> Result<&'a str, CallError> {
    params
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| CallError::Invalid(format!("missing string parameter {key}")))
}

fn env_param(params: &Value) -> Env {
    params
        .get("env")
        .and_then(Value::as_object)
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string())))
                .collect()
        })
        .unwrap_or_default()
}

fn strings(params: &Value, key: &str) -> Vec<String> {
    params
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect()
}

/// `launcher.<op>` host methods: each runs the ported oracle function with the binding's facts.
pub async fn dispatch(
    op: &str,
    params: &Value,
    binding: Arc<dyn BindingCalls>,
) -> Result<Value, CallError> {
    let ctx = LauncherContext::fetch(binding, Fallback::from_process()).await;
    let invalid = CallError::Invalid;
    let stop = vendor::shutdown_token();
    Ok(match op {
        "registry" => Value::Object(
            ctx.launchers
                .iter()
                .map(|(name, row)| {
                    let mut entry = json!({"sources": row.sources});
                    if !row.extra_sources.is_empty() {
                        entry["extra_sources"] = json!(row.extra_sources);
                    }
                    (name.clone(), entry)
                })
                .collect(),
        ),
        "bundle_launchers" => {
            let (rows, skipped) = vendor::bundle_launchers(&strings(params, "directories"));
            let rows: serde_json::Map<String, Value> = rows
                .into_iter()
                .map(|(name, row)| {
                    let mut entry = json!({"sources": row.sources});
                    if !row.extra_sources.is_empty() {
                        entry["extra_sources"] = json!(row.extra_sources);
                    }
                    (name, entry)
                })
                .collect();
            json!({"rows": rows, "skipped": skipped})
        }
        "find_vendored_source" => {
            json!(vendor::find_vendored_source(
                &ctx,
                param(params, "command")?
            ))
        }
        "is_vendored_launcher" => {
            json!(vendor::is_vendored_launcher(
                &ctx,
                param(params, "command")?
            ))
        }
        "vendored_launch_argv" => {
            json!(vendor::vendored_launch_argv(&ctx, param(params, "command")?).map_err(invalid)?)
        }
        "exact_pin_exemptions" => json!(vendor::exact_pin_exemptions(&strings(params, "sources"))),
        "snapshot_stale_reason" => json!(vendor::snapshot_stale_reason(
            param(params, "sibling")?,
            param(params, "snapshot")?
        )),
        "check_snapshot_staleness" => {
            json!(vendor::check_snapshot_staleness(&ctx, param(params, "command")?).await)
        }
        "launch_fingerprint" => {
            json!(vendor::launch_fingerprint(&ctx, param(params, "command")?).map_err(invalid)?)
        }
        "needs_cold_budget" => json!(vendor::vendored_needs_cold_budget(
            &ctx,
            param(params, "command")?
        )
        .map_err(invalid)?),
        "note_warm" => {
            vendor::note_warm(&ctx, param(params, "command")?).map_err(invalid)?;
            Value::Null
        }
        "warm_marker_path" => json!(vendor::warm_marker_path(&ctx, param(params, "command")?)),
        "unreachable_reason" => json!(vendor::unreachable_reason(&ctx).await),
        "note_unreachable" => {
            vendor::note_unreachable(&ctx, param(params, "reason")?).await;
            Value::Null
        }
        "unreachable_marker_path" => json!(vendor::unreachable_marker_path(&ctx)),
        "egress_identity" => json!(vendor::egress_identity(&ctx).await),
        "is_index_unreachable" => json!(vendor::is_index_unreachable(param(params, "text")?)),
        "reconcile_npx_cache" => json!(vendor::reconcile_npx_cache(
            param(params, "npm_cache")?,
            param(params, "cutoff")?
        )),
        "npx_rebuild_pending" => json!(vendor::npx_rebuild_pending(param(params, "npm_cache")?)),
        "reconcile_for_connect" => json!(vendor::reconcile_for_connect(&ctx)),
        "launches_into_npm_cache" => json!(vendor::launches_into_npm_cache(
            params.get("config").unwrap_or(&Value::Null)
        )),
        "ensure_writable_caches" => {
            let mut env = env_param(params);
            ctx.ensure_writable_caches(&mut env);
            json!(env)
        }
        "propagate_proxy_env" => {
            let mut env = env_param(params);
            vendor::propagate_proxy_env(&ctx, &mut env).await;
            json!(env)
        }
        "tool_cache_defaults" => {
            let root = ctx.tool_cache_root();
            Value::Object(
                stdio_env::tool_cache_defaults(&root)
                    .into_iter()
                    .map(|(k, v)| (k, json!(v)))
                    .collect(),
            )
        }
        "prewarm" => {
            let (ready, detail) =
                vendor::prewarm_vendored_launcher(&ctx, param(params, "command")?, stop)
                    .await
                    .map_err(invalid)?;
            json!([ready, detail])
        }
        "prewarm_all" => Value::Object(vendor::prewarm_all_vendored(&ctx, stop).await),
        "last_error" => json!(vendor::last_error(param(params, "command")?)),
        "start_install_job" => {
            start_install_job(ctx, param(params, "command")?).map_err(invalid)?
        }
        "get_install_job" => get_install_job(param(params, "command")?).unwrap_or(Value::Null),
        "resolve_launcher" => json!(resolve_launcher(&ctx, param(params, "command")?)),
        "prepend_interpreter_bin" => {
            let mut env = env_param(params);
            prepend_interpreter_bin_to_path(&ctx, &mut env);
            json!(env)
        }
        "install_hint" => json!(launcher_install_hint(&ctx, param(params, "command")?)),
        "connect_plan" => {
            let config = params.get("config").cloned().unwrap_or(json!({}));
            let command = config
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let args: Vec<String> = config
                .get("args")
                .and_then(Value::as_array)
                .map(|a| a.iter().map(pyos::py_str).collect())
                .unwrap_or_default();
            let plan = vendor::plan_stdio_launch(&ctx, &config, &command, &args)
                .await
                .map_err(invalid)?;
            let budget = if plan.cold_install {
                vendor::cold_install_timeout(&ctx.process_env)
            } else {
                MCP_CONNECT_TIMEOUT
            };
            json!({"command": plan.command, "args": plan.args, "cold_install": plan.cold_install,
                   "handshake_budget": budget.as_secs(), "holds_npm_lock": plan.npm_lock.is_some()})
        }
        _ => {
            return Err(CallError::Invalid(format!(
                "unknown host method launcher.{op}"
            )))
        }
    })
}
