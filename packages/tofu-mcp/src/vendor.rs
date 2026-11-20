//! Vendored MCP launchers (`lib/mcp/client/_vendor.py` and
//! `lib/mcp/vendored.py`, the Python oracles): source resolution, the isolated
//! `uv run --no-project --with-editable <src>` launch argv with its exact-pin
//! cutoff exemptions, snapshot staleness, the warm/unreachable markers and the
//! egress-scoped negative cache, the uv pre-warm as an owned process group,
//! npx cache reconciliation against the supply cutoff, the cross-process npm
//! cache lock, and the stdio connect pre-flight that chooses the handshake
//! budget.
//!
//! The registry rows, `_repo_root()`, the interpreter and the proxy pool stay
//! with the binding (`callback.rs` method `launcher_context`, plus
//! `global_proxy_url` / `reachable_global_proxy_url`): the Python owner
//! hot-reloads `vendored.py` and merges bundle rows, so Rust asks per use and
//! keeps the last good answer when the binding cannot answer.

use crate::callback::BindingCalls;
use crate::pyjson::{self, py_repr};
use crate::pyos;
use crate::stdio_env::{self, Env, EnvContext};
use crate::transport::NoVault;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tokio::io::AsyncReadExt;
use tokio_util::sync::CancellationToken;

/// `_NPX_CUTOFF_MARKER`.
pub const NPX_CUTOFF_MARKER: &str = ".tofu-supply-cutoff";
/// `_PREWARM_UNREACHABLE_MARKER`.
pub const PREWARM_UNREACHABLE_MARKER: &str = ".prewarm-unreachable.json";
/// `_PREWARM_WARM_MARKER`.
pub const PREWARM_WARM_MARKER: &str = ".prewarm-warm.json";
/// `_WARM_SECONDS`: the existing bound of one uv warm.
pub const WARM_SECONDS: u64 = 300;
/// `_prewarm_negative_cache_hours`'s default (`TOFU_MCP_PREWARM_NEGATIVE_CACHE_HOURS`).
const PREWARM_NEGATIVE_CACHE_HOURS_DEFAULT: f64 = 6.0;
/// `lib/project_mod/process_group.retire_process_group` `grace_seconds` default.
const RETIRE_GRACE_SECONDS: Duration = Duration::from_secs(3);
/// `_run_vendor_script`'s `subprocess.run(..., timeout=120)`.
const RUN_VENDOR_SCRIPT_TIMEOUT: Duration = Duration::from_secs(120);
/// `lib/mcp/types.py` `MCP_COLD_INSTALL_TIMEOUT` default (`TOFU_MCP_COLD_INSTALL_TIMEOUT`).
pub const MCP_COLD_INSTALL_TIMEOUT_DEFAULT: u64 = 300;
/// `lib/mcp/vendored.py` `MCP_LAUNCHERS_CONTRACT`.
pub const MCP_LAUNCHERS_CONTRACT: &str = "tofu.mcp-launchers/v1";
/// `_NPM_CACHE_LAUNCHERS`.
const NPM_CACHE_LAUNCHERS: [&str; 2] = ["npx", "npm"];
/// `_PROXY_ENV_KEYS`.
const PROXY_ENV_KEYS: [&str; 8] = [
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
];
const WEB_PROXY_KEYS: [&str; 4] = ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"];
/// `_VENDOR_EXCLUDE_DIRS` (mirrors `scripts/vendor_mcp.sh` EXCLUDES).
const VENDOR_EXCLUDE_DIRS: [&str; 12] = [
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".tofu",
    ".tofu_trash",
    ".chatui",
    ".oa-skills-ref",
    ".venv",
    "venv",
];

fn tofu_proxy_alias(key: &str) -> Option<&'static str> {
    match key {
        "HTTPS_PROXY" => Some("TOFU_HTTPS_PROXY"),
        "HTTP_PROXY" => Some("TOFU_HTTP_PROXY"),
        "ALL_PROXY" => Some("TOFU_ALL_PROXY"),
        "NO_PROXY" => Some("TOFU_NO_PROXY"),
        _ => None,
    }
}

/// One `VENDORED_LAUNCHERS` row.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SourceLauncher {
    pub sources: Vec<String>,
    pub extra_sources: Vec<String>,
}

/// The facts the oracle reads from `lib.mcp.client`'s globals, per use.
#[derive(Clone)]
pub struct LauncherContext {
    pub process_env: Env,
    pub runtime_base: String,
    /// `lib.mcp.client._repo_root()`.
    pub repo_root: String,
    /// The checkout `lib/mcp` itself lives in (`lib.mcp.vendored.repo_root()`).
    pub source_root: String,
    /// `sys.executable`.
    pub interpreter: String,
    /// `sys.base_prefix`.
    pub base_prefix: String,
    pub python_minor: String,
    /// `lib.mcp.client._VENDORED_LAUNCHERS`, in registry order.
    pub launchers: Vec<(String, SourceLauncher)>,
    /// `lib.mcp.vendored.VENDORED_LAUNCHERS[...]['extra_sources']`.
    pub extra_sources: BTreeMap<String, Vec<String>>,
    pub binding: Arc<dyn BindingCalls>,
}

/// What the launching process knows without the binding.
pub struct Fallback {
    pub process_env: Env,
    pub runtime_base: String,
    pub repo_root: String,
    pub python_minor: String,
}

impl Fallback {
    pub fn from_process() -> Fallback {
        Fallback {
            process_env: process_env(),
            runtime_base: String::new(),
            repo_root: String::new(),
            python_minor: String::new(),
        }
    }
}

pub fn process_env() -> Env {
    std::env::vars_os()
        .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?)))
        .collect()
}

fn last_good() -> &'static Mutex<Option<Value>> {
    static CELL: OnceLock<Mutex<Option<Value>>> = OnceLock::new();
    CELL.get_or_init(|| Mutex::new(None))
}

fn text(value: &Value, key: &str, default: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or(default)
        .to_string()
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect()
}

