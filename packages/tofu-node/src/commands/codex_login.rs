//! Permission-gated, access-only Codex login snapshot (`codex_login_read`).
//! Port of `lib/desktop_agent/_codex_login.py`,
//! `lib/oauth/codex_local_login.py::read_local_login(refresh=True)` and the
//! parts of `lib/oauth/codex_command_login.py` that read uses.
//!
//! No caller-supplied paths, shell commands, logout or secret-file writes:
//! the only param is the expected `account_id`. On POSIX the installed Codex
//! (`codex app-server` through the account's login shell, so its own setup
//! finds it) names its home and policy, and alone renews an expired access
//! token (`account/read` with `refreshToken`). Without a Codex command (and
//! always on Windows) the file seam `$CODEX_HOME` / `~/.codex` is read.
//! Results carry the access token only, never the refresh token; failures
//! are fixed non-secret `shared_login_issue` codes.
//!
//! Native protocol bounds (from `codex_command_login.py`): 4 s per exchange,
//! a cold start budget learned per launcher environment within [4 s, 30 s];
//! a start that times out at the ceiling refuses that launcher's cold starts
//! for 60 s. Successful native metadata is reused for 30 s and serves as a
//! grace answer for transient native failures for 120 s, both only while the
//! local policy/shell files are unchanged.

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

const MAX_FILE_BYTES: u64 = 1024 * 1024;
const ACCOUNT_ID_MAX_CHARS: usize = 256;

pub fn read(params: &Value) -> Value {
    let Some(map) = params.as_object() else {
        return json!({"error": "invalid_codex_login_request"});
    };
    let account = match map.get("account_id") {
        None => "",
        Some(Value::String(s)) if s.chars().count() <= ACCOUNT_ID_MAX_CHARS => s.as_str(),
        Some(_) => return json!({"error": "invalid_codex_login_request"}),
    };
    if map.keys().any(|k| k != "account_id") {
        return json!({"error": "invalid_codex_login_request"});
    }
    let environment = inherited_child_environment();
    let home = environment
        .get("HOME")
        .filter(|h| !h.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(crate::config::home_dir);
    Value::Object(read_local_login(account, &environment, &home, true))
}

type Snapshot = Map<String, Value>;

fn issue(code: &str) -> Snapshot {
    let mut out = Map::new();
    out.insert("shared_login_issue".into(), code.into());
    out
}

fn issue_of(snapshot: &Snapshot) -> &str {
    snapshot
        .get("shared_login_issue")
        .and_then(Value::as_str)
        .unwrap_or("")
}

/// `lib/tools/process_environment.inherited_child_environment`: this process's
/// environment without ambient credentials or server storage handles.
fn inherited_child_environment() -> BTreeMap<String, String> {
    std::env::vars_os()
        .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?)))
        .filter(|(k, _)| !sensitive_env(k))
        .collect()
}

fn sensitive_env(name: &str) -> bool {
    const EXACT: &[&str] = &[
        "AWS_ACCESS_KEY_ID",
        "DATABASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "MYSQL_PWD",
        "PGPASSWORD",
        "REDIS_URL",
        "SSH_AUTH_SOCK",
        "TOFU_STORAGE_CONNECTION_FILE",
        "TOFU_STORAGE_PARENT_PID",
        "TOFU_STORAGE_RETAINED_ENGINE_SHA256",
        "TOFU_STORAGE_SERVICE_ID",
        "TOFU_STORAGE_SERVICE_STATE",
        "TOFU_STORAGE_TOKEN",
    ];
    const SUFFIXES: &[&str] = &[
        "API_KEY",
        "API_KEYS",
        "TOKEN",
        "TOKENS",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWORDS",
        "PASSWD",
        "PASSWDS",
        "PRIVATE_KEY",
        "ACCESS_KEY",
        "AUTH",
    ];
    let upper = name.to_ascii_uppercase();
    EXACT.contains(&upper.as_str())
        || SUFFIXES
            .iter()
            .any(|suffix| upper == *suffix || upper.ends_with(&format!("_{suffix}")))
}

// ── read_local_login ────────────────────────────────────────────────────────

