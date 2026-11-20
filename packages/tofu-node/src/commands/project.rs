//! Project (share-root) commands: the port of `lib/desktop_agent/_project.py`.
//!
//! Remote-project contract (docs/modules/remote_execution.md):
//!
//! * Path validation lives on the node: commands address files as `root` (a
//!   name from this node's own declared `share_roots`) plus a root-RELATIVE
//!   path. The real path of the target must stay inside the root: `..`,
//!   absolute paths, sibling-prefix attacks and symlink escapes are refused.
//! * Snapshot-before-write: every write to an existing file first copies it to
//!   `<root>/.tofu/file-history/<md5(abspath)>/<epoch_ns>`.
//! * Freshness gate: a write to an EXISTING file requires a read stamp from
//!   this node process and is refused when the file changed on disk since that
//!   read. A fresh `project_read_files` re-arms the stamp; the node's own
//!   successful write re-arms it too.
//!
//! Listings and searches port the `lib/project_mod` read tools the Python
//! node imports (`IGNORE_DIRS`, root `.gitignore`/`.ignore` rules, the
//! ripgrep regex and glob dialects, the same result text). The desktop file
//! reads (`files.rs`) resolve through [`shared_path`].

use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_READ_CHARS: usize = 400_000;
const MAX_BINARY_BYTES: usize = 8_000_000;
const BINARY_EXTS: &[&str] = &[".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf"];
// `read_tools._scan_directory_entries` bounds.
const LIST_DIR_SCAN_LIMIT: usize = 10_000;
const LIST_DIR_ENTRY_LIMIT: usize = 1_000;
// `cmd_project_edit_file` processes at most this many edits of one batch.
const EDIT_BATCH_LIMIT: usize = 30;
// `read_tools.tool_grep` / `_format_grep_output` presentation bounds.
const GREP_DEFAULT_RESULTS: i64 = 50;
const GREP_MAX_RESULTS: i64 = 500;
const GREP_MAX_CONTEXT: i64 = 10;
const GREP_MAX_LINE_CHARS: usize = 300;
const GREP_MAX_CHARS: usize = 12_000;
const GREP_MAX_CHARS_WITH_CONTEXT: usize = 20_000;
const WITHHELD_LIST_MAX: usize = 8;
// `read_tools.tool_find_files` / `_python_find` bounds.
const FIND_DEFAULT_RESULTS: i64 = 100;
const FIND_MAX_RESULTS: i64 = 500;
const FIND_ERROR_NOTICES: usize = 8;
// Symlink hops before a path is taken literally (Linux MAXSYMLINKS).
const SYMLINK_HOPS: u32 = 40;

/// `lib/project_mod/config.IGNORE_DIRS`.
const IGNORE_DIRS: &[&str] = &[
    ".git",
    ".tofu",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".idea",
    ".vscode",
    ".next",
    "dist",
    "build",
    ".cache",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    "vendor",
    ".bundle",
    "coverage",
    ".nyc_output",
    ".eggs",
    ".sass-cache",
    "bower_components",
    ".parcel-cache",
    ".turbo",
    ".vercel",
    ".output",
    ".nuxt",
    ".svelte-kit",
    ".angular",
    "obj",
    "bin",
    ".project_indexes",
    "logs",
    "data",
    ".project_sessions",
    "swebench_workdir",
    "abtest_workdir",
    ".eval-runs",
    "eval-runs",
    "evaluation_results",
    "sb-cli-reports",
    "jobs",
    "trials",
    "overleaf_cache",
    ".ruff_cache",
    "uploads",
    ".migrate_backup",
];
/// `tree_index_coverage.IGNORE_RULE_FILES`.
const IGNORE_RULE_FILES: &[&str] = &[".gitignore", ".ignore"];

type Outcome = Result<Value, String>;

fn guarded(outcome: Outcome) -> Value {
    outcome.unwrap_or_else(|error| json!({ "error": error }))
}

// ── Python value semantics ──────────────────────────────────────────

/// Python truthiness of a JSON value.
pub(crate) fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Python `repr()` of a string.
pub(crate) fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python `repr()` of a JSON param (`None` when absent).
pub(crate) fn py_repr_value(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => py_repr(s),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::Bool(false)) => "False".into(),
        Some(other) => other.to_string(),
    }
}

/// Python `str()` of a JSON param.
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

/// Python `int(value)` for a JSON param.
fn py_int(value: &Value) -> Result<i64, String> {
    match value {
        Value::Bool(b) => Ok(*b as i64),
        Value::Number(n) => n
            .as_i64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite())
                    .map(|f| f.trunc() as i64)
            })
            .ok_or_else(|| format!("invalid integer: {n}")),
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| format!("invalid literal for int() with base 10: {}", py_repr(s))),
        other => Err(format!(
            "int() argument must be a string or a number, not {other}"
        )),
    }
}

/// `{n:,}` of Python.
pub(crate) fn thousands(n: i128) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 {
        format!("-{out}")
    } else {
        out
    }
}

// ── Roots & path validation ─────────────────────────────────────────

pub(crate) struct Root {
    pub name: String,
    pub path: PathBuf,
}

/// This node's OWN declared share roots (config `share_roots`), real paths.
pub(crate) fn declared_roots() -> Vec<Root> {
    crate::config::share_roots()
        .iter()
        .filter_map(|row| {
            let row = row.as_object()?;
            let path = row.get("path").filter(|p| truthy(Some(p)))?;
            let name = row
                .get("name")
                .filter(|n| truthy(Some(n)))
                .map(py_str)
                .unwrap_or_default();
            Some(Root {
                name,
                path: realpath(&crate::config::expand_user(&py_str(path))),
            })
        })
        .collect()
}

/// Python `os.path.realpath` (non-strict): symlinks resolved, a missing tail
/// kept literally, `..` applied to the resolved prefix.
pub(crate) fn realpath(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let mut resolved = PathBuf::new();
    let mut hops = SYMLINK_HOPS;
    resolve_into(&mut resolved, &absolute, &mut hops);
    resolved
}

fn resolve_into(resolved: &mut PathBuf, path: &Path, hops: &mut u32) {
    if path.has_root() {
        resolved.clear();
    }
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => resolved.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                resolved.pop();
            }
            Component::Normal(name) => {
                let candidate = resolved.join(name);
                match fs::read_link(&candidate) {
                    Ok(target) if *hops > 0 => {
                        *hops -= 1;
                        resolve_into(resolved, &target, hops);
                    }
                    _ => *resolved = candidate,
                }
            }
        }
    }
}

/// True when `target` is `root` itself or lives beneath it (whole components).
pub(crate) fn is_within(root: &Path, target: &Path) -> bool {
    target.starts_with(root)
}

/// Lexical `os.path.normpath` of an absolute path.
fn normpath(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn rel_text(value: Option<&Value>) -> String {
    let text = value.and_then(Value::as_str).unwrap_or("").trim();
    if text.is_empty() {
        ".".into()
    } else {
        text.into()
    }
}

fn is_absolute_spelling(rel: &str) -> bool {
    Path::new(rel).is_absolute()
        || rel.starts_with('/')
        || rel.starts_with('\\')
        || rel.chars().nth(1) == Some(':')
}

/// Resolve (root name, root-relative path) to (root real path, target real path).
fn resolve_in(
    roots: &[Root],
    root_name: Option<&Value>,
    rel: Option<&Value>,
) -> Result<(PathBuf, PathBuf), String> {
    if roots.is_empty() {
        return Err("no share_roots declared in the agent config — project commands are disabled on this machine".into());
    }
    let empty = Value::String(String::new());
    let root_name = root_name.unwrap_or(&empty);
    let Some(root) = roots
        .iter()
        .find(|r| root_name.as_str() == Some(r.name.as_str()))
    else {
        let names: Vec<String> = roots
            .iter()
            .map(|r| {
                if r.name.is_empty() {
                    r.path.display().to_string()
                } else {
                    r.name.clone()
                }
            })
            .collect();
        return Err(format!(
            "unknown share root {} (declared: {})",
            py_repr_value(Some(root_name)),
            names.join(", ")
        ));
    };
    let rel = rel_text(rel);
    if is_absolute_spelling(&rel) {
        return Err(format!(
            "path must be root-relative, got absolute: {}",
            py_repr(&rel)
        ));
    }
    let target = realpath(&root.path.join(&rel));
    if !is_within(&root.path, &target) {
        return Err(format!(
            "path escapes share root {}: {}",
            py_repr_value(Some(root_name)),
            py_repr(&rel)
        ));
    }
    Ok((root.path.clone(), target))
}

#[cfg(test)]
thread_local! {
    static TEST_ROOTS: std::cell::RefCell<Option<Vec<(String, PathBuf)>>> = const { std::cell::RefCell::new(None) };
}

/// Tests declare their share roots per thread instead of through the config file.
#[cfg(test)]
pub(crate) fn set_test_roots(roots: Option<Vec<(String, PathBuf)>>) {
    TEST_ROOTS.with(|cell| *cell.borrow_mut() = roots);
}

fn current_roots() -> Vec<Root> {
    #[cfg(test)]
    if let Some(roots) = TEST_ROOTS.with(|cell| cell.borrow().clone()) {
        return roots
            .into_iter()
            .map(|(name, path)| Root {
                name,
                path: realpath(&path),
            })
            .collect();
    }
    declared_roots()
}

pub(crate) fn resolve(params: &Value, rel: Option<&Value>) -> Result<(PathBuf, PathBuf), String> {
    resolve_in(&current_roots(), params.get("root"), rel)
}

/// Real path of a local path that lies inside a declared share root.
///
/// The desktop file reads resolve through this on every permission tier, so
/// `..`, `~`, absolute paths and symlinks cannot reach outside the shared
/// folders; with no share roots, local file reads are disabled.
pub(crate) fn shared_path(raw: Option<&Value>) -> Result<PathBuf, String> {
    shared_path_in(&current_roots(), raw)
}

fn shared_path_in(roots: &[Root], raw: Option<&Value>) -> Result<PathBuf, String> {
    if roots.is_empty() {
        return Err("no shared folders are declared in the agent config (share_roots) — local file reads are disabled on this machine".into());
    }
    let path = raw
        .filter(|v| truthy(Some(v)))
        .map(py_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    let target = (!path.is_empty()).then(|| realpath(&crate::config::expand_user(&path)));
    match target {
        Some(target) if roots.iter().any(|r| is_within(&r.path, &target)) => Ok(target),
        _ => {
            let shared: Vec<String> = roots.iter().map(|r| r.path.display().to_string()).collect();
            Err(format!(
                "path is outside the shared folders: {} (shared folders: {})",
                py_repr(&path),
                shared.join(", ")
            ))
        }
    }
}

// ── Freshness gate, snapshots, atomic writes ────────────────────────

type Stamp = (i128, u64);

fn freshness() -> &'static Mutex<HashMap<PathBuf, Stamp>> {
    static TOKENS: OnceLock<Mutex<HashMap<PathBuf, Stamp>>> = OnceLock::new();
    TOKENS.get_or_init(Default::default)
}

fn system_ns(time: SystemTime) -> i128 {
    match time.duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_nanos() as i128,
        Err(e) => -(e.duration().as_nanos() as i128),
    }
}

fn stamp_of(path: &Path) -> Option<Stamp> {
    let meta = fs::metadata(path).ok()?;
    Some((meta.modified().map(system_ns).unwrap_or(0), meta.len()))
}

fn stamp_read(path: &Path) {
    if let Some(stamp) = stamp_of(path) {
        freshness()
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(path.to_path_buf(), stamp);
    }
}

/// Freshness gate + read-before-edit: an existing file needs a valid stamp.
fn check_write_allowed(path: &Path) -> Result<(), String> {
    if !path.exists() {
        return Ok(());
    }
    let token = freshness()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get(path)
        .copied();
    let Some(token) = token else {
        return Err("read-before-write: read the file with project_read_files first (this agent session has no record of it)".into());
    };
    let current = stamp_of(path).ok_or_else(|| format!("cannot stat {}", path.display()))?;
    if current != token {
        return Err("file changed on disk since last read — re-read it with project_read_files to re-arm the freshness token".into());
    }
    Ok(())
}

/// Copy an existing file into the root's file-history before mutating it.
fn snapshot(root: &Path, path: &Path) -> std::io::Result<Option<PathBuf>> {
    if !path.is_file() {
        return Ok(None);
    }
    let digest = md5_hex(path.to_string_lossy().as_bytes());
    let dest_dir = root.join(".tofu").join("file-history").join(digest);
    fs::create_dir_all(&dest_dir)?;
    let dest = dest_dir.join(system_ns(SystemTime::now()).to_string());
    fs::copy(path, &dest)?;
    crate::log::info(&format!(
        "[Project] snapshot {} → {}",
        path.display(),
        dest.display()
    ));
    Ok(Some(dest))
}

/// Same-directory temp file + rename (`_project._atomic_write`).
fn atomic_write(path: &Path, content: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut temp = path.as_os_str().to_owned();
    temp.push(format!(".tofu-tmp-{}", std::process::id()));
    let temp = PathBuf::from(temp);
    let result = fs::write(&temp, content.as_bytes()).and_then(|_| fs::rename(&temp, path));
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

fn io_error(error: std::io::Error) -> String {
    error.to_string()
}

// ── Optional write receipts (`lib/project_mod/receipts.py`) ─────────

fn wants_receipts(params: &Value) -> bool {
    params
        .get("file_receipts_version")
        .is_some_and(|v| (v.is_i64() || v.is_u64()) && v.as_i64() == Some(1))
}

fn receipt_path(value: Option<&Value>) -> Result<String, String> {
    let text = value
        .and_then(Value::as_str)
        .filter(|t| !t.is_empty() && t.chars().count() <= 1024);
    let Some(text) = text else {
        return Err("Invalid project file receipt path".into());
    };
    let unified = text.replace('\\', "/");
    let absolute = unified.starts_with('/');
    let parts: Vec<&str> = unified
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    let path = if absolute {
        format!("/{}", parts.join("/"))
    } else if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    };
    let drive =
        path.len() >= 2 && path.as_bytes()[0].is_ascii_alphabetic() && path.as_bytes()[1] == b':';
    if absolute || parts.contains(&"..") || drive || path == "." {
        return Err("Project file receipt must be root-relative".into());
    }
    Ok(path)
}

fn committed_file(path: &str, action: &str) -> Value {
    json!({"changeId": uuid::Uuid::new_v4().simple().to_string(), "path": path, "action": action})
}

fn write_result(params: &Value, output: Value, files: Vec<Value>) -> Value {
    if wants_receipts(params) {
        json!({"file_receipts_version": 1, "output": output, "file_changes": files})
    } else {
        output
    }
}

// ── Commands ────────────────────────────────────────────────────────