impl LauncherContext {
    /// Ask the binding for the launcher facts; keep the last good answer.
    pub async fn fetch(binding: Arc<dyn BindingCalls>, fallback: Fallback) -> LauncherContext {
        let answer = match binding.ask("launcher_context", json!({})).await {
            Ok(value) if value.is_object() => {
                *last_good().lock().unwrap_or_else(|p| p.into_inner()) = Some(value.clone());
                Some(value)
            }
            _ => last_good()
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .clone(),
        };
        let answer = answer.unwrap_or_else(|| json!({}));
        let launchers = answer
            .get("launchers")
            .and_then(Value::as_object)
            .map(|rows| {
                rows.iter()
                    .map(|(name, row)| {
                        (
                            name.clone(),
                            SourceLauncher {
                                sources: strings(row.get("sources")),
                                extra_sources: strings(row.get("extra_sources")),
                            },
                        )
                    })
                    .collect()
            })
            .unwrap_or_default();
        let extra_sources = answer
            .get("extra_sources")
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), strings(Some(v))))
                    .collect()
            })
            .unwrap_or_default();
        let repo_root = text(&answer, "repo_root", &fallback.repo_root);
        LauncherContext {
            process_env: fallback.process_env,
            runtime_base: text(&answer, "runtime_base", &fallback.runtime_base),
            source_root: text(&answer, "source_root", &repo_root),
            repo_root,
            interpreter: text(&answer, "interpreter", ""),
            base_prefix: text(&answer, "base_prefix", ""),
            python_minor: text(&answer, "python_minor", &fallback.python_minor),
            launchers,
            extra_sources,
            binding,
        }
    }

    fn row(&self, command: &str) -> Option<&SourceLauncher> {
        self.launchers
            .iter()
            .find(|(name, _)| name == command)
            .map(|(_, row)| row)
    }

    /// Run `f` with the stdio environment seams this context implies.
    pub fn with_env_context<R>(
        &self,
        prepare_directories: bool,
        f: impl FnOnce(&EnvContext) -> R,
    ) -> R {
        let policy = |_owner: u64| String::new();
        let ctx = EnvContext {
            process_env: self.process_env.clone(),
            runtime_base: self.runtime_base.clone(),
            repo_root: self.source_root.clone(),
            python_minor: self.python_minor.clone(),
            policy_path: &policy,
            vault: &NoVault,
            pool_proxy: None,
            prepare_directories,
        };
        f(&ctx)
    }

    /// `tool_cache_root`.
    pub fn tool_cache_root(&self) -> String {
        self.with_env_context(false, stdio_env::tool_cache_root)
    }

    /// `_ensure_writable_caches`.
    pub fn ensure_writable_caches(&self, env: &mut Env) {
        let _ = self.with_env_context(true, |ctx| stdio_env::ensure_writable_caches(env, ctx));
    }

    async fn ask_text(&self, method: &str) -> String {
        match self.binding.ask(method, json!({})).await {
            Ok(Value::String(url)) => url,
            _ => String::new(),
        }
    }
}

// ── small CPython os.path helpers ─────────────────────────────────────

pub(crate) fn abspath(path: &str) -> String {
    if path.starts_with('/') {
        pyos::normpath(path)
    } else {
        let cwd = std::env::current_dir()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        pyos::normpath(&pyos::join(&cwd, path))
    }
}

pub(crate) fn dirname(path: &str) -> String {
    match path.rfind('/') {
        None => String::new(),
        Some(i) => {
            let head = &path[..=i];
            if head.chars().all(|c| c == '/') {
                head.to_string()
            } else {
                head.trim_end_matches('/').to_string()
            }
        }
    }
}

pub(crate) fn is_file(path: &str) -> bool {
    Path::new(path).is_file()
}

pub(crate) fn is_dir(path: &str) -> bool {
    Path::new(path).is_dir()
}

/// `os.access(path, os.X_OK)`.
pub(crate) fn access_x(path: &str) -> bool {
    let Ok(c) = std::ffi::CString::new(path) else {
        return false;
    };
    // SAFETY: `c` is a valid NUL-terminated path for the duration of the call.
    unsafe { libc::access(c.as_ptr(), libc::X_OK) == 0 }
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or_default()
}

/// `json.dump(obj, f)`: insertion order, `', '`/`': '`, ASCII-escaped.
fn json_dump(value: &Value) -> String {
    pyjson::dumps(
        value,
        pyjson::Style {
            spaced: true,
            sorted: false,
            ascii: true,
        },
    )
}

fn write_text(path: &str, body: &str) -> std::io::Result<()> {
    std::fs::create_dir_all(dirname(path))?;
    std::fs::write(path, body)
}

/// `str(list_of_str)`.
fn list_repr(items: &[String]) -> String {
    let inner: Vec<String> = items.iter().map(|s| py_repr(s)).collect();
    format!("[{}]", inner.join(", "))
}

// ── state the oracle keeps on `lib.mcp.client` ────────────────────────

fn install_last_error() -> &'static Mutex<BTreeMap<String, String>> {
    static CELL: OnceLock<Mutex<BTreeMap<String, String>>> = OnceLock::new();
    CELL.get_or_init(|| Mutex::new(BTreeMap::new()))
}

/// `_install_last_error.get(command, '')`.
pub fn last_error(command: &str) -> String {
    install_last_error()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .get(command)
        .cloned()
        .unwrap_or_default()
}

fn set_last_error(command: &str, message: Option<String>) {
    let mut errors = install_last_error()
        .lock()
        .unwrap_or_else(|p| p.into_inner());
    match message {
        Some(message) => errors.insert(command.into(), message),
        None => errors.remove(command),
    };
}

fn snapshot_reported() -> &'static Mutex<BTreeMap<String, String>> {
    static CELL: OnceLock<Mutex<BTreeMap<String, String>>> = OnceLock::new();
    CELL.get_or_init(|| Mutex::new(BTreeMap::new()))
}

// ── registry and launch argv ──────────────────────────────────────────

