//! The exact MCP stdio child environment (`lib/mcp/stdio_environment.py` with
//! `lib/tools/process_environment.py` and `lib/mcp/client/_vendor.py`'s cache
//! and proxy seams, the Python oracles).
//!
//! Order matters and follows the oracle: inherited environment minus ambient
//! credentials and the server's storage handles; the config overlay (vault
//! references resolved, fail closed); the owner identity and its Hope policy
//! path; `TOFU_DATA_DIR` bound to this instance's runtime base; proxies; the
//! tool cache redirect (cache AND data dirs, supply cutoff); tool PATHs; an
//! explicit overlay `PATH` wins last.

use crate::transport::{resolve_vault_env_references, VaultResolver};
use regex::Regex;
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::OnceLock;

pub type Env = BTreeMap<String, String>;

pub const SUPPLY_CUTOFF_DEFAULT: &str = "2026-08-14T00:00:00Z";
const POSIX_DEFPATH: &str = "/bin:/usr/bin";
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
const SENSITIVE_EXACT: [&str; 7] = [
    "AWS_ACCESS_KEY_ID",
    "DATABASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "MYSQL_PWD",
    "PGPASSWORD",
    "REDIS_URL",
    "SSH_AUTH_SOCK",
];
const SERVER_PRIVATE_STORAGE_ENV: [&str; 6] = [
    "TOFU_STORAGE_CONNECTION_FILE",
    "TOFU_STORAGE_PARENT_PID",
    "TOFU_STORAGE_RETAINED_ENGINE_SHA256",
    "TOFU_STORAGE_SERVICE_ID",
    "TOFU_STORAGE_SERVICE_STATE",
    "TOFU_STORAGE_TOKEN",
];

/// The launching process's facts the oracle reads from globals.
pub struct EnvContext<'a> {
    /// This process's environment (`os.environ`).
    pub process_env: Env,
    /// `runtime_path_policy.get_runtime_base()`.
    pub runtime_base: String,
    /// The repository root, for the bundled `.tofu/node-toolchain/bin`.
    pub repo_root: String,
    /// `f'{major}.{minor}'` of the server interpreter (`UV_PYTHON` pin).
    pub python_minor: String,
    /// The owner's Global Router policy file (`hope_queue_policy_launcher.policy_path`).
    pub policy_path: &'a dyn Fn(u64) -> String,
    pub vault: &'a dyn VaultResolver,
    /// `lib.proxy.first_reachable_global_proxy_url()`, when the app has one.
    pub pool_proxy: Option<String>,
    pub prepare_directories: bool,
}

fn sensitive_suffix() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(r"(?:^|_)(?:API_KEYS?|TOKENS?|SECRETS?|PASSWORDS?|PASSWDS?|PRIVATE_KEY|ACCESS_KEY|AUTH)$")
            .expect("static pattern")
    })
}

fn is_sensitive_inherited(name: &str) -> bool {
    let upper = name.to_uppercase();
    SENSITIVE_EXACT.contains(&upper.as_str())
        || SERVER_PRIVATE_STORAGE_ENV.contains(&upper.as_str())
        || sensitive_suffix().is_match(&upper)
}