struct DirRow {
    name: String,
    is_dir: bool,
    is_file: bool,
    is_symlink: bool,
    size: Option<u64>,
    modified: Option<i64>,
}

fn mtime_secs(meta: &fs::Metadata) -> Option<i64> {
    meta.modified()
        .ok()
        .map(|t| system_ns(t).div_euclid(1_000_000_000) as i64)
}

/// `read_tools._scan_directory_entries`: a bounded, sorted directory snapshot.
fn scan_directory_entries(
    target: &Path,
    show_hidden: bool,
    include_directory_stat: bool,
    respect_project_ignores: bool,
    include_non_regular: bool,
) -> std::io::Result<(Vec<DirRow>, bool, usize)> {
    let mut rows = Vec::new();
    let mut scanned = 0;
    let mut truncated = false;
    for entry in fs::read_dir(target)? {
        if scanned >= LIST_DIR_SCAN_LIMIT {
            truncated = true;
            break;
        }
        scanned += 1;
        let Ok(entry) = entry else { continue };
        let name = entry.file_name().to_string_lossy().into_owned();
        if !show_hidden && name.starts_with('.') {
            continue;
        }
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        let (is_symlink, is_dir, is_file) = (kind.is_symlink(), kind.is_dir(), kind.is_file());
        if respect_project_ignores && is_dir && IGNORE_DIRS.contains(&name.as_str()) {
            continue;
        }
        if !is_dir && !is_file && !include_non_regular {
            continue;
        }
        let (mut size, mut modified) = (None, None);
        if is_file || include_directory_stat || include_non_regular {
            match fs::symlink_metadata(entry.path()) {
                Ok(meta) => {
                    size = (!is_dir).then_some(meta.len());
                    modified = mtime_secs(&meta);
                }
                Err(_) if is_file => continue,
                Err(_) => {}
            }
        }
        rows.push(DirRow {
            name,
            is_dir,
            is_file,
            is_symlink,
            size,
            modified,
        });
    }
    rows.sort_by(|a, b| {
        (!a.is_dir, a.name.to_lowercase(), &a.name).cmp(&(
            !b.is_dir,
            b.name.to_lowercase(),
            &b.name,
        ))
    });
    if rows.len() > LIST_DIR_ENTRY_LIMIT {
        rows.truncate(LIST_DIR_ENTRY_LIMIT);
        truncated = true;
    }
    Ok((rows, truncated, scanned))
}

fn param_or_dot(params: &Value, key: &str) -> Value {
    params
        .get(key)
        .filter(|v| truthy(Some(v)))
        .cloned()
        .unwrap_or_else(|| json!("."))
}

pub fn list_dir(params: &Value) -> Value {
    guarded((|| {
        let (_, target) = resolve(params, params.get("path"))?;
        if !target.is_dir() {
            return Err(format!(
                "not a directory: {}",
                py_repr_value(params.get("path"))
            ));
        }
        let shell = truthy(params.get("shell_compatible"));
        let (rows, truncated, scanned) = scan_directory_entries(
            &target,
            truthy(params.get("show_hidden")),
            !shell,
            !shell,
            shell,
        )
        .map_err(|e| {
            format!(
                "unable to list directory {}: {e}",
                py_repr_value(params.get("path"))
            )
        })?;
        let entries: Vec<Value> = rows
            .iter()
            .map(|row| {
                let kind = if row.is_dir {
                    "dir"
                } else if row.is_file {
                    "file"
                } else if row.is_symlink {
                    "symlink"
                } else {
                    "special"
                };
                json!({
                    "name": row.name,
                    "type": kind,
                    "size": row.size,
                    "modified": row.modified.map(|secs| {
                        let t = local_time(secs);
                        format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}", t.0, t.1, t.2, t.3, t.4, t.5)
                    }),
                })
            })
            .collect();
        Ok(
            json!({"path": param_or_dot(params, "path"), "entries": entries, "truncated": truncated, "scanned": scanned}),
        )
    })())
}

/// Python text-mode decoding: UTF-8 with replacement, universal newlines.
pub(crate) fn decode_text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .replace("\r\n", "\n")
        .replace('\r', "\n")
}

fn extension_lower(path: &Path) -> String {
    path.extension()
        .map(|e| format!(".{}", e.to_string_lossy().to_lowercase()))
        .unwrap_or_default()
}

pub fn read_files(params: &Value) -> Value {
    guarded((|| {
        let (_, target) = resolve(params, params.get("path"))?;
        if !target.is_file() {
            return Err(format!("not a file: {}", py_repr_value(params.get("path"))));
        }
        let path = params.get("path").cloned().unwrap_or(Value::Null);
        let ext = extension_lower(&target);
        let file = fs::File::open(&target).map_err(io_error)?;
        let size = file.metadata().map_err(io_error)?.len();
        if BINARY_EXTS.contains(&ext.as_str()) {
            let mut data = Vec::new();
            file.take(MAX_BINARY_BYTES as u64 + 1)
                .read_to_end(&mut data)
                .map_err(io_error)?;
            stamp_read(&target);
            let truncated = data.len() > MAX_BINARY_BYTES;
            data.truncate(MAX_BINARY_BYTES);
            return Ok(
                json!({"path": path, "media": ext, "bytes": size, "truncated": truncated, "base64": base64_encode(&data)}),
            );
        }
        // Enough bytes for MAX_READ_CHARS + 1 decoded characters.
        let mut data = Vec::new();
        file.take(4 * (MAX_READ_CHARS as u64 + 2))
            .read_to_end(&mut data)
            .map_err(io_error)?;
        let text = decode_text(&data);
        let mut chars = text.char_indices();
        let truncated = text.chars().nth(MAX_READ_CHARS).is_some();
        let content = match chars.nth(MAX_READ_CHARS) {
            Some((cut, _)) => &text[..cut],
            None => &text[..],
        };
        stamp_read(&target);
        Ok(json!({"path": path, "content": content, "truncated": truncated, "size": size}))
    })())
}

pub fn write_file(params: &Value) -> Value {
    guarded((|| {
        let (root, target) = resolve(params, params.get("path"))?;
        let receipt = if wants_receipts(params) {
            Some(receipt_path(params.get("path"))?)
        } else {
            None
        };
        let Some(content) = params.get("content").and_then(Value::as_str) else {
            return Err("content must be a string".into());
        };
        check_write_allowed(&target)?;
        let snap = snapshot(&root, &target).map_err(io_error)?;
        atomic_write(&target, content).map_err(io_error)?;
        stamp_read(&target);
        let mut out = json!({"path": params.get("path").cloned().unwrap_or(Value::Null), "bytes": content.len()});
        if let Some(snap) = &snap {
            out["snapshot"] = json!(snap.display().to_string());
        }
        let action = if snap.is_some() { "written" } else { "created" };
        Ok(write_result(
            params,
            out,
            receipt
                .map(|p| vec![committed_file(&p, action)])
                .unwrap_or_default(),
        ))
    })())
}

pub fn apply_diff(params: &Value) -> Value {
    guarded((|| {
        let (root, target) = resolve(params, params.get("path"))?;
        let receipt = if wants_receipts(params) {
            Some(receipt_path(params.get("path"))?)
        } else {
            None
        };
        let search = params
            .get("search")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        let Some(search) = search else {
            return Err("search must be a non-empty string".into());
        };
        let Some(replace) = params.get("replace").map_or(Some(""), Value::as_str) else {
            return Err("replace must be a string".into());
        };
        if !target.is_file() {
            return Err(format!(
                "apply_diff edits an existing file — not found: {}",
                py_repr_value(params.get("path"))
            ));
        }
        check_write_allowed(&target)?;
        let text = decode_text(&fs::read(&target).map_err(io_error)?);
        let count = text.matches(search).count();
        if count == 0 {
            return Err("search text not found".into());
        }
        let replace_all = truthy(params.get("replace_all"));
        if count > 1 && !replace_all {
            return Err(format!(
                "search text matches {count} locations — narrow it or set replace_all=true"
            ));
        }
        let snap = snapshot(&root, &target).map_err(io_error)?;
        let new = if replace_all {
            text.replace(search, replace)
        } else {
            text.replacen(search, replace, 1)
        };
        atomic_write(&target, &new).map_err(io_error)?;
        stamp_read(&target);
        let mut out = json!({"path": params.get("path").cloned().unwrap_or(Value::Null), "replacements": if replace_all { count } else { 1 }});
        if let Some(snap) = snap {
            out["snapshot"] = json!(snap.display().to_string());
        }
        Ok(write_result(
            params,
            out,
            receipt
                .map(|p| vec![committed_file(&p, "patched")])
                .unwrap_or_default(),
        ))
    })())
}

/// `lib/tools/project.retired_edit_operation_error`.
fn retired_edit_operation_error(operation: Option<&Value>) -> Option<String> {
    let operation = match operation {
        None | Some(Value::Null) => return None,
        Some(Value::String(s)) if s == "replace" => return None,
        Some(other) => other,
    };
    let retired = "`operation` is retired: edit_file only replaces. Omit it.";
    Some(match operation.as_str() {
        Some("insert_after") => format!(
            "{retired} To insert after anchor A, put A in `anchor` and A followed by the new text in `content`: {{\"anchor\":\"A\",\"content\":\"A\\nNEW\"}}."
        ),
        Some("insert_before") => format!(
            "{retired} To insert before anchor A, put A in `anchor` and the new text followed by A in `content`: {{\"anchor\":\"A\",\"content\":\"NEW\\nA\"}}."
        ),
        Some("replace_all") => format!("{retired} To replace every match, set \"replace_all\": true on the edit."),
        _ => format!("{retired} Each edit is {{anchor, content}}, plus optional path and replace_all."),
    })
}

fn edit_one(params: &Value, edit: &Value, committed: &mut Vec<Value>) -> Result<String, String> {
    let Some(edit) = edit.as_object() else {
        return Err("edit must be an object".into());
    };
    if let Some(retired) = retired_edit_operation_error(edit.get("operation")) {
        return Err(retired);
    }
    let Some(anchor) = edit
        .get("anchor")
        .and_then(Value::as_str)
        .filter(|a| !a.is_empty())
    else {
        return Err("anchor must be a non-empty string".into());
    };
    let Some(content) = edit.get("content").and_then(Value::as_str) else {
        return Err("content must be a string".into());
    };
    let batch_path = params.get("path").and_then(Value::as_str).unwrap_or("");
    let edit_path = match edit.get("path") {
        Some(p) if truthy(Some(p)) => p.clone(),
        _ => json!(batch_path),
    };
    if !truthy(Some(&edit_path)) {
        return Err("no file named — set the batch `path` or this edit's `path`".into());
    }
    let (root, target) = resolve(params, Some(&edit_path))?;
    let receipt = if wants_receipts(params) {
        Some(receipt_path(Some(&edit_path))?)
    } else {
        None
    };
    if !target.is_file() {
        return Err(format!(
            "edit_file edits an existing file — not found: {}",
            py_repr_value(Some(&edit_path))
        ));
    }
    check_write_allowed(&target)?;
    let mut text = String::from_utf8_lossy(&fs::read(&target).map_err(io_error)?).into_owned();
    let mut anchor = anchor.to_string();
    let mut matches = text.matches(anchor.as_str()).count();
    if matches == 0 {
        // Read tools expose universal newlines: try that spelling only after
        // exact matching, never before it.
        text = text.replace("\r\n", "\n").replace('\r', "\n");
        anchor = anchor.replace("\r\n", "\n").replace('\r', "\n");
        matches = text.matches(anchor.as_str()).count();
    }
    let replace_all = truthy(edit.get("replace_all"));
    if matches == 0 {
        return Err("anchor text not found".into());
    }
    if matches > 1 && !replace_all {
        return Err(format!(
            "anchor text matches {matches} locations — narrow it"
        ));
    }
    let new_text = if replace_all {
        text.replace(&anchor, content)
    } else {
        text.replacen(&anchor, content, 1)
    };
    snapshot(&root, &target).map_err(io_error)?;
    atomic_write(&target, &new_text).map_err(io_error)?;
    stamp_read(&target);
    if let Some(receipt) = receipt {
        committed.push(committed_file(&receipt, "patched"));
    }
    Ok(py_str(&edit_path))
}

/// Apply the unified replacement batch inside a declared root. The result is
/// the Python node's text report (`Applied N/M edits ...`).
pub fn edit_file(params: &Value) -> Value {
    let Some(edits) = params
        .get("edits")
        .and_then(Value::as_array)
        .filter(|e| !e.is_empty())
    else {
        return json!({"error": "edits must be a non-empty array"});
    };
    let mut lines = Vec::new();
    let mut committed = Vec::new();
    let (mut ok, mut failed) = (0, 0);
    for (i, edit) in edits.iter().take(EDIT_BATCH_LIMIT).enumerate() {
        let i = i + 1;
        match edit_one(params, edit, &mut committed) {
            Ok(path) => {
                ok += 1;
                lines.push(format!("[{i}] OK {path} [replace]"));
            }
            Err(error) => {
                failed += 1;
                let path = if edit.is_object() {
                    [edit.get("path"), params.get("path")]
                        .into_iter()
                        .flatten()
                        .find(|p| truthy(Some(p)))
                        .map(py_str)
                        .unwrap_or_else(|| "?".into())
                } else {
                    "?".into()
                };
                lines.push(format!("[{i}] FAIL {path}: {error}"));
            }
        }
    }
    let mut header = format!("Applied {ok}/{} edits", ok + failed);
    if failed > 0 {
        header.push_str(&format!(" ({failed} failed)"));
    }
    write_result(
        params,
        json!(format!("{header}\n{}", lines.join("\n"))),
        committed,
    )
}

pub fn grep_search(params: &Value) -> Value {
    guarded((|| {
        let pattern = params
            .get("pattern")
            .and_then(Value::as_str)
            .filter(|p| !p.trim().is_empty());
        let Some(pattern) = pattern else {
            return Err("pattern must be a non-empty string".into());
        };
        let (_, target) = resolve(params, params.get("path"))?;
        let include = params
            .get("include")
            .and_then(Value::as_str)
            .filter(|i| !i.is_empty());
        let context = match params.get("context_lines").filter(|v| truthy(Some(v))) {
            Some(v) => py_int(v)?.clamp(0, GREP_MAX_CONTEXT) as usize,
            None => 0,
        };
        let cap = match params.get("max_results").filter(|v| truthy(Some(v))) {
            Some(v) => py_int(v)?.clamp(1, GREP_MAX_RESULTS) as usize,
            None => GREP_DEFAULT_RESULTS as usize,
        };
        Ok(json!({"matches": tool_grep(&target, pattern, include, context, cap)}))
    })())
}