/// `_bundle_launchers`: rows a bundle declares in `mcp_launchers/*.json`.
/// Returns the rows and one warning per skipped file.
pub fn bundle_launchers(directories: &[String]) -> (Vec<(String, SourceLauncher)>, Vec<String>) {
    let mut rows: Vec<(String, SourceLauncher)> = Vec::new();
    let mut skipped = Vec::new();
    for directory in directories {
        let mut files: Vec<String> = std::fs::read_dir(directory)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path().to_string_lossy().into_owned())
            .filter(|p| p.ends_with(".json"))
            .collect();
        files.sort();
        for path in files {
            let mut parsed: Vec<(String, SourceLauncher)> = Vec::new();
            let outcome = (|| -> Result<(), String> {
                let body = std::fs::read(&path).map_err(|e| e.to_string())?;
                let body = String::from_utf8(body).map_err(|e| e.to_string())?;
                let document: Value = serde_json::from_str(&body).map_err(|e| e.to_string())?;
                let document = document.as_object().ok_or("not an object")?;
                if document.get("contract").and_then(Value::as_str) != Some(MCP_LAUNCHERS_CONTRACT)
                {
                    return Err(format!("not a {MCP_LAUNCHERS_CONTRACT} document"));
                }
                let launchers = document
                    .get("launchers")
                    .ok_or("'launchers'")?
                    .as_object()
                    .ok_or("launchers is not a mapping")?;
                for (name, row) in launchers {
                    let row = row.as_object().ok_or("row is not a mapping")?;
                    let sources = row.get("sources").and_then(Value::as_array);
                    let valid = |items: &Vec<Value>| {
                        items
                            .iter()
                            .all(|s| s.as_str().is_some_and(|s| !s.is_empty()))
                    };
                    let Some(sources) = sources.filter(|s| !s.is_empty() && valid(s)) else {
                        return Err(format!("{name}: sources must be non-empty strings"));
                    };
                    let extra = match row.get("extra_sources") {
                        None => Vec::new(),
                        Some(Value::Array(items)) if valid(items) => items.clone(),
                        Some(_) => return Err(format!("{name}: extra_sources must be strings")),
                    };
                    let entry = SourceLauncher {
                        sources: strings(Some(&Value::Array(sources.clone()))),
                        extra_sources: strings(Some(&Value::Array(extra))),
                    };
                    parsed.retain(|(n, _)| n != name);
                    parsed.push((name.clone(), entry));
                }
                Ok(())
            })();
            // The oracle assigns rows while it validates, so the rows before a bad one stay.
            for (name, entry) in parsed {
                match rows.iter_mut().find(|(n, _)| *n == name) {
                    Some(slot) => slot.1 = entry,
                    None => rows.push((name, entry)),
                }
            }
            if let Err(error) = outcome {
                skipped.push(format!(
                    "[MCP:Vendored] skipping launcher file {path}: {error}"
                ));
            }
        }
    }
    (rows, skipped)
}

/// `_find_vendored_source`: `(src_dir, editable)` of the first candidate with a `pyproject.toml`.
pub fn find_vendored_source(ctx: &LauncherContext, command: &str) -> Option<(String, bool)> {
    let spec = ctx.row(command)?;
    let root = &ctx.repo_root;
    let tools_dir = format!("{}/", pyos::join(root, "tools"));
    for rel in &spec.sources {
        let candidate = if rel.starts_with('/') {
            rel.clone()
        } else {
            abspath(&pyos::join(root, rel))
        };
        if is_file(&pyos::join(&candidate, "pyproject.toml")) {
            let snapshot = format!("{candidate}/").starts_with(&tools_dir);
            return Some((candidate, !snapshot));
        }
    }
    None
}

/// `is_vendored_launcher`.
pub fn is_vendored_launcher(ctx: &LauncherContext, command: &str) -> bool {
    !command.is_empty() && !command.contains('/') && find_vendored_source(ctx, command).is_some()
}

fn exact_pin() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*===?\s*[^\s,*]+\s*$")
            .expect("static pattern")
    })
}

/// `_exact_pin_exemptions`: normalized names of the exact `==` pins of the sources' manifests.
pub fn exact_pin_exemptions(sources: &[String]) -> Vec<String> {
    let separators = Regex::new(r"[-_.]+").expect("static pattern");
    let mut names = BTreeSet::new();
    for source in sources {
        let path = pyos::join(source, "pyproject.toml");
        let Ok(body) = std::fs::read(&path) else {
            continue;
        };
        let Ok(body) = String::from_utf8(body) else {
            continue;
        };
        let Ok(document) = body.parse::<toml::Table>() else {
            continue;
        };
        let dependencies = document
            .get("project")
            .and_then(toml::Value::as_table)
            .and_then(|p| p.get("dependencies"))
            .and_then(toml::Value::as_array)
            .cloned()
            .unwrap_or_default();
        for requirement in dependencies {
            let Some(requirement) = requirement.as_str() else {
                continue;
            };
            let head = requirement.split(';').next().unwrap_or_default();
            if let Some(found) = exact_pin().captures(head) {
                names.insert(separators.replace_all(&found[1], "-").to_lowercase());
            }
        }
    }
    names.into_iter().collect()
}

/// `vendored_launch_argv`: the isolated launch argv of a registered vendored command.
pub fn vendored_launch_argv(
    ctx: &LauncherContext,
    command: &str,
) -> Result<Option<Vec<String>>, String> {
    if command.is_empty() || command.contains('/') {
        return Ok(None);
    }
    let Some((source, _editable)) = find_vendored_source(ctx, command) else {
        return Ok(None);
    };
    let mut sources = vec![source];
    for relative in ctx.extra_sources.get(command).into_iter().flatten() {
        let extra = pyos::join(&ctx.repo_root, relative);
        if !is_file(&pyos::join(&extra, "pyproject.toml")) {
            return Err(format!("MCP shared dependency source missing: {relative}"));
        }
        sources.push(extra);
    }
    let mut argv: Vec<String> = vec!["uv".into(), "run".into(), "--no-project".into()];
    for package in exact_pin_exemptions(&sources) {
        argv.push("--exclude-newer-package".into());
        argv.push(format!("{package}=false"));
    }
    for source in sources {
        argv.push("--with-editable".into());
        argv.push(source);
    }
    argv.push(command.into());
    Ok(Some(argv))
}