/// `inherited_child_environment`.
pub fn inherited_child_environment(process_env: &Env) -> Env {
    process_env
        .iter()
        .filter(|(name, _)| !is_sensitive_inherited(name))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// `validate_environment` (names and values).
pub fn validate_environment(environment: &Env) -> Result<(), String> {
    for (key, value) in environment {
        if key.is_empty() || key.contains('=') || key.contains('\0') {
            return Err("Invalid subprocess environment variable name".into());
        }
        if value.contains('\0') {
            return Err("Subprocess environment values must be strings without NUL".into());
        }
    }
    Ok(())
}

/// `tool_cache_defaults`.
pub fn tool_cache_defaults(cache_root: &str) -> Vec<(String, String)> {
    let root = crate::pyos::normpath(cache_root);
    let share = format!("{root}/share");
    vec![
        ("UV_CACHE_DIR".into(), format!("{root}/uv")),
        ("XDG_CACHE_HOME".into(), root.clone()),
        ("npm_config_cache".into(), format!("{root}/npm")),
        ("PIP_CACHE_DIR".into(), format!("{root}/pip")),
        ("XDG_DATA_HOME".into(), share.clone()),
        ("UV_PYTHON_INSTALL_DIR".into(), format!("{share}/uv/python")),
        ("UV_TOOL_DIR".into(), format!("{share}/uv/tools")),
        ("UV_TOOL_BIN_DIR".into(), format!("{root}/bin")),
        ("UV_LINK_MODE".into(), "copy".into()),
    ]
}

fn home_of(environment: &Env, process_env: &Env) -> String {
    environment
        .get("HOME")
        .or_else(|| environment.get("USERPROFILE"))
        .filter(|h| !h.is_empty())
        .or_else(|| process_env.get("HOME"))
        .cloned()
        .unwrap_or_default()
}

/// `chromium_env.playwright_registry` on Linux (the `'0'` package-local form never moves).
fn playwright_registry(environment: &Env, process_env: &Env) -> Option<String> {
    let home = home_of(environment, process_env);
    let registry = match environment
        .get("PLAYWRIGHT_BROWSERS_PATH")
        .map(String::as_str)
    {
        Some("0") => return None,
        Some(path) if !path.is_empty() => path.to_string(),
        _ => {
            let cache = environment
                .get("XDG_CACHE_HOME")
                .filter(|c| !c.is_empty())
                .cloned()
                .unwrap_or(format!("{home}/.cache"));
            format!("{cache}/ms-playwright")
        }
    };
    Some(if registry.starts_with('/') {
        crate::pyos::normpath(&registry)
    } else {
        let cwd = std::env::current_dir()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        crate::pyos::normpath(&crate::pyos::join(&cwd, &registry))
    })
}

/// `apply_tool_paths` (non-interpreter-first form).
pub fn apply_tool_paths(
    environment: &mut Env,
    ctx: &EnvContext,
    cache_root: Option<&str>,
) -> Result<(), String> {
    validate_environment(environment)?;
    if let Some(cache_root) = cache_root {
        let registry = playwright_registry(environment, &ctx.process_env);
        for (key, value) in tool_cache_defaults(cache_root) {
            environment.entry(key).or_insert(value);
        }
        if let Some(registry) = registry {
            if playwright_registry(environment, &ctx.process_env).as_ref() != Some(&registry) {
                environment.insert("PLAYWRIGHT_BROWSERS_PATH".into(), registry);
            }
        }
    }
    let home = environment
        .get("HOME")
        .or_else(|| environment.get("USERPROFILE"))
        .filter(|h| !h.is_empty())
        .cloned();
    let mut bins: Vec<String> = Vec::new();
    let bundled = format!("{}/.tofu/node-toolchain/bin", ctx.repo_root);
    if std::path::Path::new(&bundled).is_dir() {
        bins.push(bundled);
    }
    if let Some(dir) = environment.get("UV_TOOL_BIN_DIR").filter(|d| !d.is_empty()) {
        bins.push(dir.clone());
    }
    if let Some(home) = home {
        bins.push(format!("{home}/.local/bin"));
        bins.push(format!("{home}/.cargo/bin"));
    }
    if let Some(cargo) = environment.get("CARGO_HOME").filter(|d| !d.is_empty()) {
        bins.push(format!("{cargo}/bin"));
    }
    let npm_prefix = environment
        .get("npm_config_prefix")
        .filter(|p| !p.is_empty())
        .or_else(|| {
            environment
                .get("NPM_CONFIG_PREFIX")
                .filter(|p| !p.is_empty())
        });
    if let Some(prefix) = npm_prefix {
        bins.push(format!("{prefix}/bin"));
    }
    let existing = environment
        .get("PATH")
        .cloned()
        .unwrap_or_else(|| POSIX_DEFPATH.into());
    let mut path: Vec<String> = Vec::new();
    for entry in bins
        .into_iter()
        .chain(existing.split(':').map(str::to_string))
    {
        if !path.contains(&entry) {
            path.push(entry);
        }
    }
    environment.insert("PATH".into(), path.join(":"));
    validate_environment(environment)
}

/// `tool_cache_root`.
pub fn tool_cache_root(ctx: &EnvContext) -> String {
    match ctx
        .process_env
        .get("TOFU_MCP_CACHE_DIR")
        .filter(|d| !d.is_empty())
    {
        Some(dir) => dir.clone(),
        None => format!("{}/data/mcp-cache", ctx.runtime_base),
    }
}

/// `_ensure_writable_caches`: cache and data dirs into the tool cache, plus the supply cutoff.
pub fn ensure_writable_caches(environment: &mut Env, ctx: &EnvContext) -> Result<(), String> {
    let cache_root = tool_cache_root(ctx);
    if ctx.prepare_directories {
        let made = std::fs::create_dir_all(&cache_root)
            .and_then(|_| std::fs::create_dir_all(format!("{cache_root}/share")));
        if made.is_err() {
            return Ok(());
        }
    }
    let cutoff = ctx
        .process_env
        .get("TOFU_MCP_SUPPLY_CUTOFF")
        .map(|c| c.trim().to_string())
        .unwrap_or_else(|| SUPPLY_CUTOFF_DEFAULT.to_string());
    apply_tool_paths(environment, ctx, Some(&cache_root))?;
    if !cutoff.is_empty() {
        environment
            .entry("UV_EXCLUDE_NEWER".into())
            .or_insert(cutoff.clone());
        let date = cutoff.split('T').next().unwrap_or_default().to_string();
        environment
            .entry("npm_config_before".into())
            .or_insert(date);
        environment
            .entry("UV_PYTHON".into())
            .or_insert(ctx.python_minor.clone());
    }
    Ok(())
}

/// `_propagate_proxy_env` with its proxy-pool last resort.
pub fn propagate_proxy_env(environment: &mut Env, ctx: &EnvContext) {
    for key in PROXY_ENV_KEYS {
        if environment.get(key).is_some_and(|v| !v.is_empty()) {
            continue;
        }
        let alias = match key {
            "HTTPS_PROXY" => Some("TOFU_HTTPS_PROXY"),
            "HTTP_PROXY" => Some("TOFU_HTTP_PROXY"),
            "ALL_PROXY" => Some("TOFU_ALL_PROXY"),
            "NO_PROXY" => Some("TOFU_NO_PROXY"),
            _ => None,
        };
        let value = ctx
            .process_env
            .get(key)
            .filter(|v| !v.is_empty())
            .or_else(|| {
                alias
                    .and_then(|a| ctx.process_env.get(a))
                    .filter(|v| !v.is_empty())
            });
        if let Some(value) = value {
            environment.insert(key.into(), value.clone());
        }
    }
    let web = ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"];
    if web
        .iter()
        .any(|key| environment.get(*key).is_some_and(|v| !v.is_empty()))
    {
        return;
    }
    if let Some(url) = ctx.pool_proxy.as_ref().filter(|u| !u.is_empty()) {
        for key in web {
            environment.entry(key.into()).or_insert(url.clone());
        }
    }
}

/// `build_stdio_environment`: the exact environment an MCP stdio child receives.
pub fn build_stdio_environment(
    config: &Value,
    owner_user_id: Option<u64>,
    ctx: &EnvContext,
) -> Result<Env, String> {
    let config = config
        .as_object()
        .ok_or("MCP configuration must be an object")?;
    let mut environment = inherited_child_environment(&ctx.process_env);
    let overlay = match config.get("env") {
        None | Some(Value::Null) => serde_json::Map::new(),
        Some(Value::Object(map)) => map.clone(),
        Some(other) if !crate::pyos::truthy(Some(other)) => serde_json::Map::new(),
        Some(_) => return Err("MCP env configuration must be a string mapping".into()),
    };
    let overlay = resolve_vault_env_references(&overlay, ctx.vault)?;
    let mut overlay_env = Env::new();
    for (key, value) in &overlay {
        let Some(text) = value.as_str() else {
            return Err("Subprocess environment values must be strings without NUL".into());
        };
        overlay_env.insert(key.clone(), text.to_string());
    }
    validate_environment(&overlay_env)?;
    environment.extend(overlay_env.clone());
    match owner_user_id {
        Some(0) => return Err("MCP owner_user_id must be a positive integer".into()),
        Some(owner) => {
            environment.insert("TOFU_MCP_OWNER_ID".into(), format!("tofu:{owner}"));
            environment.insert(
                crate::queue_policy::POLICY_ENV.into(),
                (ctx.policy_path)(owner),
            );
        }
        None if overlay_env.contains_key("TOFU_MCP_OWNER_ID") => {
            return Err("MCP owner cache identity cannot be supplied in server env".into())
        }
        None => {
            environment.remove("TOFU_MCP_OWNER_ID");
            environment.remove(crate::queue_policy::POLICY_ENV);
        }
    }
    environment
        .entry("TOFU_DATA_DIR".into())
        .or_insert(format!("{}/data", ctx.runtime_base));
    propagate_proxy_env(&mut environment, ctx);
    environment
        .entry("NODE_USE_ENV_PROXY".into())
        .or_insert("1".into());
    ensure_writable_caches(&mut environment, ctx)?;
    apply_tool_paths(&mut environment, ctx, None)?;
    if let Some(path) = overlay_env.get("PATH") {
        environment.insert("PATH".into(), path.clone());
    }
    validate_environment(&environment)?;
    Ok(environment)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::transport::NoVault;
    use serde_json::json;

    fn ctx<'a>(base: &str, policy: &'a dyn Fn(u64) -> String) -> EnvContext<'a> {
        let mut process_env = Env::new();
        process_env.insert("HOME".into(), "/home/u".into());
        process_env.insert("PATH".into(), "/usr/bin:/bin".into());
        process_env.insert("GITHUB_TOKEN".into(), "ambient".into());
        process_env.insert("TOFU_STORAGE_TOKEN".into(), "live".into());
        EnvContext {
            process_env,
            runtime_base: base.into(),
            repo_root: "/nonexistent-repo".into(),
            python_minor: "3.12".into(),
            policy_path: policy,
            vault: &NoVault,
            pool_proxy: None,
            prepare_directories: true,
        }
    }

    #[test]
    fn owner_runtime_base_and_caches_are_bound() {
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path().to_str().unwrap();
        let policy = |owner: u64| format!("/router/u{owner}/HOPE_QUEUE_POLICY.json");
        let env = build_stdio_environment(
            &json!({"command": "x", "env": {"A": "1"}}),
            Some(7),
            &ctx(base, &policy),
        )
        .unwrap();
        assert_eq!(env["TOFU_MCP_OWNER_ID"], "tofu:7");
        assert_eq!(
            env["TOFU_HOPE_QUEUE_POLICY"],
            "/router/u7/HOPE_QUEUE_POLICY.json"
        );
        assert_eq!(env["TOFU_DATA_DIR"], format!("{base}/data"));
        assert_eq!(env["XDG_DATA_HOME"], format!("{base}/data/mcp-cache/share"));
        assert_eq!(env["UV_LINK_MODE"], "copy");
        assert_eq!(env["UV_PYTHON"], "3.12");
        assert_eq!(env["NODE_USE_ENV_PROXY"], "1");
        assert!(!env.contains_key("GITHUB_TOKEN") && !env.contains_key("TOFU_STORAGE_TOKEN"));
        assert!(env["PATH"].starts_with(&format!("{base}/data/mcp-cache/bin:/home/u/.local/bin")));
        let refused = build_stdio_environment(
            &json!({"env": {"TOFU_MCP_OWNER_ID": "tofu:1"}}),
            None,
            &ctx(base, &policy),
        );
        assert_eq!(
            refused.unwrap_err(),
            "MCP owner cache identity cannot be supplied in server env"
        );
        let vault = build_stdio_environment(
            &json!({"env": {"T": "vault:github_token"}}),
            None,
            &ctx(base, &policy),
        );
        assert!(vault.unwrap_err().contains("which is not configured"));
        let explicit = build_stdio_environment(
            &json!({"env": {"PATH": "/only", "TOFU_DATA_DIR": "/d"}}),
            None,
            &ctx(base, &policy),
        )
        .unwrap();
        assert_eq!(
            (
                explicit["PATH"].as_str(),
                explicit["TOFU_DATA_DIR"].as_str()
            ),
            ("/only", "/d")
        );
    }
}