pub fn find_files(params: &Value) -> Value {
    guarded((|| {
        let (root, target) = resolve(params, params.get("path"))?;
        let shell_output = truthy(params.get("shell_output"));
        if shell_output && !target.is_dir() {
            return Err(format!(
                "not a directory: {}",
                py_repr_value(params.get("path"))
            ));
        }
        let pattern = params
            .get("pattern")
            .filter(|p| truthy(Some(p)))
            .map(py_str)
            .unwrap_or_else(|| "*".into());
        let cap = match params.get("max_results").filter(|v| truthy(Some(v))) {
            Some(v) => py_int(v)?.clamp(1, FIND_MAX_RESULTS) as usize,
            None => FIND_DEFAULT_RESULTS as usize,
        };
        let respect = params
            .get("respect_project_ignores")
            .map(|v| truthy(Some(v)))
            .unwrap_or(true);
        let case_sensitive = truthy(params.get("case_sensitive"));
        let files = if shell_output {
            let rel = params
                .get("path")
                .filter(|p| truthy(Some(p)))
                .map(py_str)
                .unwrap_or_else(|| ".".into());
            tool_find_files(
                &root,
                &target,
                &pattern,
                Some(&rel),
                cap,
                case_sensitive,
                true,
                respect,
            )
        } else {
            tool_find_files(
                &target,
                &target,
                &pattern,
                None,
                cap,
                case_sensitive,
                false,
                respect,
            )
        };
        Ok(json!({"files": files}))
    })())
}

// ── Search: globs, ignore rules, grep, find ─────────────────────────

/// `glob_match.compile_glob`: ripgrep's glob dialect (globset) with the same
/// refused spellings as the Python node.
fn compile_glob(glob: &str, case_sensitive: bool) -> Result<globset::GlobMatcher, String> {
    if glob.is_empty() {
        return Err("empty glob: use '*' to select every file".into());
    }
    if glob.starts_with('!') {
        return Err("a leading '!' would make ripgrep exclude these files instead of selecting them: name the files to select, or match a literal '!' with '\\!'".into());
    }
    if glob.starts_with('#') {
        return Err("a leading '#' makes ripgrep read the glob as a comment and select every file: match a literal '#' with '\\#'".into());
    }
    if glob.ends_with(char::is_whitespace) && !glob.ends_with("\\ ") {
        return Err("trailing whitespace is dropped by ripgrep but kept by fd: remove it, or match a trailing space with '\\ '".into());
    }
    if glob.ends_with('/') {
        return Err("a trailing '/' selects directories only, never files: use 'dir/**' for the files below a directory".into());
    }
    check_glob_structure(glob)?;
    let anchored = glob.contains('/');
    let text = glob.strip_prefix('/').unwrap_or(glob);
    let full = if anchored {
        text.to_string()
    } else {
        format!("**/{text}")
    };
    globset::GlobBuilder::new(&full)
        .literal_separator(true)
        .backslash_escape(true)
        .case_insensitive(!case_sensitive)
        .build()
        .map(|g| g.compile_matcher())
        .map_err(|e| e.kind().to_string())
}

fn check_glob_structure(glob: &str) -> Result<(), String> {
    let chars: Vec<char> = glob.chars().collect();
    let mut groups: Vec<(usize, Vec<bool>)> = Vec::new();
    let mark = |groups: &mut Vec<(usize, Vec<bool>)>| {
        if let Some(last) = groups.last_mut().and_then(|g| g.1.last_mut()) {
            *last = true;
        }
    };
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '\\' => {
                if i + 1 == chars.len() {
                    return Err(
                        "trailing '\\' escapes nothing: match a literal backslash with '[\\]'"
                            .into(),
                    );
                }
                mark(&mut groups);
                i += 2;
                continue;
            }
            '[' => {
                let mut j = i + 1;
                if j < chars.len() && (chars[j] == '!' || chars[j] == '^') {
                    j += 1;
                }
                let mut first = true;
                loop {
                    if j >= chars.len() {
                        return Err("unclosed character class: add the missing ']' or match a literal '[' with '[[]'".into());
                    }
                    let c = chars[j];
                    j += 1;
                    if c == ']' && !first {
                        break;
                    }
                    first = false;
                }
                mark(&mut groups);
                i = j;
                continue;
            }
            '{' => {
                mark(&mut groups);
                groups.push((i, vec![false]));
            }
            ',' if !groups.is_empty() => groups.last_mut().expect("group").1.push(false),
            '}' => {
                let Some((start, branches)) = groups.pop() else {
                    return Err("'}' closes no brace group: match a literal '}' with '[}]'".into());
                };
                let source: String = chars[start..=i].iter().collect();
                if branches.iter().any(|b| !b) {
                    return Err(format!("brace group '{source}' has an empty alternative, which ripgrep ignores: list every alternative in full, e.g. '{{name,name.js}}' instead of 'name{{,.js}}'"));
                }
                if branches.len() < 2 {
                    return Err(format!("brace group '{source}' needs two or more comma-separated alternatives; match literal braces with '[{{]' and '[}}]'"));
                }
            }
            _ => mark(&mut groups),
        }
        i += 1;
    }
    if !groups.is_empty() {
        return Err(
            "unclosed brace group: add the missing '}' or match a literal '{' with '[{]'".into(),
        );
    }
    Ok(())
}

fn glob_rejection(field: &str, glob: &str, error: &str) -> String {
    format!(
        "Invalid pattern: `{field}` glob '{glob}': {error}.\nNothing was searched. Globs use ripgrep syntax: `*`, `?`, `[abc]`, `\\` escapes and `{{a,b}}` alternatives, e.g. `*.{{ts,js}}`."
    )
}

/// Python `fnmatch.fnmatchcase`: `*` (any run, '/' included), `?`, `[...]`/`[!...]`.
fn fnmatch(name: &str, pattern: &str) -> bool {
    enum Tok {
        Star,
        Any,
        Class(bool, Vec<(char, char)>),
        Lit(char),
    }
    let p: Vec<char> = pattern.chars().collect();
    let mut toks = Vec::new();
    let mut i = 0;
    while i < p.len() {
        match p[i] {
            '*' => {
                if !matches!(toks.last(), Some(Tok::Star)) {
                    toks.push(Tok::Star);
                }
            }
            '?' => toks.push(Tok::Any),
            '[' => {
                let mut j = i + 1;
                if j < p.len() && p[j] == '!' {
                    j += 1;
                }
                if j < p.len() && p[j] == ']' {
                    j += 1;
                }
                while j < p.len() && p[j] != ']' {
                    j += 1;
                }
                if j >= p.len() {
                    toks.push(Tok::Lit('['));
                } else {
                    let mut body: &[char] = &p[i + 1..j];
                    let negated = body.first() == Some(&'!');
                    if negated {
                        body = &body[1..];
                    }
                    let mut ranges = Vec::new();
                    let mut k = 0;
                    while k < body.len() {
                        if k + 2 < body.len() && body[k + 1] == '-' {
                            ranges.push((body[k], body[k + 2]));
                            k += 3;
                        } else {
                            ranges.push((body[k], body[k]));
                            k += 1;
                        }
                    }
                    toks.push(Tok::Class(negated, ranges));
                    i = j;
                }
            }
            c => toks.push(Tok::Lit(c)),
        }
        i += 1;
    }
    let s: Vec<char> = name.chars().collect();
    let one = |tok: &Tok, c: char| match tok {
        Tok::Any => true,
        Tok::Lit(l) => *l == c,
        Tok::Class(neg, ranges) => ranges.iter().any(|(lo, hi)| *lo <= c && c <= *hi) != *neg,
        Tok::Star => false,
    };
    let (mut si, mut ti) = (0, 0);
    let mut backtrack: Option<(usize, usize)> = None;
    while si < s.len() {
        if ti < toks.len() && matches!(toks[ti], Tok::Star) {
            backtrack = Some((ti, si));
            ti += 1;
        } else if ti < toks.len() && one(&toks[ti], s[si]) {
            ti += 1;
            si += 1;
        } else if let Some((star, at)) = backtrack {
            ti = star + 1;
            si = at + 1;
            backtrack = Some((star, at + 1));
        } else {
            return false;
        }
    }
    while ti < toks.len() && matches!(toks[ti], Tok::Star) {
        ti += 1;
    }
    ti == toks.len()
}

/// Root `.gitignore`/`.ignore` rules (`read_tools._load_gitignore_patterns`).
fn load_ignore_rules(base: &Path) -> Vec<(String, bool)> {
    let mut rules = Vec::new();
    for name in IGNORE_RULE_FILES {
        let Ok(bytes) = fs::read(base.join(name)) else {
            continue;
        };
        for line in String::from_utf8_lossy(&bytes).lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let negated = line.starts_with('!');
            let line = if negated { &line[1..] } else { line };
            rules.push((line.trim_start_matches('/').to_string(), negated));
        }
    }
    rules
}

/// `read_tools._gitignore_match`: whether `rel` ('/'-separated) is ignored.
fn ignore_match(rel: &str, is_dir: bool, rules: &[(String, bool)]) -> bool {
    let fold = |s: &str| {
        if cfg!(windows) {
            s.to_lowercase()
        } else {
            s.to_string()
        }
    };
    let mut ignored = false;
    for (pattern, negated) in rules {
        let mut pattern = pattern.as_str();
        if pattern.ends_with('/') {
            if !is_dir {
                continue;
            }
            pattern = pattern.trim_end_matches('/');
        }
        let basename = rel.rsplit('/').next().unwrap_or(rel);
        let matched = if !pattern.contains('/') {
            fnmatch(&fold(basename), &fold(pattern))
        } else {
            fnmatch(&fold(rel), &fold(pattern))
                || fnmatch(&fold(rel), &fold(&format!("**/{pattern}")))
        };
        if matched {
            ignored = !negated;
        }
    }
    ignored
}

fn relative_slash(path: &Path, base: &Path) -> String {
    let rel = path.strip_prefix(base).unwrap_or(path);
    let parts: Vec<String> = rel
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    }
}

fn sorted_entries(dir: &Path) -> std::io::Result<Vec<fs::DirEntry>> {
    let mut entries: Vec<fs::DirEntry> = fs::read_dir(dir)?.filter_map(Result::ok).collect();
    entries.sort_by_key(|e| e.file_name());
    Ok(entries)
}

/// `scanner._fmt_size`.
fn fmt_size(n: u64) -> String {
    if n < 1024 {
        format!("{n}B")
    } else if n < 1024 * 1024 {
        format!("{:.1}KB", n as f64 / 1024.0)
    } else {
        format!("{:.1}MB", n as f64 / 1024.0 / 1024.0)
    }
}

/// One `(match count, lowest line, highest line)` per file.
type FileStats = BTreeMap<String, (usize, usize, usize)>;

fn merge_stats(stats: &mut FileStats, path: &str, count: usize, lo: usize, hi: usize) {
    let entry = stats.entry(path.to_string()).or_insert((0, 0, 0));
    entry.0 += count;
    if lo > 0 {
        entry.1 = if entry.1 > 0 { entry.1.min(lo) } else { lo };
    }
    if hi > 0 {
        entry.2 = entry.2.max(hi);
    }
}

/// Recursive regex search with the result text of `read_tools.tool_grep`
/// (ripgrep backend: case-insensitive, hidden files searched, `IGNORE_DIRS`
/// and root ignore rules pruned, binary files skipped, symlinks not followed).
fn tool_grep(
    target: &Path,
    pattern: &str,
    include: Option<&str>,
    context: usize,
    cap: usize,
) -> String {
    if !target.exists() {
        return "Path not found: .. Nothing was searched.".into();
    }
    let include_glob = match include.map(|i| compile_glob(i, false)) {
        Some(Err(error)) => return glob_rejection("include", include.unwrap_or(""), &error),
        Some(Ok(glob)) => Some(glob),
        None => None,
    };
    let regex = match regex::RegexBuilder::new(pattern).case_insensitive(true).build() {
        Ok(regex) => regex,
        Err(error) => {
            return format!(
                "Invalid pattern: {error}\nNothing was searched. `pattern` is a Rust regex: escape ( ) [ ] {{ }} . * + ? | ^ $ \\ with a backslash to match them literally; `include` is a glob such as `*.py`."
            )
        }
    };
    let single = target.is_file();
    let mut files: Vec<(PathBuf, Option<String>)> = Vec::new();
    if single {
        files.push((target.to_path_buf(), None));
    } else {
        let rules = load_ignore_rules(target);
        let mut stack = vec![target.to_path_buf()];
        // Depth-first in name order: a directory's entries before the next sibling.
        let mut ordered: Vec<(PathBuf, Option<String>)> = Vec::new();
        while let Some(dir) = stack.pop() {
            let Ok(entries) = sorted_entries(&dir) else {
                continue;
            };
            let mut children = Vec::new();
            for entry in entries {
                let Ok(kind) = entry.file_type() else {
                    continue;
                };
                let path = entry.path();
                let name = entry.file_name().to_string_lossy().into_owned();
                let rel = relative_slash(&path, target);
                if kind.is_dir() {
                    if IGNORE_DIRS.contains(&name.as_str()) || ignore_match(&rel, true, &rules) {
                        continue;
                    }
                    children.push(path);
                } else if kind.is_file() {
                    if ignore_match(&rel, false, &rules) {
                        continue;
                    }
                    if include_glob.as_ref().is_some_and(|g| !g.is_match(&rel)) {
                        continue;
                    }
                    ordered.push((path, Some(rel)));
                }
            }
            stack.extend(children.into_iter().rev());
        }
        files = ordered;
    }

    let mut records: Vec<String> = Vec::new();
    let mut shown = 0usize;
    let mut last_lineno = 0usize;
    let mut remainder = FileStats::new();
    let mut remainder_total = 0usize;
    for (path, rel) in &files {
        let Ok(bytes) = fs::read(path) else { continue };
        if bytes.contains(&0) {
            continue;
        }
        let text = String::from_utf8_lossy(&bytes);
        let lines: Vec<&str> = text
            .split_terminator('\n')
            .map(|l| l.strip_suffix('\r').unwrap_or(l))
            .collect();
        let hits: Vec<usize> = (0..lines.len())
            .filter(|&i| regex.is_match(lines[i]))
            .collect();
        let mut emitted_until: Option<usize> = None; // last emitted line index of this file
        for &hit in &hits {
            if shown >= cap {
                remainder_total += 1;
                merge_stats(
                    &mut remainder,
                    rel.as_deref().unwrap_or(""),
                    1,
                    hit + 1,
                    hit + 1,
                );
                continue;
            }
            let start = hit.saturating_sub(context);
            let start = emitted_until.map_or(start, |e| start.max(e + 1));
            if context > 0 && !records.is_empty() && emitted_until.is_none_or(|e| start > e + 1) {
                records.push("--".into());
            }
            let render = |i: usize, sep: char| match rel {
                Some(rel) => format!("{rel}\0{}{sep}{}", i + 1, lines[i]),
                None => format!("{}{sep}{}", i + 1, lines[i]),
            };
            for i in start..hit {
                records.push(render(i, '-'));
            }
            records.push(render(hit, ':'));
            shown += 1;
            last_lineno = hit + 1;
            emitted_until = Some(hit);
            if shown < cap {
                let next_hit = hits.iter().find(|&&h| h > hit).copied();
                let end = (hit + context).min(lines.len().saturating_sub(1));
                let end = next_hit.map_or(end, |n| end.min(n.saturating_sub(1)));
                for i in hit + 1..=end.max(hit) {
                    if i > hit {
                        records.push(render(i, '-'));
                        emitted_until = Some(i);
                    }
                }
            }
        }
    }
    format_grep_output(
        records,
        pattern,
        include,
        context,
        shown,
        last_lineno,
        remainder,
        remainder_total,
    )
}