fn read_local_login(
    expected: &str,
    environment: &BTreeMap<String, String>,
    home: &Path,
    refresh: bool,
) -> Snapshot {
    let flight_started = std::time::Instant::now();
    let context = match native::command_context(environment, home, flight_started) {
        Ok(context) => context,
        Err(code) if code == "codex_command_missing" => {
            // The file seam reads the process environment, as the Python node does.
            let codex_home = std::env::var("CODEX_HOME").ok().filter(|v| !v.is_empty());
            return read_file_login(expected, codex_home.as_deref(), home, None, false);
        }
        Err(code) => return issue(&code),
    };
    let snapshot = read_file_login(
        expected,
        Some(&context.codex_home),
        home,
        Some(&context.settings),
        true,
    );
    if !(refresh && issue_of(&snapshot) == "codex_access_expired") {
        return snapshot;
    }
    let account = snapshot
        .get("account_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    // A changed or malformed source never authorizes renewal or rebinding.
    match native::renew(&context, &account, environment, home, flight_started) {
        Err(code) => {
            let mut out = issue(&code);
            out.insert("account_id".into(), account.into());
            out
        }
        Ok(renewed) => read_file_login(
            if expected.is_empty() {
                &account
            } else {
                expected
            },
            Some(&renewed.codex_home),
            home,
            Some(&renewed.settings),
            true,
        ),
    }
}

// ── the credential file (`_read_file_login`) ────────────────────────────────

enum FileError {
    NotFound,
    Unreadable,
}

impl From<std::io::Error> for FileError {
    fn from(error: std::io::Error) -> Self {
        if error.kind() == std::io::ErrorKind::NotFound {
            FileError::NotFound
        } else {
            FileError::Unreadable
        }
    }
}

/// Open without following a final symlink; require an owned regular file
/// that group/others cannot write, at most 1 MiB.
fn read_private(path: &Path) -> Result<Vec<u8>, FileError> {
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let mut file = options.open(path)?;
    let info = file.metadata()?;
    if !info.is_file() || info.len() > MAX_FILE_BYTES {
        return Err(FileError::Unreadable);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: getuid has no preconditions.
        if info.uid() != unsafe { libc::getuid() } || info.mode() & 0o022 != 0 {
            return Err(FileError::Unreadable);
        }
    }
    let mut data = Vec::new();
    (&mut file)
        .take(MAX_FILE_BYTES + 1)
        .read_to_end(&mut data)?;
    if data.len() as u64 > MAX_FILE_BYTES {
        return Err(FileError::Unreadable);
    }
    Ok(data)
}

/// `root/name` with `root` canonical; only a symlinked leaf is followed.
fn resolved_child(root: &Path, name: &str) -> Result<PathBuf, FileError> {
    let candidate = fs::canonicalize(root)?.join(name);
    if fs::symlink_metadata(&candidate)?.file_type().is_symlink() {
        return Ok(fs::canonicalize(&candidate)?);
    }
    Ok(candidate)
}

/// Unverified JWT payload claims (metadata, not authorization).
fn claims(token: Option<&Value>) -> Map<String, Value> {
    use base64::Engine;
    let Some(token) = token.and_then(Value::as_str).filter(|t| t.len() <= 65536) else {
        return Map::new();
    };
    let Some(part) = token.split('.').nth(1) else {
        return Map::new();
    };
    let cleaned: String = part
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        .collect();
    base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(cleaned)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn truthy(value: Option<&Value>) -> bool {
    value.is_some_and(|v| !super::egress::falsy(v))
}

/// Python `x or {}` for a claim object; Err when present but not an object.
fn object_or_empty(value: Option<&Value>) -> Result<Map<String, Value>, FileError> {
    match value.filter(|v| !super::egress::falsy(v)) {
        None => Ok(Map::new()),
        Some(Value::Object(map)) => Ok(map.clone()),
        Some(_) => Err(FileError::Unreadable),
    }
}

fn toml_to_json(value: toml::Value) -> Value {
    match value {
        toml::Value::String(s) => Value::String(s),
        toml::Value::Integer(i) => i.into(),
        toml::Value::Float(f) => json!(f),
        toml::Value::Boolean(b) => b.into(),
        toml::Value::Datetime(d) => Value::String(d.to_string()),
        toml::Value::Array(items) => Value::Array(items.into_iter().map(toml_to_json).collect()),
        toml::Value::Table(table) => Value::Object(
            table
                .into_iter()
                .map(|(k, v)| (k, toml_to_json(v)))
                .collect(),
        ),
    }
}

fn read_file_login(
    expected: &str,
    codex_home: Option<&str>,
    home: &Path,
    command_settings: Option<&Map<String, Value>>,
    command_verified: bool,
) -> Snapshot {
    let root = codex_home
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".codex"));
    if !root.is_absolute() {
        return issue("invalid_codex_home");
    }
    match file_login(expected, &root, command_settings, command_verified) {
        Ok(snapshot) => snapshot,
        Err(FileError::NotFound) => issue("codex_not_signed_in"),
        // Never include parser errors: they can embed credential contents.
        Err(FileError::Unreadable) => issue("codex_credentials_unreadable"),
    }
}

fn file_login(
    expected: &str,
    root: &Path,
    command_settings: Option<&Map<String, Value>>,
    command_verified: bool,
) -> Result<Snapshot, FileError> {
    let config = root.join("config.toml");
    let mut settings = if config.exists() {
        let path = if command_verified {
            resolved_child(root, "config.toml")?
        } else {
            config
        };
        let text = String::from_utf8(read_private(&path)?).map_err(|_| FileError::Unreadable)?;
        let table: toml::Table = toml::from_str(&text).map_err(|_| FileError::Unreadable)?;
        match toml_to_json(toml::Value::Table(table)) {
            Value::Object(map) => map,
            _ => Map::new(),
        }
    } else {
        Map::new()
    };
    if let Some(overrides) = command_settings {
        settings.extend(overrides.clone());
    }
    if settings
        .get("cli_auth_credentials_store")
        .is_some_and(|store| store != "file")
    {
        return Ok(issue("credential_store_unsupported"));
    }
    // Managed auth policies are interpreted by Codex, not bypassed here.
    if settings
        .get("forced_login_method")
        .is_some_and(|m| !m.is_null() && m != "chatgpt")
    {
        return Ok(issue("login_method_restricted"));
    }
    let path = if command_verified {
        resolved_child(root, "auth.json")?
    } else {
        root.join("auth.json")
    };
    let data: Value =
        serde_json::from_slice(&read_private(&path)?).map_err(|_| FileError::Unreadable)?;
    let Some(data) = data.as_object() else {
        return Ok(issue("not_chatgpt_login"));
    };
    if data
        .get("auth_mode")
        .is_some_and(|m| !m.is_null() && m != "chatgpt")
    {
        return Ok(issue("not_chatgpt_login"));
    }
    let Some(tokens) = data.get("tokens").and_then(Value::as_object) else {
        return Ok(issue("not_chatgpt_login"));
    };
    let access = tokens.get("access_token");
    let access_claims = claims(access);
    let identity = claims(tokens.get("id_token"));
    let auth = object_or_empty(access_claims.get("https://api.openai.com/auth"))?;
    let identity_auth = object_or_empty(identity.get("https://api.openai.com/auth"))?;
    let account = if truthy(tokens.get("account_id")) {
        tokens.get("account_id")
    } else {
        auth.get("chatgpt_account_id")
    };
    let account = match account.and_then(Value::as_str) {
        Some(a) if !a.is_empty() && a.chars().count() <= ACCOUNT_ID_MAX_CHARS => a.to_string(),
        _ => return Err(FileError::Unreadable),
    };
    for declared in [
        auth.get("chatgpt_account_id"),
        identity_auth.get("chatgpt_account_id"),
    ] {
        if truthy(declared) && declared != Some(&Value::String(account.clone())) {
            return Ok(issue("account_mismatch"));
        }
    }
    let forced = settings.get("forced_chatgpt_workspace_id");
    let required = if truthy(forced) {
        forced.cloned()
    } else if expected.is_empty() {
        None
    } else {
        Some(Value::String(expected.to_string()))
    };
    if required.is_some_and(|r| r != account.as_str())
        || (!expected.is_empty() && account != expected)
    {
        return Ok(issue("account_changed"));
    }
    let expire = access_claims.get("exp").filter(|e| e.is_number()).cloned();
    let Some(expire_seconds) = expire
        .as_ref()
        .and_then(Value::as_f64)
        .filter(|e| e.is_finite() && *e > 0.0)
    else {
        return Err(FileError::Unreadable);
    };
    if expire_seconds <= crate::util::unix_now() {
        let mut out = issue("codex_access_expired");
        out.insert("account_id".into(), account.into());
        return Ok(out);
    }
    let text = |source: &Map<String, Value>, key: &str| {
        super::egress::text(&Value::Object(source.clone()), key, "")
    };
    let plan = {
        let primary = text(&auth, "chatgpt_plan_type");
        if primary.is_empty() {
            text(&identity_auth, "chatgpt_plan_type")
        } else {
            primary
        }
    };
    let mut out = Map::new();
    out.insert(
        "access_token".into(),
        access.cloned().unwrap_or(Value::Null),
    );
    out.insert("account_id".into(), account.into());
    out.insert("expire".into(), expire.unwrap_or(Value::Null));
    out.insert(
        "email".into(),
        crate::util::truncate_chars(&text(&identity, "email"), 320).into(),
    );
    out.insert(
        "plan_type".into(),
        crate::util::truncate_chars(&plan, 64).into(),
    );
    out.insert("credential_source".into(), "local_codex".into());
    out.insert("shared_login_issue".into(), "".into());
    Ok(out)
}

