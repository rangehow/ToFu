//! Upstream version checks and one-click updates for MCP servers: the port
//! of `lib/mcp/updates.py`.
//!
//! Launch-spec parsing (`parse_package_ref`, npx/uvx, npm tokens, PEP 508-ish
//! `--from` specs), the tolerant version order (`compare_versions`), the
//! arg rewrite (`build_updated_args`), the registry lookup
//! (`fetch_latest_version`, through the shared network path of
//! `network.rs`, cached like `_LATEST_CACHE`), and the check/apply verdicts.
//! Configuration rows stay with Python's config owner: they are read and
//! patched through the binding (`mcp.config_rows`, `mcp.patch_server`).

use crate::bridge::Bridge;
use crate::callback::BindingCalls;
use crate::errors::CallError;
use crate::network::NetworkClient;
use crate::pyjson::py_strip;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::cmp::Ordering;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

/// `_PYPI_JSON_URL`.
pub const PYPI_JSON_URL: &str = "https://pypi.org/pypi/{package}/json";
/// `_NPM_LATEST_URL`.
pub const NPM_LATEST_URL: &str = "https://registry.npmjs.org/{package}/latest";
/// `_LATEST_CACHE = TTLCache(ttl=3600, max_size=256)`.
pub const LATEST_CACHE_TTL: Duration = Duration::from_secs(3600);
pub const LATEST_CACHE_MAX_SIZE: usize = 256;
/// `_FETCH_TIMEOUT`.
const FETCH_TIMEOUT: Duration = Duration::from_secs(10);

/// `_NPM_DIST_TAGS`.
const NPM_DIST_TAGS: &[&str] = &["latest", "next", "beta", "alpha", "canary", "nightly"];
/// `_NPX_VALUE_FLAGS`.
const NPX_VALUE_FLAGS: &[&str] = &["-p", "--package", "-c", "--call"];
/// `_UVX_VALUE_FLAGS`.
const UVX_VALUE_FLAGS: &[&str] = &[
    "--with",
    "--python",
    "--index-url",
    "--default-index",
    "--extra-index-url",
    "--find-links",
    "--index",
    "--exclude-newer",
];

/// Registry URL templates (`{package}` placeholder); the oracle's constants by default.
#[derive(Clone, Debug)]
pub struct Registry {
    pub pypi: String,
    pub npm: String,
}

impl Default for Registry {
    fn default() -> Self {
        Registry {
            pypi: PYPI_JSON_URL.into(),
            npm: NPM_LATEST_URL.into(),
        }
    }
}

// ── Version comparison ──────────────────────────────────────────────

/// A parsed version: release segments as canonical decimal strings (Python
/// ints are unbounded) and the `(_PRE_RANK, number)` pair.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VersionKey {
    pub release: Vec<String>,
    pub pre: (i64, String),
}

fn version_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(\d+(?:\.\d+)*)(.*)$").expect("version regex"))
}

fn pre_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^[-._]?(dev|alpha|a|beta|b|rc|post)[-._]?(\d*)").expect("pre regex")
    })
}

fn decimal_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\d$").expect("digit regex"))
}

/// A Unicode decimal digit (`\d`, category Nd).
fn is_decimal(ch: char) -> bool {
    let mut buffer = [0u8; 4];
    decimal_re().is_match(ch.encode_utf8(&mut buffer))
}

/// The value `int()` gives one Nd digit: Nd digits come in contiguous runs of ten.
fn decimal_value(ch: char) -> u32 {
    if let Some(value) = ch.to_digit(10) {
        return value;
    }
    let mut start = ch as u32;
    while let Some(previous) = start.checked_sub(1).and_then(char::from_u32) {
        if !is_decimal(previous) {
            break;
        }
        start -= 1;
    }
    (ch as u32 - start) % 10
}

/// `int(text)` of a `\d+` match, as a canonical decimal string.
fn int_text(text: &str) -> String {
    let digits: String = text
        .chars()
        .map(|c| char::from_digit(decimal_value(c), 10).unwrap_or('0'))
        .collect();
    let trimmed = digits.trim_start_matches('0');
    if trimmed.is_empty() {
        "0".into()
    } else {
        trimmed.into()
    }
}