fn is_match_record(line: &str) -> bool {
    let rest = line.split_once('\0').map_or(line, |(_, rest)| rest);
    let digits = rest.chars().take_while(char::is_ascii_digit).count();
    digits > 0 && rest[digits..].starts_with(':')
}

fn record_lineno(line: &str) -> usize {
    let rest = line.split_once('\0').map_or(line, |(_, rest)| rest);
    rest.chars()
        .take_while(char::is_ascii_digit)
        .collect::<String>()
        .parse()
        .unwrap_or(0)
}

/// `read_tools._format_grep_output` over the records the search produced.
#[allow(clippy::too_many_arguments)]
fn format_grep_output(
    records: Vec<String>,
    pattern: &str,
    include: Option<&str>,
    context: usize,
    shown: usize,
    last_lineno: usize,
    remainder: FileStats,
    remainder_total: usize,
) -> String {
    if records.is_empty() {
        let mut hint = format!("No matches found for: {pattern}");
        if let Some(include) = include {
            hint.push_str(&format!(" in {include}"));
        }
        if pattern.contains('\\') || pattern.contains(".*") || pattern.contains('|') {
            hint.push_str("\nHint: pattern looks like complex regex. Try a simpler literal substring instead.");
        } else {
            hint.push_str("\nHint: try a shorter/broader substring, or check spelling. Search is case-insensitive.");
        }
        return hint;
    }
    let max_total_chars = if context > 0 {
        GREP_MAX_CHARS_WITH_CONTEXT
    } else {
        GREP_MAX_CHARS
    };
    let mut rel_lines: Vec<String> = Vec::new();
    let mut total_chars = 0usize;
    let mut match_count = 0usize;
    let mut displayed_last = 0usize;
    let mut cut: Option<usize> = None;
    for (idx, line) in records.iter().enumerate() {
        let is_match = is_match_record(line);
        let mut rendered = line.clone();
        if rendered.chars().count() > GREP_MAX_LINE_CHARS {
            rendered = format!(
                "{}  \u{2026}(truncated)",
                crate::util::truncate_chars(&rendered, GREP_MAX_LINE_CHARS)
            );
        }
        total_chars += rendered.chars().count() + 1;
        if total_chars > max_total_chars {
            cut = Some(idx);
            break;
        }
        if is_match {
            match_count += 1;
            displayed_last = record_lineno(line);
        }
        rel_lines.push(rendered);
    }
    let _ = shown;
    let mut grouped = group_grep_lines(rel_lines);
    if let Some(cut) = cut {
        let mut unshown = FileStats::new();
        let mut unshown_total = 0;
        for line in &records[cut..] {
            if !is_match_record(line) {
                continue;
            }
            let path = line.split_once('\0').map_or("", |(p, _)| p);
            let lineno = record_lineno(line);
            unshown_total += 1;
            merge_stats(&mut unshown, path, 1, lineno, lineno);
        }
        for (path, (count, lo, hi)) in &remainder {
            merge_stats(&mut unshown, path, *count, *lo, *hi);
        }
        grouped.extend(render_withheld(
            match_count,
            "chars",
            max_total_chars,
            displayed_last,
            &unshown,
            unshown_total + remainder_total,
        ));
    } else if remainder_total > 0 {
        grouped.extend(render_withheld(
            match_count,
            "cap",
            max_total_chars,
            last_lineno,
            &remainder,
            remainder_total,
        ));
    }
    let mut header = format!("grep \"{pattern}\"");
    if let Some(include) = include {
        header.push_str(&format!(" ({include})"));
    }
    header.push_str(&format!(" \u{2014} {match_count} matches:\n\n"));
    header + &grouped.join("\n")
}

/// `read_tools._group_grep_lines`: one header per run of one file's records.
fn group_grep_lines(lines: Vec<String>) -> Vec<String> {
    let mut grouped = Vec::new();
    let mut current: Option<String> = None;
    for line in lines {
        match line.split_once('\0') {
            None => {
                if line == "--" {
                    current = None;
                }
                grouped.push(line);
            }
            Some((path, rest)) => {
                if current.as_deref() != Some(path) {
                    grouped.push(path.to_string());
                    current = Some(path.to_string());
                }
                grouped.push(rest.to_string());
            }
        }
    }
    grouped
}

/// `read_tools._render_withheld`: one summary line plus one recovery hint.
fn render_withheld(
    shown: usize,
    fired: &str,
    max_total_chars: usize,
    last_lineno: usize,
    files: &FileStats,
    total_more: usize,
) -> Vec<String> {
    let mut ranked: Vec<(&String, &(usize, usize, usize))> = files.iter().collect();
    ranked.sort_by(|a, b| b.1 .0.cmp(&a.1 .0).then_with(|| a.0.cmp(b.0)));
    let mut parts: Vec<String> = ranked
        .iter()
        .take(WITHHELD_LIST_MAX)
        .map(|(path, (count, lo, hi))| {
            let mut entry = if path.is_empty() {
                format!("this file ({count}")
            } else {
                format!("{path} ({count}")
            };
            if *hi > 0 {
                if lo == hi {
                    entry.push_str(&format!(", line {lo}"));
                } else {
                    entry.push_str(&format!(", lines {lo}\u{2013}{hi}"));
                }
            }
            entry + ")"
        })
        .collect();
    if ranked.len() > WITHHELD_LIST_MAX {
        parts.push(format!("+{} more files", ranked.len() - WITHHELD_LIST_MAX));
    }
    let lead = if fired == "chars" {
        format!("\u{2026} ({shown} shown at {max_total_chars}-char limit")
    } else {
        format!("\u{2026} ({shown} shown")
    };
    let line1 = format!(
        "{lead} \u{2014} {total_more} more matches: {})",
        parts.join("; ")
    );
    let line2 = if ranked.len() == 1 && last_lineno > 0 {
        format!("[page with from_line={}]", last_lineno + 1)
    } else {
        "[narrow with path= or include=; page within one file with from_line=N]".into()
    };
    vec![line1, line2]
}

/// `read_tools.tool_find_files` (Python walk backend).
#[allow(clippy::too_many_arguments)]
fn tool_find_files(
    base: &Path,
    target: &Path,
    pattern: &str,
    rel_path: Option<&str>,
    cap: usize,
    case_sensitive: bool,
    shell_output: bool,
    respect_project_ignores: bool,
) -> String {
    if !target.is_dir() {
        let shown = rel_path.unwrap_or(".");
        return if shell_output {
            format!("find: '{shown}': No such directory")
        } else {
            format!("Not a directory: {shown}")
        };
    }
    type Selector = Box<dyn Fn(&str, &str) -> bool>;
    let selector: Selector = if shell_output {
        let comparable = if case_sensitive {
            pattern.to_string()
        } else {
            pattern.to_lowercase()
        };
        Box::new(move |name: &str, _rel: &str| {
            if case_sensitive {
                fnmatch(name, &comparable)
            } else {
                fnmatch(&name.to_lowercase(), &comparable)
            }
        })
    } else {
        let mut stripped = pattern;
        while let Some(rest) = stripped.strip_prefix("**/") {
            stripped = rest;
        }
        let match_pattern = if stripped.contains('/') {
            pattern
        } else {
            stripped
        };
        match compile_glob(match_pattern, case_sensitive) {
            Ok(glob) => Box::new(move |_name: &str, rel: &str| glob.is_match(rel)),
            Err(error) => return glob_rejection("pattern", pattern, &error),
        }
    };
    let rules = if respect_project_ignores {
        load_ignore_rules(base)
    } else {
        Vec::new()
    };
    let query_cap = cap + 1;
    let mut matches: Vec<(String, u64)> = Vec::new();
    let mut notices: Vec<String> = Vec::new();
    let mut stack = vec![target.to_path_buf()];
    'walk: while let Some(dir) = stack.pop() {
        let entries = match sorted_entries(&dir) {
            Ok(entries) => entries,
            Err(error) => {
                if notices.len() < FIND_ERROR_NOTICES {
                    notices.push(format!(
                        "[unable to scan {}: {error}]",
                        relative_slash(&dir, base)
                    ));
                }
                continue;
            }
        };
        let mut children = Vec::new();
        for entry in entries {
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            let rel = relative_slash(&path, base);
            if kind.is_dir() {
                if respect_project_ignores && IGNORE_DIRS.contains(&name.as_str()) {
                    continue;
                }
                if ignore_match(&rel, true, &rules) {
                    continue;
                }
                children.push(path);
                continue;
            }
            if !kind.is_file() || !selector(&name, &rel) || ignore_match(&rel, false, &rules) {
                continue;
            }
            let Ok(meta) = fs::symlink_metadata(&path) else {
                continue;
            };
            matches.push((rel, meta.len()));
            if matches.len() >= query_cap {
                break 'walk;
            }
        }
        stack.extend(children.into_iter().rev());
    }
    if matches.is_empty() && notices.is_empty() {
        return if shell_output {
            String::new()
        } else {
            format!("No files matching: {pattern}")
        };
    }
    let capped = matches.len() > cap;
    matches.truncate(cap);
    let capped_notice = format!("[results capped at {cap}; narrow the pattern or path]");
    if shell_output {
        let requested = rel_path.unwrap_or(".");
        let dot_prefixed = requested == "." || requested.starts_with("./");
        let mut lines: Vec<String> = matches
            .iter()
            .map(|(rel, _)| {
                if dot_prefixed && !rel.starts_with("./") {
                    format!("./{rel}")
                } else {
                    rel.clone()
                }
            })
            .collect();
        lines.extend(notices.iter().map(|n| format!("find: {n}")));
        if capped {
            lines.push(format!("find: {capped_notice}"));
        }
        return lines.join("\n");
    }
    let mut header = format!("Files matching \"{pattern}\"");
    if let Some(rel) = rel_path {
        header.push_str(&format!(" in {rel}"));
    }
    let mut rendered: Vec<String> = matches
        .iter()
        .map(|(rel, size)| format!("  {rel} ({})", fmt_size(*size)))
        .collect();
    rendered.extend(notices.iter().map(|n| format!("  {n}")));
    if capped {
        rendered.push(format!("  {capped_notice}"));
    }
    format!(
        "{header} ({} found):\n\n{}",
        matches.len(),
        rendered.join("\n")
    )
}

// ── project_run_command ─────────────────────────────────────────────

/// `_exec.command_timeout`: None is completion-owned; explicit finite
/// nonnegative seconds are exact.
fn command_timeout(value: Option<&Value>) -> Result<Option<f64>, String> {
    const REFUSAL: &str = "Command timeout must be nonnegative and finite or None";
    let timeout = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::String(s)) => s.trim().parse::<f64>().ok(),
        Some(_) => None,
    };
    match timeout {
        Some(t) if t.is_finite() && t >= 0.0 => Ok(Some(t)),
        _ => Err(REFUSAL.into()),
    }
}

struct RunSpec {
    command: String,
    cwd: PathBuf,
    timeout: Option<f64>,
}

/// `_project._validate_project_run`: root, workdir containment, the
/// dangerous-command and delete guards, timeout parsing.
fn validate_project_run(params: &Value) -> Result<RunSpec, String> {
    let (root, target) = resolve(params, params.get("workdir"))?;
    if !target.is_dir() {
        return Err(format!(
            "workdir is not a directory: {}",
            py_repr_value(params.get("workdir"))
        ));
    }
    let Some(command) = params
        .get("command")
        .and_then(Value::as_str)
        .filter(|c| !c.trim().is_empty())
    else {
        return Err("command must be a non-empty string".into());
    };
    guard::check(command, &target, &root, &crate::config::home_dir())?;
    let timeout = command_timeout(params.get("timeout"))?;
    Ok(RunSpec {
        command: command.to_string(),
        cwd: target,
        timeout,
    })
}

/// Validate and start a streamed `project_run_command`. `Err` is a refusal
/// before anything started. The post-command project index invalidation of
/// the Python node only drops a Python-process cache (`tree_index`); this
/// node keeps no index, so there is nothing to invalidate.
pub fn start_project_run(
    _cmd_id: &str,
    params: &Value,
    on_chunk: super::ChunkSink,
    on_exit: super::ExitSink,
    cancel: crate::util::Stop,
) -> Result<(), String> {
    let spec = validate_project_run(params)?;
    super::exec::start_streamed_command(
        &spec.command,
        &spec.cwd,
        spec.timeout,
        None,
        on_chunk,
        on_exit,
        cancel,
    )
}

/// Shell-command guards of the project run: a port of the judgements the
/// Python node takes from `lib/project_mod/command_analysis.py`
/// (`_is_dangerous_command`, `_is_catastrophic_delete`,
/// `resolve_delete_targets`) over a bounded POSIX shell parse. A command the
/// parser cannot delimit is judged fail-closed on its raw text.
mod guard {
    use super::{is_within, normpath, py_repr, realpath};
    use std::collections::{BTreeSet, HashMap};
    use std::path::{Path, PathBuf};
    use std::sync::OnceLock;

    /// `lib/project_mod/config.DANGEROUS_PATTERNS`, applied to text whose
    /// structure is unknown.
    const DANGEROUS_PATTERNS: &[&str] = &[
        r"\bmkfs\b",
        r"\bdd\s+.*of=/",
        r">\s*/dev/sd",
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\binit\s+0\b",
        r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\s+(?:\S+\s+)?/[*.]?(?:\s|$)",
        r"\bformat\s+[a-zA-Z]:",
        r"\brd\s+/s\s+/q\b",
        r"\bdel\s+/s\s+/q\b",
        r"\bdiskpart\b",
    ];
    const FORK_BOMB: &str = r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:";
    const MIN_DELETE_DEPTH: usize = 2;
    const SHELLS: &[&str] = &["sh", "bash", "dash", "zsh", "ksh", "ash"];
    const DELETE_PROGRAMS: &[&str] = &["rm", "rmdir", "unlink"];