// ── snapshot staleness ────────────────────────────────────────────────

fn vendor_excluded_dir(name: &str) -> bool {
    VENDOR_EXCLUDE_DIRS.contains(&name) || name.ends_with(".egg-info")
}

fn vendor_excluded_file(name: &str) -> bool {
    name.ends_with(".pyc")
        || name.ends_with(".egg-link")
        || name == "JOURNAL.md"
        || name == "citadel (1).zip"
}

/// `int(st.st_mtime)` (truncation toward zero).
fn int_mtime(metadata: &std::fs::Metadata) -> i64 {
    use std::os::unix::fs::MetadataExt;
    let seconds = metadata.mtime();
    if seconds < 0 && metadata.mtime_nsec() > 0 {
        seconds + 1
    } else {
        seconds
    }
}

/// `_vendor_tree_signature`: relpath → (size, int(mtime)), `os.walk` semantics.
pub fn vendor_tree_signature(base: &str) -> BTreeMap<String, (u64, i64)> {
    fn walk(dir: &Path, base: &Path, signature: &mut BTreeMap<String, (u64, i64)>) {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        let mut subdirs = Vec::new();
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let path = entry.path();
            if std::fs::metadata(&path).is_ok_and(|m| m.is_dir()) {
                let link =
                    std::fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink());
                if !vendor_excluded_dir(&name) && !link {
                    subdirs.push(path);
                }
                continue;
            }
            if vendor_excluded_file(&name) {
                continue;
            }
            if let Ok(metadata) = std::fs::metadata(&path) {
                let relative = path
                    .strip_prefix(base)
                    .map(|p| p.to_string_lossy().into_owned())
                    .unwrap_or_default();
                signature.insert(relative, (metadata.len(), int_mtime(&metadata)));
            }
        }
        for subdir in subdirs {
            walk(&subdir, base, signature);
        }
    }
    let mut signature = BTreeMap::new();
    walk(Path::new(base), Path::new(base), &mut signature);
    signature
}

/// `_snapshot_stale_reason`: a concise drift description, or `''` when fresh.
pub fn snapshot_stale_reason(sibling: &str, snapshot: &str) -> String {
    if !is_dir(snapshot) {
        return "snapshot missing".into();
    }
    let sib = vendor_tree_signature(sibling);
    let snap = vendor_tree_signature(snapshot);
    if sib == snap {
        return String::new();
    }
    let missing: Vec<&String> = sib.keys().filter(|k| !snap.contains_key(*k)).collect();
    let extra: Vec<&String> = snap.keys().filter(|k| !sib.contains_key(*k)).collect();
    let changed: Vec<&String> = sib
        .keys()
        .filter(|k| snap.get(*k).is_some_and(|v| Some(v) != sib.get(*k)))
        .collect();
    let example = |items: &[&String]| {
        items
            .iter()
            .take(3)
            .map(|s| s.as_str())
            .collect::<Vec<_>>()
            .join(", ")
    };
    let mut parts = Vec::new();
    if !missing.is_empty() {
        parts.push(format!(
            "{} new/missing (e.g. {})",
            missing.len(),
            example(&missing)
        ));
    }
    if !changed.is_empty() {
        parts.push(format!(
            "{} changed (e.g. {})",
            changed.len(),
            example(&changed)
        ));
    }
    if !extra.is_empty() {
        parts.push(format!(
            "{} stale-extra (e.g. {})",
            extra.len(),
            example(&extra)
        ));
    }
    if parts.is_empty() {
        "content differs".into()
    } else {
        parts.join("; ")
    }
}

fn auto_vendor_enabled(ctx: &LauncherContext) -> bool {
    let value = ctx
        .process_env
        .get("TOFU_MCP_AUTO_VENDOR")
        .map(|v| pyjson::py_strip(v).to_lowercase())
        .unwrap_or_default();
    matches!(value.as_str(), "1" | "true" | "yes" | "on")
}

/// `_run_vendor_script`: `bash scripts/vendor_mcp.sh <command>` for one server.
async fn run_vendor_script(ctx: &LauncherContext, command: &str, root: &str) -> bool {
    let script = pyos::join(&pyos::join(root, "scripts"), "vendor_mcp.sh");
    if !is_file(&script) {
        return false;
    }
    let mut env = stdio_env::inherited_child_environment(&ctx.process_env);
    env.insert("TOFU_PYTHON".into(), ctx.interpreter.clone());
    propagate_proxy_env(ctx, &mut env).await;
    ctx.ensure_writable_caches(&mut env);
    let mut cmd = tokio::process::Command::new("bash");
    cmd.arg(&script)
        .arg(command)
        .current_dir(root)
        .env_clear()
        .envs(&env)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    match tokio::time::timeout(RUN_VENDOR_SCRIPT_TIMEOUT, cmd.output()).await {
        Ok(Ok(output)) => output.status.success(),
        _ => false,
    }
}

/// `_check_snapshot_staleness`: returns the warning the oracle logs, when it logs one.
pub async fn check_snapshot_staleness(ctx: &LauncherContext, command: &str) -> Option<String> {
    let spec = ctx.row(command)?;
    let sibling_rel = spec.sources.iter().find(|s| s.starts_with(".."))?;
    let snapshot_rel = spec.sources.iter().find(|s| s.starts_with("tools/"))?;
    let root = ctx.repo_root.clone();
    let sibling = abspath(&pyos::join(&root, sibling_rel));
    let snapshot = abspath(&pyos::join(&root, snapshot_rel));
    if !is_file(&pyos::join(&sibling, "pyproject.toml")) {
        return None;
    }
    let reason = snapshot_stale_reason(&sibling, &snapshot);
    {
        let mut reported = snapshot_reported()
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        if reported.get(command) == Some(&reason) {
            return None;
        }
        reported.insert(command.into(), reason.clone());
    }
    if reason.is_empty() {
        return None;
    }
    let warning = format!(
        "[MCP] vendored snapshot tools/{command} is STALE vs sibling {sibling}: {reason}. \
         Run `make vendor-mcp {command}` before committing/deploying."
    );
    if auto_vendor_enabled(ctx) && run_vendor_script(ctx, command, &root).await {
        let fresh = snapshot_stale_reason(&sibling, &snapshot);
        snapshot_reported()
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .insert(command.into(), fresh);
    }
    Some(warning)
}