fn cmp_int(a: &str, b: &str) -> Ordering {
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

fn pre_rank(tag: &str) -> i64 {
    match tag {
        "dev" => -4,
        "a" | "alpha" => -3,
        "b" | "beta" => -2,
        "rc" => -1,
        "post" => 1,
        _ => 0,
    }
}

/// `str.lstrip('vV')`.
fn lstrip_v(text: &str) -> &str {
    text.trim_start_matches(['v', 'V'])
}

/// `_parse_version`.
pub fn parse_version(value: &str) -> Option<VersionKey> {
    let text = lstrip_v(py_strip(value));
    if text.is_empty() {
        return None;
    }
    let captures = version_re().captures(text)?;
    let release = captures[1].split('.').map(int_text).collect();
    let rest = &captures[2];
    let mut pre = (0, "0".to_string());
    if !rest.is_empty() {
        let found = pre_re().captures(rest)?;
        let number = &found[2];
        pre = (
            pre_rank(&found[1].to_lowercase()),
            if number.is_empty() {
                "0".into()
            } else {
                int_text(number)
            },
        );
    }
    Some(VersionKey { release, pre })
}

/// `compare_versions`: -1/0/1, or `None` when either side is unparseable.
pub fn compare_versions(a: &str, b: &str) -> Option<i32> {
    let key_a = parse_version(a)?;
    let key_b = parse_version(b)?;
    let width = key_a.release.len().max(key_b.release.len());
    let padded = |release: &[String]| {
        let mut out = release.to_vec();
        out.resize(width, "0".into());
        out
    };
    let (rel_a, rel_b) = (padded(&key_a.release), padded(&key_b.release));
    let release = rel_a
        .iter()
        .zip(&rel_b)
        .map(|(x, y)| cmp_int(x, y))
        .find(|o| *o != Ordering::Equal)
        .unwrap_or(Ordering::Equal);
    let ordering = release.then_with(|| {
        key_a
            .pre
            .0
            .cmp(&key_b.pre.0)
            .then_with(|| cmp_int(&key_a.pre.1, &key_b.pre.1))
    });
    Some(match ordering {
        Ordering::Less => -1,
        Ordering::Equal => 0,
        Ordering::Greater => 1,
    })
}

// ── Launch-arg parsing ──────────────────────────────────────────────

/// `PackageRef`: where to check and where to rewrite.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackageRef {
    pub source: String,
    pub package: String,
    pub extras: String,
    pub current: String,
    pub pinned: bool,
    pub arg_index: usize,
}

impl PackageRef {
    pub fn to_value(&self) -> Value {
        json!({"source": self.source, "package": self.package, "extras": self.extras,
               "current": self.current, "pinned": self.pinned, "arg_index": self.arg_index})
    }

    pub fn from_value(value: &Value) -> Option<PackageRef> {
        let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
        Some(PackageRef {
            source: text("source")?,
            package: text("package")?,
            extras: text("extras").unwrap_or_default(),
            current: text("current").unwrap_or_default(),
            pinned: value
                .get("pinned")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            arg_index: value.get("arg_index")?.as_u64()? as usize,
        })
    }
}

/// `str.isdigit()` for one character: Numeric_Type Decimal or Digit.
fn py_isdigit(ch: char) -> bool {
    if is_decimal(ch) {
        return true;
    }
    matches!(ch as u32,
        0xB2 | 0xB3 | 0xB9 | 0x1369..=0x1371 | 0x19DA | 0x2070 | 0x2074..=0x2079
        | 0x2080..=0x2089 | 0x2460..=0x2468 | 0x2474..=0x247C | 0x2488..=0x2490
        | 0x24EA | 0x24F5..=0x24FD | 0x24FF | 0x2776..=0x277E | 0x2780..=0x2788
        | 0x278A..=0x2792 | 0x10A40..=0x10A43 | 0x10E60..=0x10E68 | 0x11052..=0x1105A
        | 0x1F100..=0x1F10A)
}