    fn regex(cell: &'static OnceLock<regex::Regex>, pattern: &str) -> &'static regex::Regex {
        cell.get_or_init(|| regex::Regex::new(pattern).expect("static regex"))
    }

    fn raw_dangerous(text: &str) -> bool {
        static ALL: OnceLock<regex::Regex> = OnceLock::new();
        regex(&ALL, &DANGEROUS_PATTERNS.join("|")).is_match(text)
    }

    fn could_run_delete(text: &str) -> bool {
        static DELETE: OnceLock<regex::Regex> = OnceLock::new();
        regex(&DELETE, r"\b(rm|rmdir|unlink)\b").is_match(text)
    }

    // ── Parse ──

    #[derive(Clone, Debug)]
    enum Seg {
        Lit(String, bool),
        Var(String),
        Opaque,
    }

    #[derive(Clone, Debug, Default)]
    struct Word {
        segs: Vec<Seg>,
        raw: String,
    }

    impl Word {
        fn push_lit(&mut self, c: char, quoted: bool) {
            if let Some(Seg::Lit(text, q)) = self.segs.last_mut() {
                if *q == quoted {
                    text.push(c);
                    return;
                }
            }
            self.segs.push(Seg::Lit(c.to_string(), quoted));
        }

        /// The word's text when it is plain literal text.
        fn literal(&self) -> Option<String> {
            let mut out = String::new();
            for seg in &self.segs {
                match seg {
                    Seg::Lit(text, _) => out.push_str(text),
                    _ => return None,
                }
            }
            Some(out)
        }

        fn unquoted_literal(&self) -> Option<&str> {
            match self.segs.as_slice() {
                [Seg::Lit(text, false)] => Some(text),
                _ => None,
            }
        }
    }

    #[derive(Debug, Default)]
    struct Command {
        words: Vec<Word>,
        redirects: Vec<(String, Word)>,
        heredocs: Vec<String>,
        nested: Vec<Vec<Command>>,
        subshell: bool,
    }

    impl Command {
        fn is_empty(&self) -> bool {
            self.words.is_empty()
                && self.redirects.is_empty()
                && self.nested.is_empty()
                && !self.subshell
        }
    }

    struct Unknown;

    struct Parser {
        chars: Vec<char>,
        pos: usize,
    }

    fn parse(text: &str) -> Result<Vec<Command>, Unknown> {
        let mut parser = Parser {
            chars: text.chars().collect(),
            pos: 0,
        };
        parser.script(None)
    }

    impl Parser {
        fn peek(&self, ahead: usize) -> Option<char> {
            self.chars.get(self.pos + ahead).copied()
        }

        fn script(&mut self, closer: Option<char>) -> Result<Vec<Command>, Unknown> {
            let mut commands: Vec<Command> = Vec::new();
            let mut current = Command::default();
            let mut heredocs: Vec<(String, bool, usize)> = Vec::new();
            loop {
                let Some(c) = self.peek(0) else {
                    if closer.is_some() {
                        return Err(Unknown);
                    }
                    break;
                };
                match c {
                    ' ' | '\t' => self.pos += 1,
                    '\\' if self.peek(1) == Some('\n') => self.pos += 2,
                    '#' => {
                        while self.peek(0).is_some_and(|c| c != '\n') {
                            self.pos += 1;
                        }
                    }
                    '\n' => {
                        self.pos += 1;
                        finish(&mut commands, &mut current);
                        for (delimiter, strip_tabs, index) in std::mem::take(&mut heredocs) {
                            let body = self.heredoc_body(&delimiter, strip_tabs);
                            if let Some(command) = commands.get_mut(index) {
                                command.heredocs.push(body);
                            }
                        }
                    }
                    '&' if self.peek(1) == Some('>') => {
                        self.redirect(&mut current, &mut heredocs, commands.len())?
                    }
                    ';' | '&' | '|' => {
                        while self.peek(0).is_some_and(|c| matches!(c, ';' | '&' | '|')) {
                            self.pos += 1;
                        }
                        finish(&mut commands, &mut current);
                    }
                    ')' => {
                        if closer != Some(')') {
                            return Err(Unknown);
                        }
                        self.pos += 1;
                        finish(&mut commands, &mut current);
                        return Ok(commands);
                    }
                    '(' => {
                        if current.words.is_empty() && current.redirects.is_empty() {
                            if self.peek(1) == Some('(') {
                                self.skip_arithmetic()?;
                                continue;
                            }
                            self.pos += 1;
                            let body = self.script(Some(')'))?;
                            commands.push(Command {
                                nested: vec![body],
                                subshell: true,
                                ..Default::default()
                            });
                        } else if self.peek(1) == Some(')') {
                            // `name()` function header: the body's commands are judged where written.
                            self.pos += 2;
                            current = Command::default();
                        } else {
                            return Err(Unknown);
                        }
                    }
                    '<' | '>' if self.peek(1) == Some('(') => {
                        self.pos += 2;
                        let body = self.script(Some(')'))?;
                        current.nested.push(body);
                        current.words.push(Word {
                            segs: vec![Seg::Opaque],
                            raw: String::new(),
                        });
                    }
                    '<' | '>' => self.redirect(&mut current, &mut heredocs, commands.len())?,
                    _ => {
                        let word = self.word(&mut current.nested)?;
                        let fd =
                            !word.raw.is_empty() && word.raw.chars().all(|c| c.is_ascii_digit());
                        if fd && matches!(self.peek(0), Some('<') | Some('>')) {
                            self.redirect(&mut current, &mut heredocs, commands.len())?;
                        } else {
                            current.words.push(word);
                        }
                    }
                }
            }
            finish(&mut commands, &mut current);
            Ok(commands)
        }

        fn skip_arithmetic(&mut self) -> Result<(), Unknown> {
            let mut depth = 0i32;
            while let Some(c) = self.peek(0) {
                self.pos += 1;
                match c {
                    '(' => depth += 1,
                    ')' => {
                        depth -= 1;
                        if depth == 0 {
                            return Ok(());
                        }
                    }
                    _ => {}
                }
            }
            Err(Unknown)
        }

        fn heredoc_body(&mut self, delimiter: &str, strip_tabs: bool) -> String {
            let mut body = String::new();
            while self.pos < self.chars.len() {
                let start = self.pos;
                while self.peek(0).is_some_and(|c| c != '\n') {
                    self.pos += 1;
                }
                let line: String = self.chars[start..self.pos].iter().collect();
                if self.peek(0) == Some('\n') {
                    self.pos += 1;
                }
                let compared = if strip_tabs {
                    line.trim_start_matches('\t')
                } else {
                    line.as_str()
                };
                if compared == delimiter {
                    break;
                }
                body.push_str(&line);
                body.push('\n');
            }
            body
        }

        fn redirect(
            &mut self,
            current: &mut Command,
            heredocs: &mut Vec<(String, bool, usize)>,
            index: usize,
        ) -> Result<(), Unknown> {
            let start = self.pos;
            while self
                .peek(0)
                .is_some_and(|c| matches!(c, '<' | '>' | '&' | '|' | '-'))
            {
                // `-` only belongs to `<<-`.
                if self.peek(0) == Some('-')
                    && !self.chars[start..self.pos]
                        .iter()
                        .collect::<String>()
                        .ends_with("<<")
                {
                    break;
                }
                self.pos += 1;
            }
            let op: String = self.chars[start..self.pos].iter().collect();
            while matches!(self.peek(0), Some(' ') | Some('\t')) {
                self.pos += 1;
            }
            if self
                .peek(0)
                .is_none_or(|c| matches!(c, '\n' | ';' | '|' | '&' | '(' | ')' | '<' | '>'))
            {
                return Err(Unknown);
            }
            let target = self.word(&mut current.nested)?;
            if op == "<<" || op == "<<-" {
                let delimiter = target
                    .segs
                    .iter()
                    .map(|s| {
                        if let Seg::Lit(t, _) = s {
                            t.as_str()
                        } else {
                            ""
                        }
                    })
                    .collect();
                heredocs.push((delimiter, op == "<<-", index));
            }
            current.redirects.push((op, target));
            Ok(())
        }

        fn word(&mut self, nested: &mut Vec<Vec<Command>>) -> Result<Word, Unknown> {
            let start = self.pos;
            let mut word = Word::default();
            while let Some(c) = self.peek(0) {
                match c {
                    ' ' | '\t' | '\n' | ';' | '&' | '|' | '(' | ')' | '<' | '>' => break,
                    '\\' => {
                        match self.peek(1) {
                            Some('\n') => {}
                            Some(next) => word.push_lit(next, true),
                            None => return Err(Unknown),
                        }
                        self.pos += 2;
                    }
                    '\'' => {
                        self.pos += 1;
                        let close = self.chars[self.pos..]
                            .iter()
                            .position(|&c| c == '\'')
                            .ok_or(Unknown)?;
                        for i in 0..close {
                            word.push_lit(self.chars[self.pos + i], true);
                        }
                        self.pos += close + 1;
                    }
                    '"' => {
                        self.pos += 1;
                        loop {
                            match self.peek(0) {
                                None => return Err(Unknown),
                                Some('"') => {
                                    self.pos += 1;
                                    break;
                                }
                                Some('\\') => {
                                    match self.peek(1) {
                                        Some('\n') => {}
                                        Some(next @ ('$' | '`' | '"' | '\\')) => {
                                            word.push_lit(next, true)
                                        }
                                        Some(next) => {
                                            word.push_lit('\\', true);
                                            word.push_lit(next, true);
                                        }
                                        None => return Err(Unknown),
                                    }
                                    self.pos += 2;
                                }
                                Some('$') => self.dollar(&mut word, nested, true)?,
                                Some('`') => self.backtick(&mut word, nested)?,
                                Some(other) => {
                                    word.push_lit(other, true);
                                    self.pos += 1;
                                }
                            }
                        }
                    }
                    '$' => self.dollar(&mut word, nested, false)?,
                    '`' => self.backtick(&mut word, nested)?,
                    other => {
                        word.push_lit(other, false);
                        self.pos += 1;
                    }
                }
            }
            word.raw = self.chars[start..self.pos].iter().collect();
            Ok(word)
        }

        fn backtick(
            &mut self,
            word: &mut Word,
            nested: &mut Vec<Vec<Command>>,
        ) -> Result<(), Unknown> {
            self.pos += 1;
            let mut body = String::new();
            loop {
                match self.peek(0) {
                    None => return Err(Unknown),
                    Some('`') => {
                        self.pos += 1;
                        break;
                    }
                    Some('\\') if matches!(self.peek(1), Some('`' | '\\' | '$')) => {
                        body.push(self.peek(1).expect("escaped"));
                        self.pos += 2;
                    }
                    Some(c) => {
                        body.push(c);
                        self.pos += 1;
                    }
                }
            }
            nested.push(parse(&body)?);
            word.segs.push(Seg::Opaque);
            Ok(())
        }

        fn dollar(
            &mut self,
            word: &mut Word,
            nested: &mut Vec<Vec<Command>>,
            quoted: bool,
        ) -> Result<(), Unknown> {
            match self.peek(1) {
                Some('(') if self.peek(2) == Some('(') => {
                    self.pos += 1;
                    self.skip_arithmetic()?;
                    word.segs.push(Seg::Opaque);
                }
                Some('(') => {
                    self.pos += 2;
                    nested.push(self.script(Some(')'))?);
                    word.segs.push(Seg::Opaque);
                }
                Some('{') => {
                    self.pos += 2;
                    let mut depth = 1;
                    let start = self.pos;
                    while depth > 0 {
                        match self.peek(0) {
                            None => return Err(Unknown),
                            Some('{') => depth += 1,
                            Some('}') => depth -= 1,
                            _ => {}
                        }
                        self.pos += 1;
                    }
                    let inner: String = self.chars[start..self.pos - 1].iter().collect();
                    if is_name(&inner) {
                        word.segs.push(Seg::Var(inner));
                    } else {
                        word.segs.push(Seg::Opaque);
                    }
                }
                Some('\'') if !quoted => {
                    self.pos += 2;
                    loop {
                        match self.peek(0) {
                            None => return Err(Unknown),
                            Some('\'') => {
                                self.pos += 1;
                                break;
                            }
                            Some('\\') => {
                                let escaped = match self.peek(1) {
                                    Some('n') => '\n',
                                    Some('t') => '\t',
                                    Some(other) => other,
                                    None => return Err(Unknown),
                                };
                                word.push_lit(escaped, true);
                                self.pos += 2;
                            }
                            Some(c) => {
                                word.push_lit(c, true);
                                self.pos += 1;
                            }
                        }
                    }
                }
                Some(c) if c.is_ascii_alphabetic() || c == '_' => {
                    self.pos += 1;
                    let start = self.pos;
                    while self
                        .peek(0)
                        .is_some_and(|c| c.is_ascii_alphanumeric() || c == '_')
                    {
                        self.pos += 1;
                    }
                    word.segs
                        .push(Seg::Var(self.chars[start..self.pos].iter().collect()));
                }
                Some(c) if c.is_ascii_digit() || "@*#?-$!".contains(c) => {
                    self.pos += 2;
                    word.segs.push(Seg::Var(c.to_string()));
                }
                _ => {
                    word.push_lit('$', quoted);
                    self.pos += 1;
                }
            }
            Ok(())
        }
    }

    fn finish(commands: &mut Vec<Command>, current: &mut Command) {
        let command = std::mem::take(current);
        if !command.is_empty() {
            commands.push(command);
        }
    }

    fn is_name(text: &str) -> bool {
        let mut chars = text.chars();
        chars
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
    }

    // ── Judge ──

    #[derive(Clone)]
    enum Binding {
        Value(String),
        Unproven(BTreeSet<String>),
        Opaque,
    }

    #[derive(Clone)]
    struct State {
        cwd: Option<PathBuf>,
        vars: HashMap<String, Binding>,
        home: PathBuf,
    }

    /// One delete operand: candidates None when it cannot be resolved.
    struct Target {
        operand: String,
        paths: Option<Vec<PathBuf>>,
        unproven: BTreeSet<String>,
    }

    #[derive(Default)]
    struct Verdicts {
        dangerous: bool,
        targets: Vec<Target>,
    }

    /// Fields of one expanded word (`None`: opaque) plus unproven names.
    fn expand(word: &Word, state: &State) -> Option<(Vec<String>, BTreeSet<String>)> {
        let mut unproven = BTreeSet::new();
        let mut fields: Vec<String> = vec![String::new()];
        for (index, seg) in word.segs.iter().enumerate() {
            match seg {
                Seg::Lit(text, quoted) => {
                    let mut text = text.clone();
                    if index == 0 && !quoted && (text == "~" || text.starts_with("~/")) {
                        text = format!("{}{}", state.home.display(), &text[1..]);
                    } else if index == 0 && !quoted && text.starts_with('~') {
                        return None;
                    }
                    let options = if *quoted {
                        vec![text]
                    } else {
                        brace_expand(&text)
                    };
                    fields = fields
                        .iter()
                        .flat_map(|head| options.iter().map(move |tail| format!("{head}{tail}")))
                        .collect();
                }
                Seg::Var(name) => {
                    let value = match state.vars.get(name) {
                        Some(Binding::Value(value)) => value.clone(),
                        Some(Binding::Unproven(names)) => {
                            unproven.extend(names.iter().cloned());
                            String::new()
                        }
                        Some(Binding::Opaque) => return None,
                        None if is_name(name) => match std::env::var(name) {
                            Ok(value) => value,
                            Err(_) => {
                                unproven.insert(name.clone());
                                String::new()
                            }
                        },
                        None => {
                            unproven.insert(name.clone());
                            String::new()
                        }
                    };
                    for field in &mut fields {
                        field.push_str(&value);
                    }
                }
                Seg::Opaque => return None,
            }
        }
        Some((fields, unproven))
    }

    /// Bash brace expansion of unquoted literal text (`a{b,c}` → `ab`, `ac`).
    fn brace_expand(text: &str) -> Vec<String> {
        let chars: Vec<char> = text.chars().collect();
        let Some(open) = chars.iter().position(|&c| c == '{') else {
            return vec![text.to_string()];
        };
        let mut depth = 0;
        let mut commas = Vec::new();
        let mut close = None;
        for (i, &c) in chars.iter().enumerate().skip(open) {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        close = Some(i);
                        break;
                    }
                }
                ',' if depth == 1 => commas.push(i),
                _ => {}
            }
        }
        let Some(close) = close.filter(|_| !commas.is_empty()) else {
            return vec![text.to_string()];
        };
        let prefix: String = chars[..open].iter().collect();
        let suffix: String = chars[close + 1..].iter().collect();
        let mut bounds = vec![open];
        bounds.extend(commas);
        bounds.push(close);
        let mut out = Vec::new();
        for pair in bounds.windows(2) {
            let alternative: String = chars[pair[0] + 1..pair[1]].iter().collect();
            for expanded in brace_expand(&format!("{prefix}{alternative}{suffix}")) {
                out.push(expanded);
            }
        }
        out
    }

    fn single_literal(word: &Word, state: &State) -> Option<String> {
        match expand(word, state) {
            Some((fields, unproven)) if unproven.is_empty() && fields.len() == 1 => {
                fields.into_iter().next()
            }
            _ => None,
        }
    }

    fn basename(program: &str) -> &str {
        program.rsplit(['/', '\\']).next().unwrap_or(program)
    }

    fn is_assignment(word: &Word) -> Option<(String, Word)> {
        let Some(Seg::Lit(first, false)) = word.segs.first() else {
            return None;
        };
        let (name, rest) = first.split_once('=')?;
        if !is_name(name) {
            return None;
        }
        let mut value = Word {
            segs: word.segs[1..].to_vec(),
            raw: String::new(),
        };
        if !rest.is_empty() {
            value.segs.insert(0, Seg::Lit(rest.to_string(), false));
        }
        Some((name.to_string(), value))
    }

    fn bind(state: &mut State, name: String, value: &Word) {
        let binding = match expand(value, state) {
            None => Binding::Opaque,
            Some((_, unproven)) if !unproven.is_empty() => Binding::Unproven(unproven),
            Some((fields, _)) => Binding::Value(fields.join(" ")),
        };
        state.vars.insert(name, binding);
    }

    const KEYWORDS: &[&str] = &[
        "!", "{", "}", "then", "else", "elif", "do", "if", "while", "until", "done", "fi", "esac",
        "in",
    ];

    fn judge_script(commands: &[Command], state: &mut State, verdicts: &mut Verdicts) {
        for command in commands {
            for script in &command.nested {
                let mut scoped = state.clone();
                judge_script(script, &mut scoped, verdicts);
            }
            if command.subshell {
                continue;
            }
            for (op, target) in &command.redirects {
                if op.contains('>') && target.literal().is_some_and(|t| t.starts_with("/dev/sd")) {
                    verdicts.dangerous = true;
                }
            }
            judge_command(command, state, verdicts);
        }
    }

    fn judge_text(text: &str, state: &mut State, verdicts: &mut Verdicts) {
        match parse(text) {
            Ok(script) => judge_script(&script, state, verdicts),
            Err(Unknown) => {
                if raw_dangerous(text) {
                    verdicts.dangerous = true;
                }
                if could_run_delete(text) {
                    verdicts.targets.push(Target {
                        operand: excerpt(text),
                        paths: None,
                        unproven: BTreeSet::new(),
                    });
                }
            }
        }
    }

    fn excerpt(text: &str) -> String {
        text.chars().take(40).collect()
    }

    fn opaque(operand: &str, verdicts: &mut Verdicts) {
        verdicts.targets.push(Target {
            operand: operand.to_string(),
            paths: None,
            unproven: BTreeSet::new(),
        });
    }

    fn judge_command(command: &Command, state: &mut State, verdicts: &mut Verdicts) {
        let mut words: &[Word] = &command.words;
        while let Some(first) = words.first().and_then(Word::unquoted_literal) {
            if KEYWORDS.contains(&first) {
                words = &words[1..];
            } else {
                break;
            }
        }
        if let Some(first) = words.first().and_then(Word::unquoted_literal) {
            if matches!(first, "for" | "select") {
                if let Some(name) = words.get(1).and_then(Word::unquoted_literal) {
                    state.vars.insert(
                        name.to_string(),
                        Binding::Unproven(BTreeSet::from([name.to_string()])),
                    );
                }
                return;
            }
            if first == "function" {
                return;
            }
        }
        let assignments = words
            .iter()
            .take_while(|w| is_assignment(w).is_some())
            .count();
        if assignments == words.len() {
            for word in words {
                if let Some((name, value)) = is_assignment(word) {
                    bind(state, name, &value);
                }
            }
            return;
        }
        judge_program(&words[assignments..], command, state, verdicts);
    }

    /// Judge `words` (program first) after unwrapping wrapper programs.
    fn judge_program(
        words: &[Word],
        command: &Command,
        state: &mut State,
        verdicts: &mut Verdicts,
    ) {
        let Some(program) = words.first().and_then(|w| single_literal(w, state)) else {
            return;
        };
        let name = basename(&program).to_string();
        let args = &words[1..];
        let literal_args: Vec<Option<String>> =
            args.iter().map(|w| single_literal(w, state)).collect();
        let arg = |i: usize| literal_args.get(i).cloned().flatten().unwrap_or_default();
        let skip_options = |takes_value: &[&str]| -> usize {
            let mut i = 0;
            while i < args.len() {
                let a = arg(i);
                if a == "--" {
                    return i + 1;
                }
                if !a.starts_with('-') || a == "-" {
                    break;
                }
                i += if takes_value.contains(&a.as_str()) {
                    2
                } else {
                    1
                };
            }
            i
        };
        match name.as_str() {
            "sudo" | "doas" => {
                let i = skip_options(&["-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"]);
                judge_program(&args[i.min(args.len())..], command, state, verdicts);
            }
            "env" => {
                let mut i = 0;
                while i < args.len() {
                    let a = arg(i);
                    if a == "-C" || a.starts_with("--chdir") {
                        state.cwd = None;
                    }
                    if a == "-u" || a == "-C" || a == "-S" {
                        i += 2;
                    } else if a.starts_with('-') || is_assignment(&args[i]).is_some() {
                        i += 1;
                    } else {
                        break;
                    }
                }
                judge_program(&args[i.min(args.len())..], command, state, verdicts);
            }
            "command" | "builtin" | "exec" | "nohup" | "time" | "busybox" | "chrt" | "setsid" => {
                let i = skip_options(&[]);
                judge_program(&args[i.min(args.len())..], command, state, verdicts);
            }
            "nice" | "ionice" | "stdbuf" => {
                let i = skip_options(&["-n", "-c", "-p", "-i", "-o", "-e"]);
                judge_program(&args[i.min(args.len())..], command, state, verdicts);
            }
            "timeout" => {
                let i = skip_options(&["-s", "-k", "--signal", "--kill-after"]);
                let i = (i + 1).min(args.len());
                judge_program(&args[i..], command, state, verdicts);
            }
            "xargs" | "parallel" => {
                let deletes = literal_args
                    .iter()
                    .flatten()
                    .any(|a| DELETE_PROGRAMS.contains(&basename(a)));
                if deletes {
                    opaque(
                        &words
                            .iter()
                            .map(|w| w.raw.as_str())
                            .collect::<Vec<_>>()
                            .join(" "),
                        verdicts,
                    );
                }
            }
            "find" => {
                let runs_delete = literal_args.windows(2).any(|pair| {
                    matches!(
                        pair[0].as_deref(),
                        Some("-exec" | "-execdir" | "-ok" | "-okdir")
                    ) && pair[1]
                        .as_deref()
                        .is_some_and(|p| DELETE_PROGRAMS.contains(&basename(p)))
                });
                if runs_delete {
                    opaque(
                        &words
                            .iter()
                            .map(|w| w.raw.as_str())
                            .collect::<Vec<_>>()
                            .join(" "),
                        verdicts,
                    );
                }
            }
            "eval" => {
                let text = args
                    .iter()
                    .map(|w| single_literal(w, state).unwrap_or_else(|| w.raw.clone()))
                    .collect::<Vec<_>>()
                    .join(" ");
                judge_text(&text, state, verdicts);
            }
            shell if SHELLS.contains(&shell) => {
                let flag = literal_args.iter().position(|a| {
                    a.as_deref().is_some_and(|a| {
                        a.starts_with('-') && !a.starts_with("--") && a.contains('c')
                    })
                });
                match flag {
                    Some(i) => {
                        if let Some(script) = args.get(i + 1) {
                            match single_literal(script, state) {
                                Some(text) => judge_text(&text, &mut state.clone(), verdicts),
                                None => {
                                    if could_run_delete(&script.raw) || raw_dangerous(&script.raw) {
                                        opaque(&script.raw, verdicts);
                                    }
                                }
                            }
                        }
                    }
                    None => {
                        for body in &command.heredocs {
                            judge_text(body, &mut state.clone(), verdicts);
                        }
                    }
                }
            }
            "cd" | "pushd" => {
                let i = skip_options(&[]);
                match args.get(i) {
                    None => state.cwd = Some(state.home.clone()),
                    Some(word) => match single_literal(word, state) {
                        Some(dir) if dir != "-" => {
                            let path = PathBuf::from(&dir);
                            state.cwd = if path.is_absolute() {
                                Some(normpath(&path))
                            } else {
                                state.cwd.as_ref().map(|c| normpath(&c.join(&path)))
                            };
                        }
                        _ => state.cwd = None,
                    },
                }
            }
            "popd" => state.cwd = None,
            "export" | "local" | "declare" | "typeset" | "readonly" => {
                for word in args {
                    if let Some((name, value)) = is_assignment(word) {
                        bind(state, name, &value);
                    }
                }
            }
            "unset" => {
                for name in literal_args.iter().flatten().filter(|a| is_name(a)) {
                    state
                        .vars
                        .insert(name.clone(), Binding::Value(String::new()));
                }
            }
            "read" | "mapfile" | "readarray" | "getopts" => {
                for name in literal_args.iter().flatten().filter(|a| is_name(a)) {
                    state.vars.insert(
                        name.clone(),
                        Binding::Unproven(BTreeSet::from([name.clone()])),
                    );
                }
            }
            delete if DELETE_PROGRAMS.contains(&delete) => {
                let mut options_done = false;
                for (i, word) in args.iter().enumerate() {
                    let literal = literal_args[i].as_deref();
                    if !options_done && literal == Some("--") {
                        options_done = true;
                        continue;
                    }
                    if !options_done && literal.is_some_and(|a| a.starts_with('-') && a.len() > 1) {
                        continue;
                    }
                    verdicts.targets.push(resolve_operand(word, state));
                }
            }
            _ => {
                if dangerous_program(&name, &literal_args) {
                    verdicts.dangerous = true;
                }
            }
        }
    }

    /// The program-level rules of `command_analysis.dangerous_command_rule`.
    fn dangerous_program(name: &str, args: &[Option<String>]) -> bool {
        let arg = |i: usize| args.get(i).cloned().flatten().unwrap_or_default();
        match name {
            "shutdown" | "reboot" | "diskpart" | "mkfs" => true,
            n if n.starts_with("mkfs.") => true,
            "systemctl" => args.iter().flatten().any(|a| a == "reboot"),
            "init" => arg(0) == "0",
            "dd" => args.iter().flatten().any(|a| a.starts_with("of=/")),
            "format" => {
                let a = arg(0);
                a.len() >= 2 && a.as_bytes()[0].is_ascii_alphabetic() && a.as_bytes()[1] == b':'
            }
            "rd" | "del" => arg(0) == "/s" && arg(1) == "/q",
            "chmod" => {
                let recursive = args
                    .iter()
                    .flatten()
                    .position(|a| a.starts_with('-') && !a.starts_with("--") && a.contains('R'));
                recursive.is_some_and(|r| {
                    args.iter()
                        .flatten()
                        .skip(r + 1)
                        .any(|a| matches!(a.as_str(), "/" | "/*" | "/."))
                })
            }
            _ => false,
        }
    }

    fn resolve_operand(word: &Word, state: &State) -> Target {
        let operand = word.raw.clone();
        let Some((fields, unproven)) = expand(word, state) else {
            return Target {
                operand,
                paths: None,
                unproven: BTreeSet::new(),
            };
        };
        let mut paths = Vec::new();
        for field in fields.into_iter().filter(|f| !f.is_empty()) {
            let path = PathBuf::from(&field);
            if path.is_absolute() {
                paths.push(normpath(&path));
            } else if let Some(cwd) = &state.cwd {
                paths.push(normpath(&cwd.join(path)));
            } else {
                return Target {
                    operand,
                    paths: None,
                    unproven: BTreeSet::new(),
                };
            }
        }
        Target {
            operand,
            paths: Some(paths),
            unproven,
        }
    }

    fn depth(path: &Path) -> usize {
        path.components()
            .filter(|c| matches!(c, std::path::Component::Normal(_)))
            .count()
    }

    /// The Git metadata of the checkout holding `cwd`, if any.
    fn git_metadata(cwd: &Path) -> Option<PathBuf> {
        cwd.ancestors()
            .map(|dir| dir.join(".git"))
            .find(|marker| marker.exists())
    }

    /// `_is_catastrophic_delete`: the depth rule and the anchor rule (home,
    /// the command's cwd, the checkout's Git metadata, or an ancestor).
    fn catastrophic(path: &Path, cwd: &Path, home: &Path, git: Option<&Path>) -> bool {
        let real = realpath(path);
        [path, real.as_path()].iter().any(|p| {
            depth(p) < MIN_DELETE_DEPTH
                || home.starts_with(p)
                || cwd.starts_with(p)
                || git.is_some_and(|g| g.starts_with(p))
        })
    }

    /// Refuse a command the Python node's project run refuses.
    pub(super) fn check(command: &str, cwd: &Path, root: &Path, home: &Path) -> Result<(), String> {
        static FORK: OnceLock<regex::Regex> = OnceLock::new();
        let mut state = State {
            cwd: Some(cwd.to_path_buf()),
            vars: HashMap::new(),
            home: home.to_path_buf(),
        };
        let mut verdicts = Verdicts::default();
        judge_text(command, &mut state, &mut verdicts);
        if verdicts.dangerous || regex(&FORK, FORK_BOMB).is_match(command) {
            return Err("command blocked by dangerous-pattern guard".into());
        }
        let git = git_metadata(cwd);
        for target in &verdicts.targets {
            if !target.unproven.is_empty() {
                continue;
            }
            for path in target.paths.iter().flatten() {
                if catastrophic(path, cwd, home, git.as_deref()) {
                    return Err(format!(
                        "command blocked: catastrophic delete target {}",
                        py_repr(&target.operand)
                    ));
                }
            }
        }
        for target in &verdicts.targets {
            let Some(paths) = &target.paths else {
                return Err(format!(
                    "command blocked: unresolvable delete target {}",
                    py_repr(&target.operand)
                ));
            };
            if !target.unproven.is_empty() {
                let names: Vec<String> = target.unproven.iter().map(|n| format!("${n}")).collect();
                return Err(format!(
                    "command blocked: delete target {} depends on {}, whose value cannot be proven inside the share root",
                    py_repr(&target.operand),
                    names.join(", ")
                ));
            }
            if paths.iter().any(|p| !is_within(root, &realpath(p))) {
                return Err(format!(
                    "command blocked: delete target outside share root: {}",
                    py_repr(&target.operand)
                ));
            }
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::check;
        use std::path::Path;

        fn verdict(command: &str, cwd: &Path, root: &Path) -> Result<(), String> {
            check(command, cwd, root, Path::new("/home/someone"))
        }

        #[test]
        fn delete_targets_stay_inside_the_root() {
            let tmp = tempfile::tempdir().unwrap();
            let root = super::realpath(tmp.path());
            let cwd = root.join("work");
            std::fs::create_dir_all(&cwd).unwrap();
            #[cfg(unix)]
            {
                let outside = tempfile::tempdir().unwrap();
                std::os::unix::fs::symlink(outside.path(), cwd.join("link")).unwrap();
                let error = verdict("rm -rf link/data", &cwd, &root).unwrap_err();
                assert!(error.contains("outside share root: 'link/data'"), "{error}");
            }
            for ok in [
                "rm -rf build",
                "rm -f a.txt b.txt && ls",
                "X=out; rm -rf \"$X\"/cache",
                "rm -- -weird",
                "cd sub && rm x",
                "rm -rf {a,b}/tmp",
                "echo rm -rf /",
                "git rm file",
                "echo 'shutdown'",
            ] {
                assert_eq!(verdict(ok, &cwd, &root), Ok(()), "{ok}");
            }
            let refusals = [
                ("rm -rf /", "catastrophic"),
                ("rm -rf .", "catastrophic"),
                ("rm -rf ..", "catastrophic"),
                ("rm -rf ../../elsewhere", "outside share root"),
                ("rm -rf /etc/passwd.d/x", "outside share root"),
                ("sudo rm -rf ~/old", "outside share root"),
                ("cd /tmp && rm -rf zzz-not-root/x", "outside share root"),
                (
                    "rm -rf $UNSET_TOFU_TEST_VAR/x",
                    "depends on $UNSET_TOFU_TEST_VAR",
                ),
                ("rm -rf $(cat list)", "unresolvable"),
                ("find . -exec rm {} \\;", "unresolvable"),
                ("ls | xargs rm", "unresolvable"),
                ("bash -c 'rm -rf ../../x'", "outside share root"),
                ("echo $(rm -rf /etc/x)", "outside share root"),
                ("rm -rf 'unterminated", "unresolvable"),
                ("for f in *; do rm $f; done", "depends on $f"),
            ];
            for (command, expected) in refusals {
                let error = verdict(command, &cwd, &root).expect_err(command);
                assert!(error.contains(expected), "{command}: {error}");
            }
        }

        #[test]
        fn dangerous_programs_are_refused_but_quoted_data_is_not() {
            let tmp = tempfile::tempdir().unwrap();
            let root = super::realpath(tmp.path());
            for bad in [
                "shutdown -h now",
                "sudo reboot",
                "mkfs.ext4 /dev/sdb",
                "dd if=/dev/zero of=/dev/sda",
                "echo x > /dev/sda",
                ":(){ :|:& };:",
                "chmod -R 777 /",
                "sh -c 'reboot'",
            ] {
                let error = verdict(bad, &root, &root).expect_err(bad);
                assert!(error.contains("dangerous-pattern"), "{bad}: {error}");
            }
            for ok in [
                "git commit -m 'fix reboot handling'",
                "grep shutdown log.txt",
                "dd if=a of=b",
            ] {
                assert_eq!(verdict(ok, &root, &root), Ok(()), "{ok}");
            }
        }
    }
}