// ── proxy propagation and egress identity ─────────────────────────────

/// `_propagate_proxy_env` with `_propagate_pool_proxy_env` (the binding's reachable pool entry).
pub async fn propagate_proxy_env(ctx: &LauncherContext, env: &mut Env) {
    ctx.with_env_context(false, |c| stdio_env::propagate_proxy_env(env, c));
    if WEB_PROXY_KEYS
        .iter()
        .any(|k| env.get(*k).is_some_and(|v| !v.is_empty()))
    {
        return;
    }
    let url = ctx.ask_text("reachable_global_proxy_url").await;
    if url.is_empty() {
        return;
    }
    for key in WEB_PROXY_KEYS {
        env.entry(key.into()).or_insert(url.clone());
    }
}

/// `urlsplit(url)` → `f'{scheme}://{hostname}:{port}:{username}'`, or `Err` where CPython raises.
fn split_identity(url: &str) -> Result<String, ()> {
    let mut rest = url;
    let mut scheme = String::new();
    if let Some(i) = url.find(':') {
        let head = &url[..i];
        let first = head.chars().next();
        if i > 0
            && first.is_some_and(|c| c.is_ascii_alphabetic())
            && head
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
        {
            scheme = head.to_lowercase();
            rest = &url[i + 1..];
        }
    }
    let netloc = match rest.strip_prefix("//") {
        Some(after) => {
            let end = after.find(['/', '?', '#']).unwrap_or(after.len());
            &after[..end]
        }
        None => "",
    };
    if netloc.contains('[') != netloc.contains(']') {
        return Err(());
    }
    let (userinfo, hostinfo) = match netloc.rfind('@') {
        Some(i) => (Some(&netloc[..i]), &netloc[i + 1..]),
        None => (None, netloc),
    };
    let username = userinfo
        .map(|u| u.split(':').next().unwrap_or_default())
        .unwrap_or_default();
    let (hostname, port) = match hostinfo.split_once('[') {
        Some((_, bracketed)) => {
            let (host, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            (host, after.split_once(':').map(|(_, p)| p).unwrap_or(""))
        }
        None => match hostinfo.split_once(':') {
            Some((host, port)) => (host, port),
            None => (hostinfo, ""),
        },
    };
    let hostname = match hostname.split_once('%') {
        Some((host, zone)) => format!("{}%{zone}", host.to_lowercase()),
        None => hostname.to_lowercase(),
    };
    let port = if port.is_empty() {
        String::new()
    } else {
        if !port.chars().all(|c| c.is_ascii_digit()) {
            return Err(());
        }
        let significant = port.trim_start_matches('0');
        if significant.len() > 5 {
            return Err(());
        }
        let value: u64 = significant.parse().unwrap_or(0);
        if value > 65535 {
            return Err(());
        }
        if value == 0 {
            String::new()
        } else {
            value.to_string()
        }
    };
    Ok(format!("{scheme}://{hostname}:{port}:{username}"))
}

/// `_egress_identity`: scheme, host, port and user of the installer's egress, never the credential.
pub async fn egress_identity(ctx: &LauncherContext) -> String {
    let mut url = ctx.ask_text("global_proxy_url").await;
    if url.is_empty() {
        for key in PROXY_ENV_KEYS {
            let value = ctx
                .process_env
                .get(key)
                .filter(|v| !v.is_empty())
                .or_else(|| {
                    tofu_proxy_alias(key)
                        .and_then(|a| ctx.process_env.get(a))
                        .filter(|v| !v.is_empty())
                });
            if let Some(value) = value {
                url = value.clone();
                break;
            }
        }
    }
    if url.is_empty() {
        return "direct".into();
    }
    split_identity(&url).unwrap_or_else(|_| "unparsed".into())
}

// ── negative cache and warm markers ───────────────────────────────────

/// `float(text)` for the strings CPython accepts here.
fn py_float(text: &str) -> Option<f64> {
    let text = pyjson::py_strip(text);
    let plain: String = text.chars().filter(|c| *c != '_').collect();
    if plain.len() != text.len()
        && (text.starts_with('_') || text.ends_with('_') || text.contains("__"))
    {
        return None;
    }
    plain.parse::<f64>().ok()
}

fn negative_cache_hours(ctx: &LauncherContext) -> f64 {
    let raw = ctx
        .process_env
        .get("TOFU_MCP_PREWARM_NEGATIVE_CACHE_HOURS")
        .cloned()
        .unwrap_or_else(|| "6".into());
    match py_float(&raw) {
        Some(hours) => f64::max(0.0, hours),
        None => PREWARM_NEGATIVE_CACHE_HOURS_DEFAULT,
    }
}

/// `_prewarm_unreachable_marker_path`.
pub fn unreachable_marker_path(ctx: &LauncherContext) -> String {
    pyos::join(&ctx.tool_cache_root(), PREWARM_UNREACHABLE_MARKER)
}

fn value_float(value: Option<&Value>) -> Option<f64> {
    match value {
        v if !pyos::truthy(v) => Some(0.0),
        Some(Value::Bool(b)) => Some(if *b { 1.0 } else { 0.0 }),
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::String(s)) => py_float(s),
        _ => None,
    }
}

fn value_text(value: Option<&Value>) -> String {
    match value {
        Some(v) if pyos::truthy(Some(v)) => pyos::py_str(v),
        _ => String::new(),
    }
}