// ── the native Codex command (`codex_command_login.py`) ─────────────────────

/// What native Codex reported: its authoritative home and login policy.
#[derive(Clone, Debug)]
pub struct Context {
    codex_home: String,
    settings: Map<String, Value>,
}

#[cfg(not(unix))]
mod native {
    //! Windows keeps the strict file-only adapter (no native launcher yet).
    use super::Context;
    use std::collections::BTreeMap;
    use std::path::Path;
    use std::time::Instant;

    pub fn command_context(
        _environment: &BTreeMap<String, String>,
        _home: &Path,
        _flight_started: Instant,
    ) -> Result<Context, String> {
        Err("codex_command_missing".into())
    }

    pub fn renew(
        _context: &Context,
        _account: &str,
        _environment: &BTreeMap<String, String>,
        _home: &Path,
        _flight_started: Instant,
    ) -> Result<Context, String> {
        Err("codex_command_missing".into())
    }
}

#[cfg(unix)]
mod native {
    use super::Context;
    use crate::log;
    use serde_json::{json, Map, Value};
    use std::collections::BTreeMap;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::path::{Path, PathBuf};
    use std::process::{Child, ChildStdin, Command, Stdio};
    use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
    use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
    use std::time::{Duration, Instant};

    const EXCHANGE: Duration = Duration::from_secs(4);
    const STARTUP_CEILING: Duration = Duration::from_secs(30);
    const STARTUP_BUDGET_SIGNATURES: usize = 16;
    const STARTUP_BUDGET_DECAY: f64 = 0.75;
    const STARTUP_EXHAUSTED: Duration = Duration::from_secs(60);
    const CONTEXT_TTL: Duration = Duration::from_secs(30);
    const CONTEXT_MAX_AGE: Duration = Duration::from_secs(120);
    const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
    const MAX_LINE_BYTES: usize = 65536;
    const INBOX_MESSAGES: usize = 16;
    const POLL: Duration = Duration::from_millis(100);
    const MIN_HOME_FREE_INODES: u64 = 1024;
    const MIN_HOME_FREE_BYTES: u64 = 64 * 1024 * 1024;
    const HOME_UNAVAILABLE: &str = "codex_home_unavailable";
    const HOME_STORAGE_FULL: &str = "codex_home_storage_full";
    const UNAVAILABLE: &str = "codex_command_unavailable";
    const TIMEOUT: &str = "codex_command_timeout";
    const TRANSIENT: &[&str] = &[TIMEOUT, UNAVAILABLE, HOME_STORAGE_FULL, HOME_UNAVAILABLE];

    type Env = BTreeMap<String, String>;
    type Revision = Vec<String>;

    struct Verified {
        signature: String,
        at: Instant,
        context: Context,
        revision: Revision,
    }

    #[derive(Default)]
    struct Startup {
        budgets: Vec<(String, f64)>,
        exhausted: Vec<(String, Instant)>,
    }

    fn lock<T>(cell: &'static OnceLock<Mutex<T>>) -> MutexGuard<'static, T>
    where
        T: Default,
    {
        cell.get_or_init(Default::default)
            .lock()
            .unwrap_or_else(|e| e.into_inner())
    }

    static VERIFIED: OnceLock<Mutex<Option<Verified>>> = OnceLock::new();
    static STARTUP: OnceLock<Mutex<Startup>> = OnceLock::new();
    /// One native flight at a time (the Python adapter's `_lock`).
    static FLIGHT: OnceLock<Mutex<()>> = OnceLock::new();

    // ── launcher environment ────────────────────────────────────────────────

    fn account_shell() -> String {
        // SAFETY: getpwuid returns a pointer into static storage or null; the
        // shell string is copied before any other passwd call.
        unsafe {
            let entry = libc::getpwuid(libc::getuid());
            if entry.is_null() || (*entry).pw_shell.is_null() {
                return String::new();
            }
            std::ffi::CStr::from_ptr((*entry).pw_shell)
                .to_string_lossy()
                .into_owned()
        }
    }

    /// The account's login shell, not a service launcher's `SHELL`.
    fn native_shell(environment: &Env) -> Option<String> {
        let from_env = environment.get("SHELL").cloned().unwrap_or_default();
        [account_shell(), from_env].into_iter().find(|shell| {
            let path = Path::new(shell);
            !shell.is_empty()
                && path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| ["bash", "zsh", "fish", "sh", "dash"].contains(&n))
                && path.is_file()
        })
    }

    fn child_environment(environment: &Env, home: &Path) -> Env {
        let mut child: Env = environment
            .iter()
            .filter(|(k, _)| {
                !["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"].contains(&k.as_str())
                    && !k.starts_with("OPENAI_IDENTITY_")
            })
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        if let Some(codex_home) = environment.get("CODEX_HOME").filter(|v| !v.is_empty()) {
            child.insert("TOFU_CODEX_NATIVE_HOME".into(), codex_home.clone());
        }
        let home_text = home.to_string_lossy().into_owned();
        child.insert("HOME".into(), home_text.clone());
        child.insert("PWD".into(), home_text);
        if let Some(shell) = native_shell(environment) {
            child.insert("SHELL".into(), shell);
        }
        child
    }

    /// Digest of every launcher input; values are never retained or logged.
    fn environment_revision(environment: &Env, home: &Path) -> String {
        let payload = serde_json::to_vec(&child_environment(environment, home)).unwrap_or_default();
        crate::util::sha256_hex(&payload)
    }