/// `_parse_npm_token`: `(package, version, pinned)`.
fn parse_npm_token(token: &str) -> Option<(String, String, bool)> {
    if token.is_empty() {
        return None;
    }
    let at = if token.starts_with('@') {
        let slash = token.find('/')?;
        token[slash..].find('@').map(|i| i + slash)
    } else {
        token.find('@')
    };
    let Some(at) = at else {
        return Some((token.into(), String::new(), false));
    };
    let (package, tag) = (&token[..at], &token[at + 1..]);
    if package.is_empty() || tag.is_empty() {
        return None;
    }
    let first_digit = tag.chars().next().is_some_and(py_isdigit);
    if NPM_DIST_TAGS.contains(&tag) || !first_digit {
        return Some((package.into(), String::new(), false));
    }
    Some((package.into(), tag.into(), true))
}

fn from_spec_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        // Python's `\s` also matches U+001C..U+001F.
        Regex::new(concat!(
            r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)",
            r"(\[[^\]]*\])?",
            r"[\s\x1c-\x1f]*(==|>=|<=|~=|!=|>|<)?",
            r"[\s\x1c-\x1f]*([^,;\s\x1c-\x1f]+)?$"
        ))
        .expect("from-spec regex")
    })
}

/// `_parse_pypi_spec`: `(package, extras, version, pinned)`.
fn parse_pypi_spec(spec: &str) -> Option<(String, String, String, bool)> {
    let text = py_strip(spec);
    if text.is_empty() {
        return None;
    }
    if ["/", "./", "../", "~"].iter().any(|p| text.starts_with(p))
        || text.contains('/')
        || [".whl", ".tar.gz", ".zip"]
            .iter()
            .any(|s| text.ends_with(s))
    {
        return None;
    }
    let found = from_spec_re().captures(text)?;
    let group = |i: usize| found.get(i).map(|m| m.as_str()).unwrap_or_default();
    let (package, extras, op, version) = (group(1), group(2), group(3), group(4));
    if op == "==" && !version.is_empty() {
        return Some((package.into(), extras.into(), version.into(), true));
    }
    Some((package.into(), extras.into(), String::new(), false))
}

/// `_first_positional`.
fn first_positional(args: &[String], value_flags: &[&str]) -> Option<usize> {
    let mut skip_next = false;
    for (index, token) in args.iter().enumerate() {
        if skip_next {
            skip_next = false;
            continue;
        }
        if token == "--" {
            continue;
        }
        if value_flags.contains(&token.as_str()) {
            skip_next = true;
            continue;
        }
        if token.starts_with('-') {
            continue;
        }
        return Some(index);
    }
    None
}

/// `list(cfg.get('args') or [])`.
fn config_args(config: &Value) -> Vec<String> {
    match config.get("args") {
        Some(Value::Array(items)) => items.iter().map(crate::pyos::py_str).collect(),
        Some(Value::String(text)) => text.chars().map(String::from).collect(),
        Some(Value::Object(map)) => map.keys().cloned().collect(),
        _ => Vec::new(),
    }
}

/// `parse_package_ref`: `Ok(ref)` or `Err(reason)` (`remote-transport`,
/// `unsupported-launcher`, `local-path`, `no-package`).
pub fn parse_package_ref(config: &Value) -> Result<PackageRef, &'static str> {
    if !crate::transport::is_stdio(config) {
        return Err("remote-transport");
    }
    let command = match config.get("command") {
        Some(value) if crate::pyos::truthy(Some(value)) => crate::pyos::py_str(value),
        _ => String::new(),
    };
    let command = crate::pyos::basename(&command);
    let args = config_args(config);
    match command {
        "npx" => {
            let index = first_positional(&args, NPX_VALUE_FLAGS).ok_or("no-package")?;
            let (package, current, pinned) = parse_npm_token(&args[index]).ok_or("no-package")?;
            Ok(PackageRef {
                source: "npm".into(),
                package,
                extras: String::new(),
                current,
                pinned,
                arg_index: index,
            })
        }
        "uvx" => {
            let (index, parsed) = if let Some(from_at) = args.iter().position(|a| a == "--from") {
                if from_at + 1 >= args.len() {
                    return Err("no-package");
                }
                (from_at + 1, parse_pypi_spec(&args[from_at + 1]))
            } else {
                let index = first_positional(&args, UVX_VALUE_FLAGS).ok_or("no-package")?;
                let token = args[index].strip_suffix("@latest").unwrap_or(&args[index]);
                (index, parse_pypi_spec(token))
            };
            let (package, extras, current, pinned) = parsed.ok_or("local-path")?;
            Ok(PackageRef {
                source: "pypi".into(),
                package,
                extras,
                current,
                pinned,
                arg_index: index,
            })
        }
        _ => Err("unsupported-launcher"),
    }
}