/// `_prewarm_unreachable_reason`: the cached unreachable reason, or `''` when stale.
pub async fn unreachable_reason(ctx: &LauncherContext) -> String {
    let hours = negative_cache_hours(ctx);
    if hours <= 0.0 {
        return String::new();
    }
    let Ok(body) = std::fs::read_to_string(unreachable_marker_path(ctx)) else {
        return String::new();
    };
    let Ok(Value::Object(data)) = serde_json::from_str::<Value>(&body) else {
        return String::new();
    };
    let Some(ts) = value_float(data.get("ts")) else {
        return String::new();
    };
    let reason = value_text(data.get("reason"));
    if now() - ts >= hours * 3600.0 {
        return String::new();
    }
    let recorded = value_text(data.get("egress"));
    if recorded != egress_identity(ctx).await {
        return String::new();
    }
    if reason.is_empty() {
        "package index unreachable".into()
    } else {
        reason
    }
}

/// `_prewarm_note_unreachable`.
pub async fn note_unreachable(ctx: &LauncherContext, reason: &str) {
    let mut marker = Map::new();
    marker.insert("ts".into(), json!(now()));
    marker.insert("reason".into(), json!(pyjson::py_prefix(reason, 300)));
    marker.insert("egress".into(), json!(egress_identity(ctx).await));
    let _ = write_text(
        &unreachable_marker_path(ctx),
        &json_dump(&Value::Object(marker)),
    );
}

/// `_prewarm_warm_marker_path`.
pub fn warm_marker_path(ctx: &LauncherContext, command: &str) -> String {
    pyos::join(
        &ctx.tool_cache_root(),
        &format!("{command}{PREWARM_WARM_MARKER}"),
    )
}

/// `_launch_fingerprint`: sha256 of `json.dumps(argv)` and every editable source manifest.
pub fn launch_fingerprint(ctx: &LauncherContext, command: &str) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let Some(argv) = vendored_launch_argv(ctx, command)? else {
        return Ok(String::new());
    };
    let mut digest = Sha256::new();
    digest.update(json_dump(&json!(argv)).as_bytes());
    for (at, arg) in argv.iter().enumerate() {
        if arg == "--with-editable" && at + 1 < argv.len() {
            match std::fs::read(pyos::join(&argv[at + 1], "pyproject.toml")) {
                Ok(bytes) => digest.update(&bytes),
                Err(_) => digest.update(b"\0missing"),
            }
        }
    }
    Ok(digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// `vendored_needs_cold_budget`: no warm recorded for this exact launch.
pub fn vendored_needs_cold_budget(ctx: &LauncherContext, command: &str) -> Result<bool, String> {
    if find_vendored_source(ctx, command).is_none() {
        return Ok(false);
    }
    let recorded = std::fs::read_to_string(warm_marker_path(ctx, command))
        .ok()
        .and_then(|body| serde_json::from_str::<Value>(&body).ok())
        .and_then(|data| data.as_object().map(|d| d.get("launch").cloned()));
    let Some(recorded) = recorded else {
        return Ok(true);
    };
    Ok(recorded != Some(Value::String(launch_fingerprint(ctx, command)?)))
}

/// `_prewarm_note_warm`.
pub fn note_warm(ctx: &LauncherContext, command: &str) -> Result<(), String> {
    let mut marker = Map::new();
    marker.insert("ts".into(), json!(now()));
    marker.insert("command".into(), json!(command));
    marker.insert("launch".into(), json!(launch_fingerprint(ctx, command)?));
    let _ = write_text(
        &warm_marker_path(ctx, command),
        &json_dump(&Value::Object(marker)),
    );
    Ok(())
}

/// `_is_index_unreachable`: a DNS/egress failure to the package index.
pub fn is_index_unreachable(text: &str) -> bool {
    let low = text.to_lowercase();
    let dns = low.contains("dns error")
        || low.contains("name or service not known")
        || low.contains("temporary failure in name resolution")
        || low.contains("could not resolve host");
    dns && (low.contains("pypi") || low.contains("simple"))
}

// ── npx cache reconciliation and the npm cache lock ───────────────────

fn slots(npm_cache: &str) -> Vec<String> {
    let root = pyos::join(npm_cache, "_npx");
    if !is_dir(&root) {
        return Vec::new();
    }
    std::fs::read_dir(&root)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| pyos::join(&root, &e.file_name().to_string_lossy()))
        .collect()
}

/// `_reconcile_npx_cache`: evict `_npx` slots whose lock was resolved under a different cutoff.
pub fn reconcile_npx_cache(npm_cache: &str, cutoff: &str) -> usize {
    let mut evicted = 0;
    for slot in slots(npm_cache) {
        if !is_file(&pyos::join(&slot, "package-lock.json")) {
            continue;
        }
        let marker = pyos::join(&slot, NPX_CUTOFF_MARKER);
        let stamped = std::fs::read_to_string(&marker).unwrap_or_default();
        if pyjson::py_strip(&stamped) == cutoff {
            continue;
        }
        let evict = std::fs::remove_dir_all(&slot)
            .and_then(|_| std::fs::create_dir_all(&slot))
            .and_then(|_| std::fs::write(&marker, cutoff));
        if evict.is_ok() {
            evicted += 1;
        }
    }
    evicted
}

/// `_npx_rebuild_pending`: evicted slots npm has not rebuilt yet (marker present, lock absent).
pub fn npx_rebuild_pending(npm_cache: &str) -> usize {
    slots(npm_cache)
        .iter()
        .filter(|slot| {
            is_file(&pyos::join(slot, NPX_CUTOFF_MARKER))
                && !is_file(&pyos::join(slot, "package-lock.json"))
        })
        .count()
}

/// `reconcile_for_connect`: the pre-flight migration; returns the outstanding npx rebuilds.
pub fn reconcile_for_connect(ctx: &LauncherContext) -> usize {
    let mut env = Env::new();
    ctx.ensure_writable_caches(&mut env);
    let cutoff = env.get("UV_EXCLUDE_NEWER").cloned().unwrap_or_default();
    let cache = env.get("npm_config_cache").cloned().unwrap_or_default();
    if cutoff.is_empty() || cache.is_empty() {
        return 0;
    }
    reconcile_npx_cache(&cache, &cutoff);
    npx_rebuild_pending(&cache)
}