    /// Stat only an explicit `CODEX_HOME`; never create or search homes.
    fn explicit_home_issue(environment: &Env, home: &Path) -> &'static str {
        let Some(codex_home) = environment.get("CODEX_HOME").filter(|v| !v.is_empty()) else {
            return "";
        };
        let root = Path::new(codex_home);
        let root = if root.is_absolute() {
            root.to_path_buf()
        } else {
            home.join(root)
        };
        match std::fs::metadata(&root) {
            Ok(info) if info.is_dir() => "",
            Ok(_) => HOME_UNAVAILABLE,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => HOME_UNAVAILABLE,
            Err(e) if e.raw_os_error() == Some(libc::ENOTDIR) => HOME_UNAVAILABLE,
            Err(e) if e.raw_os_error() == Some(libc::ENOSPC) => HOME_STORAGE_FULL,
            Err(_) => UNAVAILABLE,
        }
    }

    fn unverifiable(issue: &str) -> bool {
        issue == HOME_UNAVAILABLE || issue == UNAVAILABLE
    }

    /// Fresh home evidence refines only success or unqualified outages.
    fn recheck_home_failure(issue: &str, home_issue: &str) -> String {
        let refinable = [
            "",
            UNAVAILABLE,
            TIMEOUT,
            "codex_command_missing",
            "codex_not_signed_in",
            "codex_credentials_unreadable",
            "codex_access_expired",
        ];
        if !home_issue.is_empty() && refinable.contains(&issue) {
            home_issue.to_string()
        } else {
            issue.to_string()
        }
    }

    /// Whether the Codex home filesystem cannot take new files (statvfs only).
    fn home_storage_full(environment: &Env, home: &Path) -> bool {
        let codex = environment
            .get("CODEX_HOME")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".codex"));
        for candidate in [home.to_path_buf(), codex] {
            let probe = if candidate.is_dir() {
                candidate.clone()
            } else {
                candidate
                    .parent()
                    .map(Path::to_path_buf)
                    .unwrap_or_default()
            };
            let Ok(path) = std::ffi::CString::new(probe.to_string_lossy().as_bytes()) else {
                continue;
            };
            // SAFETY: a zeroed statvfs is valid output storage; `path` is NUL-terminated.
            let mut info: libc::statvfs = unsafe { std::mem::zeroed() };
            if unsafe { libc::statvfs(path.as_ptr(), &mut info) } != 0 {
                continue;
            }
            if info.f_files as u64 != 0 && (info.f_favail as u64) < MIN_HOME_FREE_INODES {
                return true;
            }
            if info.f_blocks as u64 == 0 {
                continue;
            }
            if (info.f_bavail as u64).saturating_mul(info.f_frsize as u64) < MIN_HOME_FREE_BYTES {
                return true;
            }
        }
        false
    }

    fn which_codex(environment: &Env) -> Option<String> {
        use std::os::unix::fs::PermissionsExt;
        let path = environment
            .get("PATH")
            .cloned()
            .unwrap_or_else(|| ":/bin:/usr/bin".into());
        path.split(':').find_map(|dir| {
            let candidate = Path::new(if dir.is_empty() { "." } else { dir }).join("codex");
            let info = std::fs::metadata(&candidate).ok()?;
            (info.is_file() && info.permissions().mode() & 0o111 != 0)
                .then(|| candidate.to_string_lossy().into_owned())
        })
    }

    /// The fixed launch command; user shell startup is trusted local
    /// configuration, never bridge input.
    fn launch(environment: &Env) -> Option<Vec<String>> {
        if let Some(shell) = native_shell(environment) {
            let fixed = if environment.get("CODEX_HOME").is_some_and(|v| !v.is_empty()) {
                "exec env CODEX_HOME=\"$TOFU_CODEX_NATIVE_HOME\" codex app-server --listen stdio://"
            } else {
                "exec codex app-server --listen stdio://"
            };
            return Some(vec![shell, "-ic".into(), fixed.into()]);
        }
        which_codex(environment).map(|exe| {
            vec![
                exe,
                "app-server".into(),
                "--listen".into(),
                "stdio://".into(),
            ]
        })
    }

    /// Non-secret stamps of the files that shape Codex's policy and launch.
    fn source_revision(
        environment: &Env,
        home: &Path,
        codex_home: &str,
    ) -> Result<Revision, String> {
        use std::os::unix::fs::MetadataExt;
        let codex_root = PathBuf::from(codex_home);
        let shell_root = environment
            .get("ZDOTDIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.to_path_buf());
        let xdg_root = environment
            .get("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".config"));
        let mut paths: Vec<PathBuf> = [".profile", ".bashrc", ".bash_profile", ".bash_login"]
            .iter()
            .map(|n| home.join(n))
            .collect();
        paths.extend(
            [".zshenv", ".zprofile", ".zshrc", ".zlogin"]
                .iter()
                .map(|n| shell_root.join(n)),
        );
        paths.extend([
            xdg_root.join("fish").join("config.fish"),
            codex_root.join("config.toml"),
            codex_root.join("requirements.toml"),
            PathBuf::from("/etc/codex/requirements.toml"),
            PathBuf::from("/etc/codex/managed_config.toml"),
            PathBuf::from("/etc/codex/config.toml"),
            PathBuf::from("/etc/profile"),
            PathBuf::from("/etc/bash.bashrc"),
            PathBuf::from("/Library/Managed Preferences/com.openai.codex.plist"),
            home.join("Library/Preferences/com.openai.codex.plist"),
        ]);
        if let Some(shell) = native_shell(environment) {
            paths.push(PathBuf::from(shell));
        }
        if let Some(exe) = which_codex(environment) {
            paths.push(PathBuf::from(exe));
        }
        let stamp = |m: &std::fs::Metadata| {
            format!(
                "{}:{}:{}:{}:{}:{}:{}:{}:{}",
                m.dev(),
                m.ino(),
                m.mtime(),
                m.mtime_nsec(),
                m.ctime(),
                m.ctime_nsec(),
                m.size(),
                m.mode(),
                m.uid()
            )
        };
        let mut stamps = Vec::with_capacity(paths.len());
        for path in paths {
            match std::fs::symlink_metadata(&path) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    stamps.push(format!("{}=", path.display()))
                }
                Err(_) => return Err(UNAVAILABLE.into()),
                Ok(link) => {
                    let target = if link.file_type().is_symlink() {
                        std::fs::metadata(&path).map_err(|_| UNAVAILABLE.to_string())?
                    } else {
                        link.clone()
                    };
                    stamps.push(format!(
                        "{}={}|{}",
                        path.display(),
                        stamp(&link),
                        stamp(&target)
                    ));
                }
            }
        }
        Ok(stamps)
    }

    // ── learned cold-start budget ───────────────────────────────────────────

    fn startup_seconds(provenance: &str) -> f64 {
        let exchange = EXCHANGE.as_secs_f64();
        let learned = lock(&STARTUP)
            .budgets
            .iter()
            .find(|(p, _)| p == provenance)
            .map_or(exchange, |(_, b)| *b);
        learned.clamp(exchange, STARTUP_CEILING.as_secs_f64())
    }

    fn flight_budget(provenance: &str) -> Duration {
        Duration::from_secs_f64(startup_seconds(provenance)) + EXCHANGE
    }

    /// The next cold budget is twice a completed startup or an exhausted
    /// budget; a faster start decays the previous budget by at most a quarter.
    fn learn_startup(provenance: &str, observed: f64) {
        let exchange = EXCHANGE.as_secs_f64();
        let ceiling = STARTUP_CEILING.as_secs_f64();
        let previous = startup_seconds(provenance);
        let learned = (2.0 * observed)
            .max(STARTUP_BUDGET_DECAY * previous)
            .clamp(exchange, ceiling);
        let mut state = lock(&STARTUP);
        state.budgets.retain(|(p, _)| p != provenance);
        if learned > exchange {
            state.budgets.push((provenance.to_string(), learned));
            if state.budgets.len() > STARTUP_BUDGET_SIGNATURES {
                state.budgets.remove(0);
            }
        }
    }

    fn cold_starts_refused(provenance: &str) -> bool {
        let mut state = lock(&STARTUP);
        let now = Instant::now();
        state.exhausted.retain(|(_, until)| *until > now);
        state.exhausted.iter().any(|(p, _)| p == provenance)
    }

    fn refuse_cold_starts(provenance: &str) {
        let mut state = lock(&STARTUP);
        state.exhausted.retain(|(p, _)| p != provenance);
        state
            .exhausted
            .push((provenance.to_string(), Instant::now() + STARTUP_EXHAUSTED));
        if state.exhausted.len() > STARTUP_BUDGET_SIGNATURES {
            state.exhausted.remove(0);
        }
        log::warn(&format!(
            "Codex native startup exhausted its ceiling ceiling_ms={}; cold starts refused for {} s",
            STARTUP_CEILING.as_millis(),
            STARTUP_EXHAUSTED.as_secs()
        ));
    }

    // ── one app-server session ──────────────────────────────────────────────

    struct Session {
        child: Option<Child>,
        stdin: Option<ChildStdin>,
        inbox: Receiver<Value>,
        failed: Arc<std::sync::atomic::AtomicBool>,
        reader_done: Receiver<()>,
        environment: Env,
        home: PathBuf,
        wire_id: u64,
    }

    impl Session {
        fn start(command: &[String], environment: &Env, home: &Path) -> Result<Session, String> {
            use std::os::unix::process::CommandExt;
            let mut launcher = Command::new(&command[0]);
            launcher
                .args(&command[1..])
                .current_dir(home)
                .env_clear()
                .envs(child_environment(environment, home))
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null());
            // SAFETY: setsid is async-signal-safe; the new session keeps an
            // interactive shell off any terminal and owns the whole group.
            unsafe {
                launcher.pre_exec(|| {
                    if libc::setsid() < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
            let mut child = launcher.spawn().map_err(|e| {
                if e.raw_os_error() == Some(libc::ENOSPC) {
                    HOME_STORAGE_FULL.to_string()
                } else if e.kind() == std::io::ErrorKind::NotFound {
                    "codex_command_missing".to_string()
                } else {
                    UNAVAILABLE.to_string()
                }
            })?;
            let stdout = child.stdout.take();
            let stdin = child.stdin.take();
            let (send, inbox) = mpsc::sync_channel(INBOX_MESSAGES);
            let (done, reader_done) = mpsc::channel();
            let failed = Arc::new(std::sync::atomic::AtomicBool::new(false));
            let flag = failed.clone();
            let spawned = std::thread::Builder::new()
                .name("codex-login-protocol".into())
                .spawn(move || {
                    if let Some(stdout) = stdout {
                        receive(stdout, &send, &flag);
                    }
                    let _ = done.send(());
                });
            let mut session = Session {
                child: Some(child),
                stdin,
                inbox,
                failed,
                reader_done,
                environment: environment.clone(),
                home: home.to_path_buf(),
                wire_id: 0,
            };
            if spawned.is_err() {
                session.stop();
                return Err(UNAVAILABLE.into());
            }
            Ok(session)
        }

        fn send(&mut self, mut message: Value) -> Result<u64, String> {
            let mut id = 0;
            if message.get("id").is_some() {
                self.wire_id += 1;
                id = self.wire_id;
                message["id"] = id.into();
            }
            let mut line = serde_json::to_vec(&message).unwrap_or_default();
            line.push(b'\n');
            let stdin = self.stdin.as_mut().ok_or(UNAVAILABLE)?;
            if stdin.write_all(&line).and_then(|_| stdin.flush()).is_err() {
                return Err(if home_storage_full(&self.environment, &self.home) {
                    HOME_STORAGE_FULL.into()
                } else {
                    UNAVAILABLE.into()
                });
            }
            Ok(id)
        }

        fn result(&mut self, id: u64, deadline: Instant) -> Result<Map<String, Value>, String> {
            use std::sync::atomic::Ordering;
            loop {
                let now = Instant::now();
                if now >= deadline {
                    return Err(TIMEOUT.into());
                }
                if self.failed.load(Ordering::SeqCst) {
                    return Err("codex_command_invalid_response".into());
                }
                match self.inbox.recv_timeout(POLL.min(deadline - now)) {
                    Ok(message) => {
                        if message.get("id").and_then(Value::as_u64) != Some(id) {
                            continue;
                        }
                        return match message.get("result") {
                            Some(Value::Object(result)) if message.get("error").is_none() => {
                                Ok(result.clone())
                            }
                            _ => Err(UNAVAILABLE.into()),
                        };
                    }
                    Err(RecvTimeoutError::Timeout) | Err(RecvTimeoutError::Disconnected) => {
                        let exited = self
                            .child
                            .as_mut()
                            .and_then(|c| c.try_wait().ok().flatten());
                        if let Some(status) = exited {
                            if self.failed.load(Ordering::SeqCst) {
                                return Err("codex_command_invalid_response".into());
                            }
                            if status.code() == Some(127) {
                                return Err("codex_command_missing".into());
                            }
                            if home_storage_full(&self.environment, &self.home) {
                                return Err(HOME_STORAGE_FULL.into());
                            }
                            return Err(UNAVAILABLE.into());
                        }
                        if matches!(
                            self.inbox.recv_timeout(Duration::ZERO),
                            Err(RecvTimeoutError::Disconnected)
                        ) {
                            // Output closed but the child lingers: wait out one poll.
                            std::thread::sleep(
                                POLL.min(deadline.saturating_duration_since(Instant::now())),
                            );
                        }
                    }
                }
            }
        }

        /// Terminate and reap the owned process group; join the reader.
        fn stop(&mut self) {
            self.stdin.take();
            let Some(mut child) = self.child.take() else {
                return;
            };
            let group = child.id() as libc::pid_t;
            // SAFETY: signals to the group this session leads.
            unsafe { libc::killpg(group, libc::SIGTERM) };
            let (reaped, waited) = mpsc::channel();
            std::thread::spawn(move || {
                let _ = child.wait();
                let _ = reaped.send(());
            });
            if waited.recv_timeout(Duration::from_millis(500)).is_err() {
                unsafe { libc::killpg(group, libc::SIGKILL) };
                let _ = waited.recv();
            }
            if self
                .reader_done
                .recv_timeout(Duration::from_millis(200))
                .is_err()
            {
                // An exited launcher can still have children holding stdout open.
                unsafe { libc::killpg(group, libc::SIGKILL) };
                if self
                    .reader_done
                    .recv_timeout(Duration::from_secs(1))
                    .is_err()
                {
                    log::warn("Codex native protocol reader did not finish after teardown");
                }
            }
        }
    }

    impl Drop for Session {
        fn drop(&mut self) {
            self.stop();
        }
    }

    fn receive(
        stdout: impl Read,
        inbox: &mpsc::SyncSender<Value>,
        failed: &std::sync::atomic::AtomicBool,
    ) {
        use std::sync::atomic::Ordering;
        let mut reader = BufReader::new(stdout);
        let mut total = 0usize;
        loop {
            let mut line = Vec::new();
            match (&mut reader)
                .take(MAX_LINE_BYTES as u64 + 1)
                .read_until(b'\n', &mut line)
            {
                Ok(0) => return,
                Ok(n) => {
                    total += n;
                    if n > MAX_LINE_BYTES || total > MAX_OUTPUT_BYTES {
                        failed.store(true, Ordering::SeqCst);
                        return;
                    }
                }
                Err(_) => {
                    failed.store(true, Ordering::SeqCst);
                    return;
                }
            }
            // Shell startup may print bounded non-protocol text.
            let Ok(message) = serde_json::from_slice::<Value>(&line) else {
                continue;
            };
            if message.is_object() && inbox.try_send(message).is_err() {
                failed.store(true, Ordering::SeqCst);
                return;
            }
        }
    }

    /// Spawn, initialize, read the policy (and renew when `renew_root` names
    /// the already checked home); always tears the app-server down.
    fn exchange(
        environment: &Env,
        home: &Path,
        provenance: &str,
        renew_root: Option<&str>,
        cap: Instant,
    ) -> Result<Context, String> {
        let started = Instant::now();
        let learned = startup_seconds(provenance);
        let budget = Duration::from_secs_f64(learned).min(cap.saturating_duration_since(started));
        let home_issue = explicit_home_issue(environment, home);
        if !home_issue.is_empty() {
            return Err(home_issue.into());
        }
        let command = launch(environment).ok_or("codex_command_missing")?;
        if home_storage_full(environment, home) {
            return Err(HOME_STORAGE_FULL.into());
        }
        if cold_starts_refused(provenance) {
            return Err(TIMEOUT.into());
        }
        let mut session = Session::start(&command, environment, home)?;
        let id = session.send(json!({"id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "tofu_codex_login", "version": "1.0.0"}}}))?;
        let hello = match session.result(id, started + budget) {
            Ok(hello) => hello,
            Err(issue) => {
                // A budget truncated by the caller's deadline is no startup evidence.
                if issue == TIMEOUT && budget.as_secs_f64() >= learned {
                    learn_startup(provenance, budget.as_secs_f64());
                    if learned >= STARTUP_CEILING.as_secs_f64() {
                        refuse_cold_starts(provenance);
                    }
                    log::warn(&format!(
                        "Codex native credential probe failed phase=initialize issue={TIMEOUT} budget_ms={}",
                        budget.as_millis()
                    ));
                }
                return Err(issue);
            }
        };
        let initialized = Instant::now();
        learn_startup(provenance, (initialized - started).as_secs_f64());
        let deadline = (initialized + EXCHANGE).min(cap);
        let root = match hello.get("codexHome").and_then(Value::as_str) {
            Some(root) if root.len() <= 4096 && Path::new(root).is_absolute() => root.to_string(),
            _ => return Err("codex_command_unsupported".into()),
        };
        session.send(json!({"method": "initialized"}))?;
        let policy_revision = source_revision(environment, home, &root)?;
        let id = session
            .send(json!({"id": 2, "method": "config/read", "params": {"includeLayers": false}}))?;
        let Some(Value::Object(config)) = session.result(id, deadline)?.remove("config") else {
            return Err("codex_command_invalid_response".into());
        };
        let id =
            session.send(json!({"id": 3, "method": "configRequirements/read", "params": {}}))?;
        let requirements = match session.result(id, deadline)?.remove("requirements") {
            Some(Value::Object(map)) => map,
            Some(v) if super::super::egress::falsy(&v) => Map::new(),
            None => Map::new(),
            Some(_) => return Err("codex_command_invalid_response".into()),
        };
        let mut settings = Map::new();
        for key in ["forced_login_method", "forced_chatgpt_workspace_id"] {
            if let Some(value) = config.get(key) {
                settings.insert(key.into(), value.clone());
            }
        }
        if let Some(store) = requirements
            .get("cliAuthCredentialsStore")
            .filter(|v| !v.is_null())
        {
            settings.insert("cli_auth_credentials_store".into(), store.clone());
        }
        if let Some(methods) = requirements
            .get("allowedLoginMethods")
            .filter(|v| !v.is_null())
        {
            let Some(methods) = methods
                .as_array()
                .filter(|m| m.len() <= 16 && m.iter().all(Value::is_string))
            else {
                return Err("codex_command_invalid_response".into());
            };
            if !methods.iter().any(|m| m == "chatgpt") {
                return Err("login_method_restricted".into());
            }
        }
        if requirements
            .get("chatgptBaseUrl")
            .is_some_and(|u| !u.is_null() && u != "https://chatgpt.com")
        {
            return Err("login_method_restricted".into());
        }
        if source_revision(environment, home, &root)? != policy_revision {
            return Err("codex_source_changed".into());
        }
        if let Some(expected_root) = renew_root {
            if Path::new(&root) != Path::new(expected_root) {
                return Err("codex_source_changed".into());
            }
            let id = session.send(
                json!({"id": 4, "method": "account/read", "params": {"refreshToken": true}}),
            )?;
            let account = session.result(id, deadline)?.remove("account");
            if !account
                .as_ref()
                .and_then(Value::as_object)
                .is_some_and(|a| a.get("type") == Some(&json!("chatgpt")))
            {
                return Err("not_chatgpt_login".into());
            }
        }
        Ok(Context {
            codex_home: root,
            settings,
        })
    }

    /// A final source check before an answer leaves: a vanished or
    /// unverifiable explicit home refines the answer and drops cached metadata.
    fn home_checked(
        answer: Result<Context, String>,
        environment: &Env,
        home: &Path,
        signature: &str,
    ) -> Result<Context, String> {
        let fresh = explicit_home_issue(environment, home);
        if !unverifiable(fresh) {
            return answer;
        }
        let current = answer.err().unwrap_or_default();
        let mut verified = lock(&VERIFIED);
        if verified.as_ref().is_some_and(|v| v.signature == signature) {
            *verified = None;
        }
        Err(recheck_home_failure(&current, fresh))
    }

    fn reusable(
        environment: &Env,
        home: &Path,
        signature: &str,
        max_age: Duration,
    ) -> Option<Context> {
        let verified = lock(&VERIFIED);
        let entry = verified.as_ref()?;
        if entry.signature != signature || entry.at.elapsed() >= max_age {
            return None;
        }
        let matches = source_revision(environment, home, &entry.context.codex_home)
            .is_ok_and(|r| r == entry.revision);
        (matches && !unverifiable(explicit_home_issue(environment, home)))
            .then(|| entry.context.clone())
    }

    pub fn command_context(
        environment: &Env,
        home: &Path,
        flight_started: Instant,
    ) -> Result<Context, String> {
        let signature = environment_revision(environment, home);
        let home_issue = explicit_home_issue(environment, home);
        if unverifiable(home_issue) {
            return home_checked(Err(home_issue.into()), environment, home, &signature);
        }
        if let Some(context) = reusable(environment, home, &signature, CONTEXT_TTL) {
            return home_checked(Ok(context), environment, home, &signature);
        }
        let _flight = lock(&FLIGHT);
        // A flight that finished while this one waited may have published.
        if let Some(context) = reusable(environment, home, &signature, CONTEXT_TTL) {
            return home_checked(Ok(context), environment, home, &signature);
        }
        let deadline = flight_started + flight_budget(&signature);
        let mut answer = exchange(environment, home, &signature, None, deadline);
        if let Ok(context) = &answer {
            let previous = lock(&VERIFIED)
                .as_ref()
                .filter(|v| v.signature == signature)
                .map(|v| v.context.codex_home.clone());
            if previous.is_some_and(|p| p != context.codex_home) {
                answer = Err("codex_source_changed".into());
            }
        }
        let revision = match &answer {
            Ok(context) => match source_revision(environment, home, &context.codex_home) {
                Ok(revision) => Some(revision),
                Err(issue) => {
                    answer = Err(issue);
                    None
                }
            },
            Err(_) => None,
        };
        let fresh = explicit_home_issue(environment, home);
        let issue = recheck_home_failure(answer.as_ref().err().map_or("", String::as_str), fresh);
        match (answer, revision) {
            (Ok(context), Some(revision)) if issue.is_empty() => {
                *lock(&VERIFIED) = Some(Verified {
                    signature: signature.clone(),
                    at: Instant::now(),
                    context: context.clone(),
                    revision,
                });
                home_checked(Ok(context), environment, home, &signature)
            }
            _ => {
                if TRANSIENT.contains(&issue.as_str())
                    && issue != HOME_UNAVAILABLE
                    && !unverifiable(fresh)
                {
                    if let Some(context) = reusable(environment, home, &signature, CONTEXT_MAX_AGE)
                    {
                        log::warn(&format!(
                            "Codex native credential probe using bounded verified metadata issue={issue}"
                        ));
                        return home_checked(Ok(context), environment, home, &signature);
                    }
                } else {
                    *lock(&VERIFIED) = None;
                }
                home_checked(Err(issue), environment, home, &signature)
            }
        }
    }

    /// Renew only a previously checked account through native Codex; another
    /// reader may have renewed while this one waited, so re-read first.
    pub fn renew(
        context: &Context,
        account: &str,
        environment: &Env,
        home: &Path,
        flight_started: Instant,
    ) -> Result<Context, String> {
        let signature = environment_revision(environment, home);
        let deadline = flight_started + flight_budget(&signature);
        let _flight = lock(&FLIGHT);
        let home_issue = explicit_home_issue(environment, home);
        if unverifiable(home_issue) {
            return home_checked(Err(home_issue.into()), environment, home, &signature);
        }
        let current = super::read_file_login(
            account,
            Some(&context.codex_home),
            home,
            Some(&context.settings),
            true,
        );
        let current = if current.get("access_token").is_some() {
            Ok(context.clone())
        } else {
            let code = super::issue_of(&current);
            Err(if code.is_empty() {
                "codex_credentials_unreadable"
            } else {
                code
            }
            .to_string())
        };
        let current = home_checked(current, environment, home, &signature);
        if current.as_ref().err().map(String::as_str) != Some("codex_access_expired") {
            return current;
        }
        if Instant::now() >= deadline {
            return home_checked(Err(TIMEOUT.into()), environment, home, &signature);
        }
        let renewed = exchange(
            environment,
            home,
            &signature,
            Some(&context.codex_home),
            deadline,
        );
        let issue = recheck_home_failure(
            renewed.as_ref().err().map_or("", String::as_str),
            explicit_home_issue(environment, home),
        );
        let answer = if issue.is_empty() {
            renewed
        } else {
            Err(issue)
        };
        home_checked(answer, environment, home, &signature)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn home_failures_refine_only_unqualified_outcomes() {
            assert_eq!(recheck_home_failure("", HOME_UNAVAILABLE), HOME_UNAVAILABLE);
            assert_eq!(
                recheck_home_failure(TIMEOUT, HOME_UNAVAILABLE),
                HOME_UNAVAILABLE
            );
            assert_eq!(
                recheck_home_failure("login_method_restricted", HOME_UNAVAILABLE),
                "login_method_restricted"
            );
            assert_eq!(recheck_home_failure(TIMEOUT, ""), TIMEOUT);
        }

        #[test]
        fn launcher_children_never_inherit_api_identities() {
            let env: Env = [
                ("OPENAI_API_KEY", "k"),
                ("OPENAI_IDENTITY_TOKEN", "t"),
                ("CODEX_HOME", "/c"),
                ("PATH", "/bin"),
            ]
            .into_iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
            let child = child_environment(&env, Path::new("/h"));
            assert!(
                !child.contains_key("OPENAI_API_KEY")
                    && !child.contains_key("OPENAI_IDENTITY_TOKEN")
            );
            assert_eq!(child["TOFU_CODEX_NATIVE_HOME"], "/c");
            assert_eq!(child["HOME"], "/h");
        }

        #[test]
        fn a_missing_explicit_home_is_unavailable() {
            let dir = tempfile::tempdir().unwrap();
            let mut env = Env::new();
            env.insert(
                "CODEX_HOME".into(),
                dir.path().join("gone").to_string_lossy().into(),
            );
            assert_eq!(explicit_home_issue(&env, dir.path()), HOME_UNAVAILABLE);
            env.insert("CODEX_HOME".into(), dir.path().to_string_lossy().into());
            assert_eq!(explicit_home_issue(&env, dir.path()), "");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;

    #[test]
    fn params_are_validated_like_the_python_node() {
        let invalid = json!({"error": "invalid_codex_login_request"});
        assert_eq!(read(&json!([])), invalid);
        assert_eq!(read(&json!({"path": "/etc/passwd"})), invalid);
        assert_eq!(read(&json!({"account_id": 5})), invalid);
        assert_eq!(read(&json!({"account_id": "a".repeat(257)})), invalid);
        assert_eq!(read(&json!({"account_id": "", "x": 1})), invalid);
    }

    #[test]
    fn sensitive_names_follow_the_child_environment_contract() {
        for name in [
            "GITHUB_TOKEN",
            "LLM_API_KEY",
            "x_auth",
            "PGPASSWORD",
            "TOKEN",
        ] {
            assert!(sensitive_env(name), "{name}");
        }
        for name in ["PATH", "HOME", "CODEX_HOME", "AUTHOR", "TOKENIZER"] {
            assert!(!sensitive_env(name), "{name}");
        }
    }

    fn jwt(claims: Value) -> String {
        let payload = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(claims.to_string());
        format!("header.{payload}.signature")
    }

    fn write_auth(root: &Path, account: &str, exp: f64) {
        let auth = json!({"auth_mode": "chatgpt", "tokens": {
            "access_token": jwt(json!({"exp": exp, "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": "plus"}})),
            "id_token": jwt(json!({"email": "a@example.com"})),
            "refresh_token": "never-copy-refresh", "account_id": account}});
        let path = root.join("auth.json");
        fs::write(&path, auth.to_string()).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        }
    }

    #[test]
    fn the_file_seam_returns_an_access_only_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_string_lossy().into_owned();
        let later = crate::util::unix_now() + 3600.0;
        write_auth(dir.path(), "account-a", later);
        let snapshot = read_file_login("", Some(&root), dir.path(), None, false);
        assert_eq!(snapshot["account_id"], "account-a");
        assert_eq!(snapshot["email"], "a@example.com");
        assert_eq!(snapshot["plan_type"], "plus");
        assert_eq!(snapshot["credential_source"], "local_codex");
        assert!(!snapshot.contains_key("refresh_token"));
        let other = read_file_login("account-b", Some(&root), dir.path(), None, false);
        assert_eq!(issue_of(&other), "account_changed");
        write_auth(dir.path(), "account-a", 10.0);
        let expired = read_file_login("", Some(&root), dir.path(), None, false);
        assert_eq!(issue_of(&expired), "codex_access_expired");
        assert_eq!(expired["account_id"], "account-a");
        fs::write(
            dir.path().join("config.toml"),
            "cli_auth_credentials_store = \"keyring\"\n",
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                dir.path().join("config.toml"),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
        }
        let keyring = read_file_login("", Some(&root), dir.path(), None, false);
        assert_eq!(issue_of(&keyring), "credential_store_unsupported");
        let missing = read_file_login(
            "",
            Some(&dir.path().join("none").to_string_lossy()),
            dir.path(),
            None,
            false,
        );
        assert_eq!(issue_of(&missing), "codex_not_signed_in");
        assert_eq!(
            issue_of(&read_file_login(
                "",
                Some("relative"),
                dir.path(),
                None,
                false
            )),
            "invalid_codex_home"
        );
    }

    #[cfg(unix)]
    #[test]
    fn writable_or_symlinked_credentials_are_unreadable() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_string_lossy().into_owned();
        write_auth(dir.path(), "account-a", crate::util::unix_now() + 3600.0);
        fs::set_permissions(
            dir.path().join("auth.json"),
            fs::Permissions::from_mode(0o666),
        )
        .unwrap();
        let open = read_file_login("", Some(&root), dir.path(), None, false);
        assert_eq!(issue_of(&open), "codex_credentials_unreadable");
        let real = tempfile::tempdir().unwrap();
        write_auth(real.path(), "account-a", crate::util::unix_now() + 3600.0);
        fs::remove_file(dir.path().join("auth.json")).unwrap();
        std::os::unix::fs::symlink(real.path().join("auth.json"), dir.path().join("auth.json"))
            .unwrap();
        // The file seam never follows a symlinked credential; a native-verified home does.
        let linked = read_file_login("", Some(&root), dir.path(), None, false);
        assert_eq!(issue_of(&linked), "codex_credentials_unreadable");
        let verified = read_file_login("", Some(&root), dir.path(), Some(&Map::new()), true);
        assert_eq!(verified["account_id"], "account-a");
    }
}