/// `build_updated_args`: pin `latest` in place; a pypi rewrite drops `--exclude-newer`.
pub fn build_updated_args(config: &Value, reference: &PackageRef, latest: &str) -> Vec<String> {
    let mut args = config_args(config);
    if reference.source == "npm" {
        args[reference.arg_index] = format!("{}@{latest}", reference.package);
    } else {
        args[reference.arg_index] = format!("{}{}=={latest}", reference.package, reference.extras);
        while let Some(at) = args.iter().position(|a| a == "--exclude-newer") {
            let end = (at + 2).min(args.len());
            args.drain(at..end);
        }
    }
    args
}

// ── Upstream latest-version lookup ──────────────────────────────────

struct CacheEntry {
    key: (String, String),
    version: String,
    stored: Instant,
}

/// `TTLCache(ttl=3600, max_size=256)` with LRU touch on read; insertion
/// order is the LRU order. `now` is the caller's clock (a test seam).
#[derive(Default)]
pub struct LatestCache {
    entries: Vec<CacheEntry>,
}

impl LatestCache {
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn get(&mut self, key: &(String, String), now: Instant) -> Option<String> {
        let index = self.entries.iter().position(|e| &e.key == key)?;
        if now.duration_since(self.entries[index].stored) >= LATEST_CACHE_TTL {
            self.entries.remove(index);
            return None;
        }
        let entry = self.entries.remove(index);
        let version = entry.version.clone();
        self.entries.push(entry);
        Some(version)
    }

    pub fn set(&mut self, key: (String, String), version: String, now: Instant) {
        self.entries.retain(|e| e.key != key);
        self.entries.push(CacheEntry {
            key,
            version,
            stored: now,
        });
        while self.entries.len() > LATEST_CACHE_MAX_SIZE {
            self.entries.remove(0);
        }
    }

    pub fn invalidate(&mut self, key: &(String, String)) {
        self.entries.retain(|e| &e.key != key);
    }
}

fn latest_cache() -> &'static Mutex<LatestCache> {
    static CACHE: OnceLock<Mutex<LatestCache>> = OnceLock::new();
    CACHE.get_or_init(Default::default)
}

/// Empty `_LATEST_CACHE` (the oracle tests' `_LATEST_CACHE.clear()`).
pub fn clear_latest_cache() {
    latest_cache()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .entries
        .clear();
}