/// `launches_into_npm_cache`.
pub fn launches_into_npm_cache(config: &Value) -> bool {
    let command = if crate::transport::is_stdio(config) {
        config
            .get("command")
            .and_then(Value::as_str)
            .unwrap_or_default()
    } else {
        ""
    };
    NPM_CACHE_LAUNCHERS.contains(&pyos::basename(command))
}

/// The held `npm_cache_lock`; the kernel releases it when this closes.
pub struct NpmCacheLock(#[allow(dead_code)] std::fs::File);

/// `npm_cache_lock`: hold the shared npm cache exclusively across processes.
///
/// `flock(LOCK_EX)` blocks, and the host has one current-thread runtime and no
/// blocking pool, so the wait runs on a thread of its own that hands the held
/// descriptor back (or closes it if the connect went away meanwhile).
pub async fn npm_cache_lock(ctx: &LauncherContext) -> std::io::Result<Option<NpmCacheLock>> {
    use std::os::unix::fs::OpenOptionsExt;
    use std::os::unix::io::AsRawFd;
    let mut env = Env::new();
    ctx.ensure_writable_caches(&mut env);
    let cache = ctx
        .process_env
        .get("npm_config_cache")
        .filter(|c| !c.is_empty())
        .cloned()
        .or_else(|| env.get("npm_config_cache").cloned())
        .unwrap_or_default();
    if cache.is_empty() {
        return Ok(None);
    }
    std::fs::create_dir_all(&cache)?;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(pyos::join(&cache, ".tofu-launch.lock"))?;
    let (sender, receiver) = tokio::sync::oneshot::channel();
    std::thread::spawn(move || {
        // SAFETY: flock on a descriptor this thread owns.
        let held = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } == 0;
        let _ = sender.send(if held {
            Ok(file)
        } else {
            Err(std::io::Error::last_os_error())
        });
    });
    match receiver.await {
        Ok(Ok(file)) => Ok(Some(NpmCacheLock(file))),
        Ok(Err(error)) => Err(error),
        Err(_) => Err(std::io::Error::other("npm cache lock waiter vanished")),
    }
}

// ── pre-warm ──────────────────────────────────────────────────────────

enum WarmFailure {
    Cancelled,
    Incomplete(String),
}

struct Completed {
    returncode: i32,
    stdout: String,
    stderr: String,
}

/// TERM the group, wait the leader through the grace, KILL what remains.
async fn retire_process_group(pid: i32, child: &mut tokio::process::Child) {
    // SAFETY: `pid` leads a session this function's caller created for the warm.
    unsafe { libc::killpg(pid, libc::SIGTERM) };
    let _ = tokio::time::timeout(RETIRE_GRACE_SECONDS, child.wait()).await;
    // SAFETY: as above; ESRCH (nothing left) is the expected outcome.
    unsafe { libc::killpg(pid, libc::SIGKILL) };
    let _ = child.wait().await;
}

/// `_run_warm`: one uv warm as an owned process group, retired on every exit.
async fn run_warm(
    argv: &[String],
    env: &Env,
    stop: &CancellationToken,
) -> Result<Completed, WarmFailure> {
    let mut cmd = tokio::process::Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .env_clear()
        .envs(env)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    // SAFETY: setsid is async-signal-safe (the oracle's `start_new_session=True`).
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let mut child = cmd.spawn().map_err(|e| {
        WarmFailure::Incomplete(match e.raw_os_error() {
            Some(code) => format!(
                "[Errno {code}] {}: {}",
                pyos::strerror(&e),
                py_repr(&argv[0])
            ),
            None => e.to_string(),
        })
    })?;
    let pid = child.id().map(|p| p as i32).unwrap_or(0);
    let mut stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    let outcome = {
        let collect = async {
            let (mut out, mut err) = (Vec::new(), Vec::new());
            let read_out = async {
                if let Some(s) = stdout.as_mut() {
                    let _ = s.read_to_end(&mut out).await;
                }
            };
            let read_err = async {
                if let Some(s) = stderr.as_mut() {
                    let _ = s.read_to_end(&mut err).await;
                }
            };
            let (_, _, status) = tokio::join!(read_out, read_err, child.wait());
            (out, err, status)
        };
        tokio::select! {
            (out, err, status) = collect => {
                let returncode = match status {
                    Ok(status) => {
                        use std::os::unix::process::ExitStatusExt;
                        status.code().unwrap_or_else(|| -status.signal().unwrap_or(0))
                    }
                    Err(_) => -1,
                };
                Ok(Completed {
                    returncode,
                    stdout: String::from_utf8_lossy(&out).into_owned(),
                    stderr: String::from_utf8_lossy(&err).into_owned(),
                })
            }
            _ = tokio::time::sleep(Duration::from_secs(WARM_SECONDS)) => Err(WarmFailure::Incomplete(format!(
                "Command '{}' timed out after {WARM_SECONDS} seconds",
                list_repr(argv)
            ))),
            _ = stop.cancelled() => Err(WarmFailure::Cancelled),
        }
    };
    if pid > 1 {
        retire_process_group(pid, &mut child).await;
    }
    outcome
}

/// The host's shutdown signal for running warms.
pub fn shutdown_token() -> &'static CancellationToken {
    static CELL: OnceLock<CancellationToken> = OnceLock::new();
    CELL.get_or_init(CancellationToken::new)
}