// ── Small portable primitives ───────────────────────────────────────

/// MD5 hex digest (RFC 1321), for the snapshot directory name the Python
/// node derives with `hashlib.md5(abspath)`.
fn md5_hex(input: &[u8]) -> String {
    const SHIFTS: [u32; 64] = [
        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5,
        9, 14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10,
        15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
    ];
    let constants: Vec<u32> = (0..64)
        .map(|i| ((i as f64 + 1.0).sin().abs() * 4_294_967_296.0) as u32)
        .collect();
    let mut state: [u32; 4] = [0x6745_2301, 0xefcd_ab89, 0x98ba_dcfe, 0x1032_5476];
    let mut message = input.to_vec();
    let bit_length = (input.len() as u64).wrapping_mul(8);
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_length.to_le_bytes());
    for chunk in message.chunks(64) {
        let words: Vec<u32> = chunk
            .chunks(4)
            .map(|w| u32::from_le_bytes([w[0], w[1], w[2], w[3]]))
            .collect();
        let [mut a, mut b, mut c, mut d] = state;
        for i in 0..64 {
            let (f, g) = match i / 16 {
                0 => ((b & c) | (!b & d), i),
                1 => ((d & b) | (!d & c), (5 * i + 1) % 16),
                2 => (b ^ c ^ d, (3 * i + 5) % 16),
                _ => (c ^ (b | !d), (7 * i) % 16),
            };
            let rotated = f
                .wrapping_add(a)
                .wrapping_add(constants[i])
                .wrapping_add(words[g]);
            a = d;
            d = c;
            c = b;
            b = b.wrapping_add(rotated.rotate_left(SHIFTS[i]));
        }
        for (slot, value) in state.iter_mut().zip([a, b, c, d]) {
            *slot = slot.wrapping_add(value);
        }
    }
    state
        .iter()
        .flat_map(|w| w.to_le_bytes())
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Standard base64 with padding.
pub(crate) fn base64_encode(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | (*chunk.get(1).unwrap_or(&0) as u32) << 8
            | *chunk.get(2).unwrap_or(&0) as u32;
        out.push(ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// Local wall-clock `(year, month, day, hour, minute, second)` of a Unix time
/// (Python `time.localtime`). Windows renders UTC: the platform zone API is
/// not among this build's bindings.
pub(crate) fn local_time(secs: i64) -> (i64, u32, u32, u32, u32, u32) {
    #[cfg(unix)]
    {
        let time = secs as libc::time_t;
        // SAFETY: localtime_r only writes the caller-owned `tm`.
        let mut tm: libc::tm = unsafe { std::mem::zeroed() };
        if !unsafe { libc::localtime_r(&time, &mut tm) }.is_null() {
            return (
                tm.tm_year as i64 + 1900,
                tm.tm_mon as u32 + 1,
                tm.tm_mday as u32,
                tm.tm_hour as u32,
                tm.tm_min as u32,
                tm.tm_sec as u32,
            );
        }
    }
    utc_time(secs)
}

fn utc_time(secs: i64) -> (i64, u32, u32, u32, u32, u32) {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (
        year,
        month,
        day,
        (rem / 3600) as u32,
        (rem % 3600 / 60) as u32,
        (rem % 60) as u32,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    struct Fixture {
        _tmp: tempfile::TempDir,
        root: PathBuf,
        outside: PathBuf,
        roots: Vec<Root>,
    }

    fn fixture() -> Fixture {
        let tmp = tempfile::tempdir().unwrap();
        let base = realpath(tmp.path());
        let root = base.join("app");
        let outside = base.join("app-secrets");
        fs::create_dir_all(root.join("src")).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::write(root.join("README.md"), "# app\nhello world\n").unwrap();
        fs::write(root.join("src").join("main.py"), "print('hello world')\n").unwrap();
        fs::write(outside.join("secret.txt"), "top secret\n").unwrap();
        let roots = vec![Root {
            name: "app".into(),
            path: root.clone(),
        }];
        Fixture {
            _tmp: tmp,
            root,
            outside,
            roots,
        }
    }

    fn rel(text: &str) -> Value {
        json!(text)
    }

    #[test]
    fn resolve_refuses_every_escape() {
        let fx = fixture();
        let name = json!("app");
        let resolve = |path: &str| resolve_in(&fx.roots, Some(&name), Some(&rel(path)));
        assert!(resolve("README.md").is_ok());
        assert_eq!(resolve(".").unwrap().1, fx.root);
        assert!(resolve("../app-secrets/secret.txt")
            .unwrap_err()
            .contains("escapes share root"));
        assert!(resolve("src/../../app-secrets")
            .unwrap_err()
            .contains("escapes share root"));
        let absolute = fx.outside.join("secret.txt").display().to_string();
        assert!(resolve(&absolute).unwrap_err().contains("root-relative"));
        assert!(resolve("C:\\x").unwrap_err().contains("root-relative"));
        assert!(
            resolve_in(&fx.roots, Some(&json!("other")), Some(&rel("x")))
                .unwrap_err()
                .contains("unknown share root 'other'")
        );
        assert!(resolve_in(&[], Some(&name), Some(&rel("x")))
            .unwrap_err()
            .contains("no share_roots declared"));
        // A missing tail still resolves inside the root (new files).
        assert_eq!(
            resolve("new/a.txt").unwrap().1,
            fx.root.join("new").join("a.txt")
        );
    }

    #[cfg(unix)]
    #[test]
    fn symlinks_cannot_leave_the_root() {
        let fx = fixture();
        std::os::unix::fs::symlink(&fx.outside, fx.root.join("escape")).unwrap();
        std::os::unix::fs::symlink("../app-secrets/secret.txt", fx.root.join("rel-escape"))
            .unwrap();
        std::os::unix::fs::symlink("README.md", fx.root.join("inside")).unwrap();
        let name = json!("app");
        let resolve = |path: &str| resolve_in(&fx.roots, Some(&name), Some(&rel(path)));
        assert!(resolve("escape/secret.txt")
            .unwrap_err()
            .contains("escapes share root"));
        assert!(resolve("rel-escape")
            .unwrap_err()
            .contains("escapes share root"));
        assert_eq!(resolve("inside").unwrap().1, fx.root.join("README.md"));
        // Desktop reads: the same containment over absolute / `~` / `..` paths.
        let shared = |path: &str| shared_path_in(&fx.roots, Some(&json!(path)));
        assert!(
            shared(&fx.root.join("escape/secret.txt").display().to_string())
                .unwrap_err()
                .contains("outside the shared folders")
        );
        assert!(shared(&format!("{}/../app-secrets/secret.txt", fx.root.display())).is_err());
        assert!(shared(&fx.outside.display().to_string()).is_err());
        assert!(shared("~/.ssh/id_rsa").is_err());
        assert!(shared("").is_err());
        assert_eq!(
            shared(&fx.root.join("inside").display().to_string()).unwrap(),
            fx.root.join("README.md")
        );
        assert!(shared_path_in(&[], Some(&json!("/")))
            .unwrap_err()
            .contains("no shared folders"));
    }

    #[test]
    fn freshness_gate_and_snapshots() {
        let fx = fixture();
        let target = fx.root.join("README.md");
        assert!(check_write_allowed(&target)
            .unwrap_err()
            .contains("read-before-write"));
        assert!(check_write_allowed(&fx.root.join("new.txt")).is_ok());
        stamp_read(&target);
        assert!(check_write_allowed(&target).is_ok());
        fs::write(&target, "# app\nhello world\nEXTERNALLY EDITED\n").unwrap();
        assert!(check_write_allowed(&target)
            .unwrap_err()
            .contains("changed on disk"));
        stamp_read(&target);
        let snap = snapshot(&fx.root, &target).unwrap().unwrap();
        assert!(snap.starts_with(fx.root.join(".tofu").join("file-history")));
        assert_eq!(
            fs::read_to_string(snap).unwrap(),
            "# app\nhello world\nEXTERNALLY EDITED\n"
        );
        atomic_write(&target, "v2").unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "v2");
        let names: BTreeSet<String> = fs::read_dir(&fx.root)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            names,
            BTreeSet::from([".tofu".into(), "README.md".into(), "src".into()]),
            "no temp file left behind"
        );
    }

    #[test]
    fn md5_and_base64_known_vectors() {
        assert_eq!(md5_hex(b""), "d41d8cd98f00b204e9800998ecf8427e");
        assert_eq!(md5_hex(b"abc"), "900150983cd24fb0d6963f7d28e17f72");
        assert_eq!(md5_hex(&[b'a'; 100]), "36a92cc94a9e0fa21f625f8bfb007adf");
        assert_eq!(base64_encode(b"Man"), "TWFu");
        assert_eq!(base64_encode(b"Ma"), "TWE=");
        assert_eq!(base64_encode(b"M"), "TQ==");
    }

    #[test]
    fn python_reprs() {
        assert_eq!(py_repr("a'b"), "\"a'b\"");
        assert_eq!(py_repr("x\ny"), "'x\\ny'");
        assert_eq!(py_repr_value(None), "None");
        assert_eq!(thousands(1_234_567), "1,234,567");
        assert!(local_time(0).0 >= 1969);
        assert_eq!(utc_time(0), (1970, 1, 1, 0, 0, 0));
        assert_eq!(utc_time(951_782_400 + 3_723), (2000, 2, 29, 1, 2, 3));
        assert_eq!(utc_time(-1), (1969, 12, 31, 23, 59, 59));
    }

    #[test]
    fn receipt_paths_are_root_relative() {
        assert_eq!(receipt_path(Some(&json!("./a//b.txt"))).unwrap(), "a/b.txt");
        assert_eq!(receipt_path(Some(&json!("a\\b"))).unwrap(), "a/b");
        for bad in ["/abs", "../x", ".", "c:/x"] {
            assert!(receipt_path(Some(&json!(bad))).is_err(), "{bad}");
        }
    }

    #[test]
    fn globs_follow_the_ripgrep_dialect() {
        let glob = compile_glob("*.py", false).unwrap();
        assert!(glob.is_match("main.py") && glob.is_match("src/deep/Main.PY"));
        let anchored = compile_glob("src/*.py", false).unwrap();
        assert!(
            anchored.is_match("src/a.py")
                && !anchored.is_match("x/src/a.py")
                && !anchored.is_match("src/x/a.py")
        );
        assert!(compile_glob("*.{ts,js}", false).unwrap().is_match("a/b.js"));
        assert!(compile_glob("a**b", false).is_ok());
        for bad in [
            "", "!x", "#x", "x ", "dir/", "{a}", "x{,.js}", "[ab", "{a,b", "a}",
        ] {
            assert!(compile_glob(bad, false).is_err(), "{bad}");
        }
        assert!(
            fnmatch("main.py", "*.py")
                && fnmatch("a", "[!b]")
                && !fnmatch("b", "[!b]")
                && fnmatch("x/y", "x*")
        );
    }

    #[test]
    fn grep_output_matches_the_python_text() {
        let fx = fixture();
        fs::create_dir_all(fx.root.join("node_modules")).unwrap();
        fs::write(fx.root.join("node_modules").join("dep.js"), "hello world").unwrap();
        fs::create_dir_all(fx.root.join(".tofu")).unwrap();
        fs::write(fx.root.join(".tofu").join("snap"), "hello world").unwrap();
        fs::write(fx.root.join("ignored.log"), "hello world").unwrap();
        fs::write(fx.root.join(".gitignore"), "*.log\n").unwrap();
        fs::write(fx.root.join("bin.dat"), b"hello world\0").unwrap();
        let out = tool_grep(&fx.root, "HELLO world", None, 0, 50);
        assert_eq!(out, "grep \"HELLO world\" \u{2014} 2 matches:\n\nREADME.md\n2:hello world\nsrc/main.py\n1:print('hello world')");
        let single = tool_grep(&fx.root.join("README.md"), "hello", None, 0, 50);
        assert_eq!(
            single,
            "grep \"hello\" \u{2014} 1 matches:\n\n2:hello world"
        );
        let included = tool_grep(&fx.root, "hello", Some("*.py"), 0, 50);
        assert!(
            included.starts_with("grep \"hello\" (*.py) \u{2014} 1 matches:")
                && !included.contains("README")
        );
        assert!(tool_grep(&fx.root, "nothing-here", None, 0, 50)
            .starts_with("No matches found for: nothing-here\nHint:"));
        assert!(tool_grep(&fx.root, "(", None, 0, 50).starts_with("Invalid pattern:"));
        assert!(tool_grep(&fx.root, "x", Some("{a}"), 0, 50)
            .starts_with("Invalid pattern: `include` glob '{a}'"));
        assert_eq!(
            tool_grep(&fx.root.join("missing"), "x", None, 0, 50),
            "Path not found: .. Nothing was searched."
        );
    }

    #[test]
    fn grep_cap_and_context_report_withheld_matches() {
        let fx = fixture();
        let lines: String = (1..=10).map(|i| format!("match {i}\nother\n")).collect();
        fs::write(fx.root.join("many.txt"), lines).unwrap();
        let out = tool_grep(&fx.root.join("many.txt"), "match", None, 0, 3);
        assert_eq!(
            out,
            "grep \"match\" \u{2014} 3 matches:\n\n1:match 1\n3:match 2\n5:match 3\n\u{2026} (3 shown \u{2014} 7 more matches: this file (7, lines 7\u{2013}19))\n[page with from_line=6]"
        );
        fs::create_dir_all(fx.root.join("two")).unwrap();
        fs::write(fx.root.join("two").join("a.txt"), "x\nx\n").unwrap();
        fs::write(fx.root.join("two").join("b.txt"), "y\nx\n").unwrap();
        assert_eq!(
            tool_grep(&fx.root.join("two"), "x", None, 0, 2),
            "grep \"x\" \u{2014} 2 matches:\n\na.txt\n1:x\n2:x\n\u{2026} (2 shown \u{2014} 1 more matches: b.txt (1, line 2))\n[page with from_line=3]"
        );
        assert_eq!(
            tool_grep(&fx.root.join("two"), "x", None, 1, 50),
            "grep \"x\" \u{2014} 3 matches:\n\na.txt\n1:x\n2:x\n--\nb.txt\n1-y\n2:x"
        );
        let ctx = tool_grep(&fx.root.join("many.txt"), "match (1|2|10)$", None, 1, 50);
        assert_eq!(ctx, "grep \"match (1|2|10)$\" \u{2014} 3 matches:\n\n1:match 1\n2-other\n3:match 2\n4-other\n--\n18-other\n19:match 10\n20-other");
    }

    #[test]
    fn find_files_both_shapes() {
        let fx = fixture();
        fs::write(fx.root.join(".hidden.py"), "x").unwrap();
        fs::create_dir_all(fx.root.join("node_modules")).unwrap();
        fs::write(fx.root.join("node_modules").join("dep.py"), "x").unwrap();
        let out = tool_find_files(&fx.root, &fx.root, "*.py", None, 100, false, false, true);
        assert_eq!(
            out,
            "Files matching \"*.py\" (2 found):\n\n  .hidden.py (1B)\n  src/main.py (21B)"
        );
        let shell = tool_find_files(
            &fx.root,
            &fx.root,
            "*.py",
            Some("."),
            100,
            true,
            true,
            false,
        );
        assert_eq!(shell, "./.hidden.py\n./node_modules/dep.py\n./src/main.py");
        let capped = tool_find_files(&fx.root, &fx.root, "**/*.py", None, 1, false, false, true);
        assert!(
            capped.contains("(1 found)")
                && capped.contains("[results capped at 1; narrow the pattern or path]")
        );
        assert_eq!(
            tool_find_files(&fx.root, &fx.root, "*.rs", None, 100, false, false, true),
            "No files matching: *.rs"
        );
        assert_eq!(
            tool_find_files(
                &fx.root,
                &fx.root,
                "*.rs",
                Some("."),
                100,
                false,
                true,
                false
            ),
            ""
        );
    }

    #[test]
    fn list_dir_rows_hide_ignored_and_keep_shell_entries() {
        let fx = fixture();
        fs::create_dir_all(fx.root.join(".tofu")).unwrap();
        fs::create_dir_all(fx.root.join("node_modules")).unwrap();
        let (rows, truncated, scanned) =
            scan_directory_entries(&fx.root, true, true, true, false).unwrap();
        let names: Vec<&str> = rows.iter().map(|r| r.name.as_str()).collect();
        assert_eq!(names, ["src", "README.md"]);
        assert!(!truncated && scanned == 4);
        let (rows, _, _) = scan_directory_entries(&fx.root, true, false, false, true).unwrap();
        assert_eq!(rows.len(), 4);
    }

    fn declare(fx: &Fixture) {
        set_test_roots(Some(vec![("app".into(), fx.root.clone())]));
    }

    #[test]
    fn commands_read_write_and_refuse_like_python() {
        let fx = fixture();
        declare(&fx);
        let read = read_files(&json!({"root": "app", "path": "README.md"}));
        assert_eq!(
            read,
            json!({"path": "README.md", "content": "# app\nhello world\n", "truncated": false, "size": 18})
        );
        let created = write_file(&json!({"root": "app", "path": "new/a.txt", "content": "fresh"}));
        assert_eq!(created, json!({"path": "new/a.txt", "bytes": 5}));
        let refused = write_file(&json!({"root": "app", "path": "src/main.py", "content": "x"}));
        assert!(refused["error"]
            .as_str()
            .unwrap()
            .contains("read-before-write"));
        let written = write_file(
            &json!({"root": "app", "path": "README.md", "content": "v2", "file_receipts_version": 1}),
        );
        assert_eq!(written["file_receipts_version"], 1);
        assert_eq!(written["output"]["bytes"], 2);
        assert!(written["output"]["snapshot"]
            .as_str()
            .unwrap()
            .contains(".tofu"));
        assert_eq!(written["file_changes"][0]["path"], "README.md");
        assert_eq!(written["file_changes"][0]["action"], "written");
        // Our own write re-armed the stamp.
        assert!(
            write_file(&json!({"root": "app", "path": "README.md", "content": "v3"}))
                .get("error")
                .is_none()
        );
        let escape =
            write_file(&json!({"root": "app", "path": "../app-secrets/x", "content": "x"}));
        assert!(escape["error"]
            .as_str()
            .unwrap()
            .contains("escapes share root"));
        assert!(!fx.outside.join("x").exists());
        let image = fx.root.join("pic.png");
        fs::write(&image, b"\x89PNG\r\n\x1a\n").unwrap();
        let read = read_files(&json!({"root": "app", "path": "pic.png"}));
        assert_eq!(read["media"], ".png");
        assert_eq!(read["base64"], "iVBORw0KGgo=");
        assert!(
            read_files(&json!({"root": "app", "path": "nope.txt"}))["error"]
                .as_str()
                .unwrap()
                .contains("not a file: 'nope.txt'")
        );
        let listed = list_dir(&json!({"root": "app"}));
        assert_eq!(listed["path"], ".");
        let names: Vec<&str> = listed["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, ["new", "src", "pic.png", "README.md"]);
        assert!(listed["entries"][2]["modified"]
            .as_str()
            .unwrap()
            .contains('T'));
        assert!(grep_search(&json!({"root": "app", "pattern": "  "}))["error"].is_string());
        assert!(
            grep_search(&json!({"root": "app", "pattern": "hello"}))["matches"]
                .as_str()
                .unwrap()
                .contains("src/main.py")
        );
        assert!(
            find_files(&json!({"root": "app", "pattern": "*.py"}))["files"]
                .as_str()
                .unwrap()
                .contains("src/main.py")
        );
        set_test_roots(None);
    }

    #[test]
    fn apply_diff_and_edit_file_semantics() {
        let fx = fixture();
        declare(&fx);
        fs::write(fx.root.join("dup.txt"), "aa bb aa").unwrap();
        let diff = |extra: Value| {
            let mut params =
                json!({"root": "app", "path": "dup.txt", "search": "aa", "replace": "zz"});
            params
                .as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            apply_diff(&params)
        };
        assert!(diff(json!({}))["error"]
            .as_str()
            .unwrap()
            .contains("read-before-write"));
        read_files(&json!({"root": "app", "path": "dup.txt"}));
        assert!(diff(json!({}))["error"]
            .as_str()
            .unwrap()
            .contains("matches 2 locations"));
        assert!(diff(json!({"search": "nope"}))["error"]
            .as_str()
            .unwrap()
            .contains("not found"));
        assert_eq!(diff(json!({"replace_all": true}))["replacements"], 2);
        assert_eq!(
            fs::read_to_string(fx.root.join("dup.txt")).unwrap(),
            "zz bb zz"
        );

        let sample = fx.root.join("sample.txt");
        fs::write(&sample, "A\r\nC\r\n").unwrap();
        read_files(&json!({"root": "app", "path": "sample.txt"}));
        let out = edit_file(
            &json!({"root": "app", "edits": [{"path": "sample.txt", "anchor": "A\nC", "content": "A\nB\nC"}]}),
        );
        assert_eq!(out, json!("Applied 1/1 edits\n[1] OK sample.txt [replace]"));
        assert_eq!(fs::read(&sample).unwrap(), b"A\nB\nC\n");
        fs::write(&sample, "A\r\nC\r\n").unwrap();
        read_files(&json!({"root": "app", "path": "sample.txt"}));
        let out = edit_file(&json!({"root": "app", "path": "sample.txt", "edits": [
            {"anchor": "A\r\n", "content": "A\r\nB\r\n"},
            {"anchor": "missing", "content": "x"},
            {"operation": "insert_after", "anchor": "A", "content": "x"},
            {"path": "../app-secrets/secret.txt", "anchor": "top", "content": "x"},
            "not an object",
        ]}));
        let out = out.as_str().unwrap();
        assert!(out.starts_with("Applied 1/5 edits (4 failed)\n[1] OK sample.txt [replace]\n[2] FAIL sample.txt: anchor text not found"), "{out}");
        assert!(out.contains("[3] FAIL sample.txt: `operation` is retired"));
        assert!(out.contains("[4] FAIL ../app-secrets/secret.txt: path escapes share root"));
        assert!(out.contains("[5] FAIL ?: edit must be an object"));
        assert_eq!(fs::read(&sample).unwrap(), b"A\r\nB\r\nC\r\n");
        assert_eq!(
            fs::read_to_string(fx.outside.join("secret.txt")).unwrap(),
            "top secret\n"
        );
        fs::write(&sample, "changed elsewhere").unwrap();
        let stale = edit_file(
            &json!({"root": "app", "edits": [{"path": "sample.txt", "anchor": "changed", "content": "x"}]}),
        );
        assert!(stale.as_str().unwrap().contains("changed on disk"));
        assert_eq!(
            edit_file(&json!({"root": "app", "edits": []})),
            json!({"error": "edits must be a non-empty array"})
        );
        set_test_roots(None);
    }

    #[test]
    fn run_validation_refuses_before_starting() {
        let fx = fixture();
        declare(&fx);
        let refuse = |params: Value| validate_project_run(&params).err().unwrap_or_default();
        assert!(
            refuse(json!({"root": "app", "command": "ls", "workdir": ".."}))
                .contains("escapes share root")
        );
        assert!(
            refuse(json!({"root": "app", "command": "ls", "workdir": "README.md"}))
                .contains("workdir is not a directory: 'README.md'")
        );
        assert!(refuse(json!({"root": "app", "command": "  "}))
            .contains("command must be a non-empty string"));
        assert!(refuse(json!({"root": "app", "command": "shutdown -h now"})).contains("blocked"));
        assert!(refuse(json!({"root": "app", "command": "rm -rf /"})).contains("blocked"));
        assert!(
            refuse(json!({"root": "app", "command": "rm -rf ../app-secrets"}))
                .contains("outside share root")
        );
        assert!(
            refuse(json!({"root": "app", "command": "ls", "timeout": "inf"}))
                .contains("nonnegative and finite")
        );
        let spec = validate_project_run(
            &json!({"root": "app", "command": "rm -rf src/old", "workdir": "src", "timeout": 5}),
        )
        .unwrap();
        assert_eq!((spec.cwd, spec.timeout), (fx.root.join("src"), Some(5.0)));
        set_test_roots(None);
    }

    #[test]
    fn timeouts_parse_like_python() {
        assert_eq!(command_timeout(Some(&json!(1.5))), Ok(Some(1.5)));
        assert_eq!(command_timeout(Some(&json!("2"))), Ok(Some(2.0)));
        assert!(command_timeout(Some(&json!(-1))).is_err());
        assert!(command_timeout(Some(&json!(true))).is_err());
        assert_eq!(command_timeout(None), Ok(None));
    }
}