/// `urllib.parse.quote(text, safe='@')`.
fn quote_npm(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || b"_.-~@".contains(&byte) {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// `fetch_latest_version`: the latest published version, or `""` on failure.
pub async fn fetch_latest_version(
    network: &NetworkClient,
    registry: &Registry,
    source: &str,
    package: &str,
    refresh: bool,
) -> String {
    let key = (source.to_string(), package.to_string());
    {
        let mut cache = latest_cache().lock().unwrap_or_else(|p| p.into_inner());
        if refresh {
            cache.invalidate(&key);
        }
        if let Some(cached) = cache.get(&key, Instant::now()) {
            return cached;
        }
    }
    let url = match source {
        "pypi" => registry.pypi.replace("{package}", package),
        "npm" => registry.npm.replace("{package}", &quote_npm(package)),
        _ => return String::new(),
    };
    let Ok(response) = network
        .send(&url, |client| client.get(&url).timeout(FETCH_TIMEOUT))
        .await
    else {
        return String::new();
    };
    if response.status().as_u16() != 200 {
        return String::new();
    }
    let Ok(data) = response.json::<Value>().await else {
        return String::new();
    };
    let Value::Object(data) = data else {
        return String::new();
    };
    let field = if source == "pypi" {
        match data.get("info") {
            Some(info) if crate::pyos::truthy(Some(info)) => match info {
                Value::Object(info) => info.get("version").cloned(),
                _ => return String::new(),
            },
            _ => None,
        }
    } else {
        data.get("version").cloned()
    };
    let version = match field {
        Some(value) if crate::pyos::truthy(Some(&value)) => crate::pyos::py_str(&value),
        _ => String::new(),
    };
    if version.is_empty() || parse_version(&version).is_none() {
        return String::new();
    }
    latest_cache()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .set(key, version.clone(), Instant::now());
    version
}

// ── Check ───────────────────────────────────────────────────────────

/// `check_server_update`.
pub async fn check_server_update(
    network: &NetworkClient,
    registry: &Registry,
    config: &Value,
    live_version: &str,
) -> Value {
    let reference = match parse_package_ref(config) {
        Ok(reference) => reference,
        Err(reason) => return json!({"updatable": false, "reason": reason}),
    };
    let latest = fetch_latest_version(
        network,
        registry,
        &reference.source,
        &reference.package,
        false,
    )
    .await;
    let mut current = reference.current.clone();
    if current.is_empty() && !live_version.is_empty() && parse_version(live_version).is_some() {
        current = lstrip_v(py_strip(live_version)).to_string();
    }
    let update_available = if !latest.is_empty() && !current.is_empty() {
        json!(compare_versions(&latest, &current).is_some_and(|n| n > 0))
    } else {
        Value::Null
    };
    json!({
        "updatable": true,
        "source": reference.source,
        "package": reference.package,
        "pinned": reference.pinned,
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "error": if latest.is_empty() { "lookup-failed" } else { "" },
    })
}

async fn config_rows(
    binding: &dyn BindingCalls,
    owner: Option<u64>,
) -> Result<Map<String, Value>, CallError> {
    match binding
        .ask("mcp.config_rows", json!({"owner_user_id": owner}))
        .await?
    {
        Value::Object(rows) => Ok(rows),
        other => Err(CallError::Error(format!(
            "mcp.config_rows answered a non-object: {other}"
        ))),
    }
}

/// `check_all_updates`: every configured server; failures are per server.
pub async fn check_all_updates(
    network: &NetworkClient,
    registry: &Registry,
    binding: &dyn BindingCalls,
    bridge: &Bridge,
) -> Result<Value, CallError> {
    let config = config_rows(binding, bridge.owner_user_id()).await?;
    let mut live = Map::new();
    for info in bridge.list_servers() {
        let version = info
            .get("server_version")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if let (false, Some(name)) = (version.is_empty(), info.get("name").and_then(Value::as_str))
        {
            live.insert(name.into(), json!(version));
        }
    }
    let checks = config.iter().map(|(name, cfg)| {
        let live_version = live
            .get(name)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        async move {
            // A non-object row raises in the oracle's `cfg.get` and is caught per server.
            let verdict = if cfg.is_object() || cfg.is_null() {
                check_server_update(network, registry, cfg, &live_version).await
            } else {
                json!({"updatable": false, "reason": "check-error"})
            };
            (name.clone(), verdict)
        }
    });
    let pairs = futures::future::join_all(checks).await;
    Ok(Value::Object(pairs.into_iter().collect()))
}

// ── Apply ───────────────────────────────────────────────────────────

/// The `MCPServerNotFound` failure.
pub fn server_not_found(name: &str) -> CallError {
    CallError::Error(format!("MCPServerNotFound: {name}"))
}

/// `apply_update`: pin the stored config to the latest release and reconnect.
pub async fn apply_update(
    network: &NetworkClient,
    registry: &Registry,
    binding: &dyn BindingCalls,
    bridge: &Bridge,
    name: &str,
) -> Result<Value, CallError> {
    let owner = bridge.owner_user_id();
    let config = config_rows(binding, owner).await?;
    let Some(cfg) = config.get(name).filter(|c| c.is_object()).cloned() else {
        return Err(server_not_found(name));
    };
    let reference = parse_package_ref(&cfg)
        .map_err(|reason| CallError::Invalid(format!("BadRequest(field=id): {reason}")))?;
    let latest = fetch_latest_version(
        network,
        registry,
        &reference.source,
        &reference.package,
        true,
    )
    .await;
    if latest.is_empty() {
        return Ok(json!({"updated": false, "error": "lookup-failed",
                         "package": reference.package, "source": reference.source}));
    }
    let current = reference.current.clone();
    if !current.is_empty() && compare_versions(&latest, &current).is_none_or(|n| n <= 0) {
        return Ok(
            json!({"updated": false, "already_latest": true, "version": current,
                         "latest": latest, "package": reference.package,
                         "source": reference.source}),
        );
    }
    let new_args = build_updated_args(&cfg, &reference, &latest);
    let patched = binding
        .ask(
            "mcp.patch_server",
            json!({"owner_user_id": owner, "name": name, "changes": {"args": new_args}}),
        )
        .await?;
    if patched.is_null() {
        return Err(server_not_found(name));
    }
    let mut result = json!({
        "updated": true, "version": latest, "previous": current,
        "package": reference.package, "source": reference.source, "reconnected": false,
    });
    if !crate::pyos::truthy(Some(cfg.get("enabled").unwrap_or(&Value::Bool(true)))) {
        return Ok(result);
    }
    if bridge
        .list_servers()
        .iter()
        .any(|s| s.get("name").and_then(Value::as_str) == Some(name))
    {
        // The oracle logs a failed pre-update disconnect and reconnects anyway.
        let _ = bridge.disconnect_one(name, true, false).await;
    }
    let refreshed = config_rows(binding, owner)
        .await?
        .get(name)
        .cloned()
        .unwrap_or_else(|| json!({}));
    let tools = bridge
        .connect_server(name, refreshed)
        .await
        .map_err(CallError::Error)?;
    result["reconnected"] = json!(true);
    result["tools_count"] = json!(tools.len());
    result["tool_names"] = json!(tools);
    Ok(result)
}

/// Registry templates from a host request (`registry: {pypi, npm}`), else the oracle's.
fn registry_param(params: &Value) -> Registry {
    let mut registry = Registry::default();
    if let Some(given) = params.get("registry") {
        if let Some(pypi) = given.get("pypi").and_then(Value::as_str) {
            registry.pypi = pypi.into();
        }
        if let Some(npm) = given.get("npm").and_then(Value::as_str) {
            registry.npm = npm.into();
        }
    }
    registry
}

/// The host protocol's `updates.<op>` methods.
pub async fn host_call(
    op: &str,
    params: &Value,
    binding: Arc<dyn BindingCalls>,
    bridge: &Bridge,
) -> Result<Value, CallError> {
    let network = crate::network::host_network(binding.clone());
    let registry = registry_param(params);
    let text = |key: &str| {
        params
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    };
    let config = params.get("config").cloned().unwrap_or(Value::Null);
    match op {
        "parse_version" => Ok(parse_version(&text("value"))
            .map(|key| json!([key.release, [key.pre.0, key.pre.1]]))
            .unwrap_or(Value::Null)),
        "compare_versions" => Ok(json!(compare_versions(&text("a"), &text("b")))),
        "parse_package_ref" => Ok(match parse_package_ref(&config) {
            Ok(reference) => json!([reference.to_value(), ""]),
            Err(reason) => json!([null, reason]),
        }),
        "build_updated_args" => {
            let reference = params
                .get("ref")
                .and_then(PackageRef::from_value)
                .ok_or_else(|| {
                    CallError::Invalid("build_updated_args needs a package ref".into())
                })?;
            if reference.arg_index >= config_args(&config).len() {
                return Err(CallError::Invalid(
                    "IndexError: list assignment index out of range".into(),
                ));
            }
            Ok(json!(build_updated_args(
                &config,
                &reference,
                &text("latest")
            )))
        }
        "fetch_latest_version" => Ok(json!(
            fetch_latest_version(
                &network,
                &registry,
                &text("source"),
                &text("package"),
                params.get("refresh") == Some(&Value::Bool(true)),
            )
            .await
        )),
        "check_server_update" => {
            Ok(check_server_update(&network, &registry, &config, &text("live_version")).await)
        }
        "check_all_updates" => {
            check_all_updates(&network, &registry, binding.as_ref(), bridge).await
        }
        "apply_update" => {
            apply_update(&network, &registry, binding.as_ref(), bridge, &text("name")).await
        }
        "clear_cache" => {
            clear_latest_cache();
            Ok(Value::Null)
        }
        _ => Err(CallError::Invalid(format!(
            "unknown host method updates.{op}"
        ))),
    }
}