/// `prewarm_vendored_launcher`: resolve a vendored server's isolated env before connect.
pub async fn prewarm_vendored_launcher(
    ctx: &LauncherContext,
    command: &str,
    stop: &CancellationToken,
) -> Result<(bool, String), String> {
    if command.is_empty() || command.contains('/') {
        return Ok((true, String::new()));
    }
    let Some(argv) = vendored_launch_argv(ctx, command)? else {
        return Ok((true, String::new()));
    };
    let cached = unreachable_reason(ctx).await;
    if !cached.is_empty() {
        set_last_error(command, Some(cached.clone()));
        return Ok((false, cached));
    }
    let mut env = stdio_env::inherited_child_environment(&ctx.process_env);
    propagate_proxy_env(ctx, &mut env).await;
    ctx.ensure_writable_caches(&mut env);
    let mut warm: Vec<String> = argv[..argv.len() - 1].to_vec();
    warm.extend([
        "python".into(),
        "-c".into(),
        format!("import {}", command.replace('-', "_")),
    ]);
    let completed = match run_warm(&warm, &env, stop).await {
        Ok(completed) => completed,
        Err(WarmFailure::Cancelled) => {
            return Ok((false, "pre-warm cancelled by shutdown".into()));
        }
        Err(WarmFailure::Incomplete(error)) => {
            let message = format!("uv warm did not complete: {error}");
            set_last_error(command, Some(message.clone()));
            return Ok((false, message));
        }
    };
    if completed.returncode != 0 {
        let output = if completed.stderr.is_empty() {
            completed.stdout
        } else {
            completed.stderr
        };
        let lines = pyos::splitlines(pyjson::py_strip(&output));
        let joined = lines[lines.len().saturating_sub(5)..].join(" | ");
        let message = format!("uv warm exited {}: {joined}", completed.returncode);
        if is_index_unreachable(&joined) {
            note_unreachable(ctx, &format!("package index unreachable: {joined}")).await;
        }
        let _ = std::fs::remove_file(warm_marker_path(ctx, command));
        set_last_error(command, Some(message.clone()));
        return Ok((false, message));
    }
    note_warm(ctx, command)?;
    set_last_error(command, None);
    Ok((true, argv.join(" ")))
}

/// `prewarm_all_vendored`: `{command: 'ok' | reason}` for every launcher with a source.
pub async fn prewarm_all_vendored(
    ctx: &LauncherContext,
    stop: &CancellationToken,
) -> Map<String, Value> {
    let mut out = Map::new();
    let cached = unreachable_reason(ctx).await;
    if !cached.is_empty() {
        for (command, _) in &ctx.launchers {
            if find_vendored_source(ctx, command).is_some() {
                out.insert(command.clone(), json!(cached));
            }
        }
        return out;
    }
    for (command, _) in &ctx.launchers {
        if stop.is_cancelled() {
            break;
        }
        if find_vendored_source(ctx, command).is_none() {
            continue;
        }
        let outcome = match prewarm_vendored_launcher(ctx, command, stop).await {
            Ok((true, _)) => "ok".to_string(),
            Ok((false, detail)) if detail.is_empty() => "warm failed".into(),
            Ok((false, detail)) => detail,
            Err(error) => format!("error: {error}"),
        };
        out.insert(command.clone(), json!(outcome));
    }
    out
}

// ── the stdio connect pre-flight ──────────────────────────────────────

/// `MCP_COLD_INSTALL_TIMEOUT` (`TOFU_MCP_COLD_INSTALL_TIMEOUT`, as the oracle reads it).
pub fn cold_install_timeout(process_env: &Env) -> Duration {
    let seconds = process_env
        .get("TOFU_MCP_COLD_INSTALL_TIMEOUT")
        .and_then(|v| pyjson::py_strip(v).parse::<u64>().ok())
        .unwrap_or(MCP_COLD_INSTALL_TIMEOUT_DEFAULT);
    Duration::from_secs(seconds)
}

/// The launch `connect_server` and `_server_owner` derive before the handshake.
pub struct StdioLaunch {
    pub command: String,
    pub args: Vec<String>,
    /// `cold_install`: an npx rebuild is outstanding or the vendored launch has no warm record.
    pub cold_install: bool,
    /// Held until the handshake ends for a launcher that installs into the npm cache.
    pub npm_lock: Option<NpmCacheLock>,
}

/// `connect_server`'s pre-flight (staleness, npm lock, reconcile, cold budget) and
/// `_server_owner`'s vendored translation, in the oracle's order.
pub async fn plan_stdio_launch(
    ctx: &LauncherContext,
    config: &Value,
    command: &str,
    args: &[String],
) -> Result<StdioLaunch, String> {
    if !command.is_empty() && !command.contains('/') {
        check_snapshot_staleness(ctx, command).await;
    }
    let mut npm_lock = npm_cache_lock(ctx).await.ok().flatten();
    let evicted = reconcile_for_connect(ctx);
    if !launches_into_npm_cache(config) {
        npm_lock = None;
    }
    let cold_vendored = evicted == 0
        && !command.is_empty()
        && vendored_needs_cold_budget(ctx, command).unwrap_or(false);
    let (command, args) = match vendored_launch_argv(ctx, command)? {
        Some(argv) => {
            let mut full = argv[1..].to_vec();
            full.extend(args.iter().cloned());
            (argv[0].clone(), full)
        }
        None => (command.to_string(), args.to_vec()),
    };
    Ok(StdioLaunch {
        command,
        args,
        cold_install: evicted > 0 || cold_vendored,
        npm_lock,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn egress_identity_never_carries_the_credential() {
        assert_eq!(
            split_identity("http://alice:s3cr3t@GW.internal:8080/x").unwrap(),
            "http://gw.internal:8080:alice"
        );
        assert_eq!(split_identity("socks5://h").unwrap(), "socks5://h::");
        assert!(split_identity("http://h:80a").is_err());
        assert!(split_identity("http://h:70000").is_err());
        assert!(split_identity("http://[::1").is_err());
        assert_eq!(split_identity("localhost").unwrap(), "://::");
    }

    #[test]
    fn exact_pins_only() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("pyproject.toml"),
            "[project]\nname = \"p\"\ndependencies = [\"Exact_Pkg[extra]==1.2.3\", \"marked==2.0; python_version >= '3'\", \"floating>=1\", \"wild==1.*\", \"ranged==1.0,<2\"]\n",
        )
        .unwrap();
        let source = dir.path().to_string_lossy().into_owned();
        assert_eq!(exact_pin_exemptions(&[source]), vec!["exact-pkg", "marked"]);
    }

    #[test]
    fn index_unreachable_is_dns_to_the_index_only() {
        assert!(is_index_unreachable(
            "Failed to fetch https://pypi.org/simple/mcp/ dns error: Name or service not known"
        ));
        assert!(!is_index_unreachable("error: distribution mcp not found"));
        assert!(!is_index_unreachable(""));
    }
}
