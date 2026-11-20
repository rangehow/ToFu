//! MCP credentials that copy the owner's browser session
//! (`lib/mcp/browser_credentials.py`, the Python oracle).
//!
//! A *binding* records which browser cookies an env value copies: one
//! cookie's value (`{kind: cookie, name, url}`) or a Cookie header composed in
//! the user's order (`{kind: cookie_header, names, required, url}`). A
//! catalog `env_specs` row may declare `browser_cookie`; every other server
//! learns its bindings by asking the owner's `cookie_match` devices to compare
//! HMAC-SHA-256 digests under a one-time key, so neither stored values nor
//! cookie values cross the bridge. `refresh_from_browser` copies the browser's
//! current session into the stored row (a `vault:<entry>` value renews its
//! vault entry instead) and audits `mcp_credential_refreshed` by key name.
//!
//! The pure rules (session-likeness, header parsing, matching, the sending
//! URL, renewal values, the model-facing notice) live here. State Python owns
//! is reached through host-to-binding calls (`callback.rs`), answered by the
//! thin handlers in `lib/mcp/rust_host.py`: `mcp.registry_catalog`
//! (via `registry::get_catalog_entry`), `mcp.config_rows` / `mcp.update_row`
//! (`lib.mcp.config` row read and locked compare-and-set), `vault.get_entry`,
//! `browser.connected_clients`, `browser.select_client`, `browser.command`,
//! `browser.read_allowed` (`lib.browser`).

use crate::bridge::Hooks;
use crate::callback::BindingCalls;
use crate::errors::CallError;
use crate::pyjson::py_strip;
use crate::transport::vault_reference_entry;
use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::sync::OnceLock;

/// `_COOKIE_COMMAND_TIMEOUT_S` (lib/browser/cookie_capture.py's bound).
pub const COOKIE_COMMAND_TIMEOUT_S: u64 = 10;
/// RFC 6265 §6.1 (`_MAX_COOKIE_BYTES`).
pub const MAX_COOKIE_BYTES: usize = 4096;
/// OWASP session identifier entropy (`_MIN_SESSION_ENTROPY_BITS`).
pub const MIN_SESSION_ENTROPY_BITS: f64 = 64.0;
/// `BrowserCapability.COOKIE_MATCH` (lib/browser/protocol.py).
pub const COOKIE_MATCH_CAPABILITY: &str = "cookie_match";

fn cookie_name_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$").expect("static pattern"))
}

fn cookie_name(text: &str) -> bool {
    cookie_name_re().is_match(text)
}

/// RFC 6265 §4.1.1 cookie-octet.
fn cookie_octet(ch: char) -> bool {
    ('\u{21}'..'\u{7f}').contains(&ch) && !matches!(ch, '"' | ',' | ';' | '\\')
}

/// `_fingerprint`.
pub fn fingerprint(value: &str) -> String {
    Sha256::digest(value.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// `_entropy_bits`.
pub fn entropy_bits(value: &str) -> f64 {
    let mut alphabet = 0u32;
    if value.chars().any(|c| c.is_ascii_digit()) {
        alphabet += 10;
    }
    if value.chars().any(|c| c.is_ascii_lowercase()) {
        alphabet += 26;
    }
    if value.chars().any(|c| c.is_ascii_uppercase()) {
        alphabet += 26;
    }
    if value.chars().any(|c| !c.is_ascii_alphanumeric()) {
        alphabet += 32;
    }
    if alphabet > 1 {
        value.chars().count() as f64 * f64::from(alphabet).log2()
    } else {
        0.0
    }
}

/// `_session_like`: could `value` be a session identifier stored in a cookie?
pub fn session_like(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_COOKIE_BYTES
        && value.chars().all(cookie_octet)
        && !value.contains("://")
        && !value.starts_with(['/', '~'])
        && entropy_bits(value) >= MIN_SESSION_ENTROPY_BITS
}

/// `_header_pairs`: `[(name, value)]` when `value` is a Cookie header, else empty.
pub fn header_pairs(value: &str) -> Vec<(String, String)> {
    if !value.contains('=') {
        return Vec::new();
    }
    let mut pairs = Vec::new();
    for part in value.split(';') {
        let part = py_strip(part);
        if part.is_empty() {
            continue;
        }
        let Some((name, cookie_value)) = part.split_once('=') else {
            return Vec::new();
        };
        let name = py_strip(name);
        if !cookie_name(name) {
            return Vec::new();
        }
        pairs.push((name.to_string(), py_strip(cookie_value).to_string()));
    }
    pairs
}

/// `_usable`: a value safe to put into a child's environment.
pub fn usable(value: Option<&Value>) -> bool {
    match value {
        Some(Value::String(text)) => {
            !text.is_empty()
                && text.len() <= MAX_COOKIE_BYTES
                && !text.contains(['\u{0}', '\r', '\n'])
        }
        _ => false,
    }
}

/// `urlparse(url)`'s `(scheme, hostname)`.
pub fn url_scheme_host(url: &str) -> (String, Option<String>) {
    let cleaned: String = url
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = cleaned.as_str();
    let mut scheme = String::new();
    if let Some(index) = rest.find(':') {
        let head = &rest[..index];
        let starts_alpha = head.chars().next().is_some_and(|c| c.is_ascii_alphabetic());
        if index > 0
            && starts_alpha
            && head
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
        {
            scheme = head.to_ascii_lowercase();
            rest = &rest[index + 1..];
        }
    }
    let Some(after) = rest.strip_prefix("//") else {
        return (scheme, None);
    };
    let netloc = after.split(['/', '?', '#']).next().unwrap_or_default();
    let hostinfo = netloc.rsplit('@').next().unwrap_or_default();
    let hostname = match hostinfo.split_once('[') {
        Some((_, bracketed)) => bracketed.split(']').next().unwrap_or_default(),
        None => hostinfo.split(':').next().unwrap_or_default(),
    };
    if hostname.is_empty() {
        return (scheme, None);
    }
    let (host, zone) = match hostname.split_once('%') {
        Some((host, zone)) => (host.to_lowercase(), format!("%{zone}")),
        None => (hostname.to_lowercase(), String::new()),
    };
    (scheme, Some(host + &zone))
}

/// `_host`.
pub fn host(url: &str) -> String {
    url_scheme_host(url).1.unwrap_or_default().to_lowercase()
}

/// `_same_site`.
pub fn same_site(left: &str, right: &str) -> bool {
    let left = left.trim_start_matches('.').to_lowercase();
    let right = right.trim_start_matches('.').to_lowercase();
    !left.is_empty()
        && !right.is_empty()
        && (left == right
            || left.ends_with(&format!(".{right}"))
            || right.ends_with(&format!(".{left}")))
}

/// Python iteration of `value or ()` for a JSON list (or a string's characters).
fn iter_items(value: Option<&Value>) -> Vec<Value> {
    match value {
        Some(Value::Array(items)) => items.clone(),
        Some(Value::String(text)) => text.chars().map(|c| json!(c.to_string())).collect(),
        _ => Vec::new(),
    }
}

/// `_normalized`: a valid binding, else None.
pub fn normalized(entry: &Value) -> Option<Value> {
    let url_value = entry.get("url").filter(|v| crate::pyos::truthy(Some(v)));
    let url = py_strip(&url_value.map(crate::pyos::py_str).unwrap_or_default()).to_string();
    let (scheme, hostname) = url_scheme_host(&url);
    if !matches!(scheme.as_str(), "http" | "https") || hostname.is_none() {
        return None;
    }
    let kind = entry.get("kind").cloned().unwrap_or(Value::Null);
    let mut binding = Map::new();
    binding.insert("kind".into(), kind.clone());
    binding.insert("url".into(), json!(url));
    match kind.as_str() {
        Some("cookie") => {
            let name = entry
                .get("name")
                .filter(|v| crate::pyos::truthy(Some(v)))
                .map(crate::pyos::py_str)
                .unwrap_or_default();
            if !cookie_name(&name) {
                return None;
            }
            binding.insert("name".into(), entry["name"].clone());
        }
        Some("cookie_header") => {
            let names: Vec<Value> = iter_items(entry.get("names"))
                .into_iter()
                .filter(|n| n.as_str().is_some_and(cookie_name))
                .collect();
            let required: Vec<Value> = iter_items(entry.get("required"))
                .into_iter()
                .filter(|n| names.contains(n))
                .collect();
            if required.is_empty() {
                return None;
            }
            binding.insert("names".into(), Value::Array(names));
            binding.insert("required".into(), Value::Array(required));
        }
        _ => return None,
    }
    if let Some(Value::String(client)) = entry.get("client_id") {
        if !client.is_empty() {
            binding.insert("client_id".into(), json!(client));
        }
    }
    Some(Value::Object(binding))
}

/// `_env`.
pub fn env_of(row: &Value) -> Map<String, Value> {
    row.get("env")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

/// `_stored_entries`.
pub fn stored_entries(row: &Value) -> Map<String, Value> {
    row.get("browser_credentials")
        .and_then(Value::as_object)
        .map(|stored| {
            stored
                .iter()
                .filter(|(_, entry)| entry.is_object())
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect()
        })
        .unwrap_or_default()
}

fn env_specs(entry: Option<&Value>) -> Vec<Value> {
    match entry.and_then(|e| e.get("env_specs")) {
        Some(Value::Array(specs)) => specs.clone(),
        _ => Vec::new(),
    }
}

/// `_declared`: catalog `browser_cookie` declarations, with a configured base URL.
pub fn declared(entry: Option<&Value>, row: &Value) -> Map<String, Value> {
    let env = env_of(row);
    let mut out = Map::new();
    for spec in env_specs(entry) {
        let Some(cookie) = spec
            .get("browser_cookie")
            .filter(|c| crate::pyos::truthy(Some(c)))
        else {
            continue;
        };
        let configured = cookie
            .get("url_env")
            .filter(|u| crate::pyos::truthy(Some(u)))
            .and_then(|url_env| env.get(&crate::pyos::py_str(url_env)))
            .filter(|v| crate::pyos::truthy(Some(v)));
        let url = configured
            .or_else(|| cookie.get("url"))
            .map(crate::pyos::py_str)
            .unwrap_or_default();
        let candidate = json!({"kind": "cookie", "name": cookie.get("name").cloned().unwrap_or(Value::Null),
                               "url": py_strip(&url)});
        if let Some(binding) = normalized(&candidate) {
            out.insert(
                spec.get("key").map(crate::pyos::py_str).unwrap_or_default(),
                binding,
            );
        }
    }
    out
}

/// `_non_secret_keys`.
pub fn non_secret_keys(entry: Option<&Value>) -> BTreeSet<String> {
    env_specs(entry)
        .iter()
        .filter(|spec| {
            spec.get("secret") == Some(&Value::Bool(false))
                || spec.get("type") == Some(&json!("select"))
        })
        .filter_map(|spec| spec.get("key").map(crate::pyos::py_str))
        .collect()
}

/// `credential_bindings` over already-resolved credential values: a catalog
/// declaration wins; a learned binding counts while its fingerprint matches.
pub fn bindings_for(
    entry: Option<&Value>,
    row: &Value,
    values: &Map<String, Value>,
) -> Map<String, Value> {
    let stored = stored_entries(row);
    let mut bindings = Map::new();
    for (key, stored_entry) in &stored {
        if let Some(Value::String(value)) = values.get(key) {
            if stored_entry.get("fingerprint") == Some(&json!(fingerprint(value))) {
                if let Some(binding) = normalized(stored_entry) {
                    bindings.insert(key.clone(), binding);
                }
            }
        }
    }
    for (key, binding) in declared(entry, row) {
        let mut binding = binding;
        if let Some(device) = stored
            .get(&key)
            .and_then(|e| e.get("client_id"))
            .filter(|d| crate::pyos::truthy(Some(d)))
        {
            binding["client_id"] = device.clone();
        }
        bindings.insert(key, binding);
    }
    bindings
}

/// `_learning_candidates`.
pub fn learning_candidates(
    bound: &Map<String, Value>,
    non_secret: &BTreeSet<String>,
    values: &Map<String, Value>,
    skip: &BTreeSet<String>,
) -> Vec<(String, String)> {
    let mut candidates = Vec::new();
    for (key, value) in values {
        let Some(value) = value.as_str() else {
            continue;
        };
        if bound.contains_key(key) || non_secret.contains(key) || skip.contains(&fingerprint(value))
        {
            continue;
        }
        if session_like(value) || header_pairs(value).iter().any(|(_, v)| session_like(v)) {
            candidates.push((key.clone(), value.to_string()));
        }
    }
    candidates
}

/// `_utf16_length`: JavaScript string length.
pub fn utf16_length(value: &str) -> usize {
    value.encode_utf16().count()
}

/// HMAC-SHA-256 (RFC 2104) as lowercase hex.
pub fn hmac_sha256_hex(key: &[u8], message: &[u8]) -> String {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block.map(|b| b ^ 0x36));
    inner.update(message);
    let mut outer = Sha256::new();
    outer.update(block.map(|b| b ^ 0x5c));
    outer.update(inner.finalize());
    outer
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// RFC 4648 base64 with padding.
pub fn base64_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(ALPHABET[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// `os.urandom(32)`.
fn urandom_key() -> std::io::Result<[u8; 32]> {
    use std::io::Read;
    let mut key = [0u8; 32];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut key)?;
    Ok(key)
}

/// One candidate id → (env key, Cookie-header pair index or None for the whole value).
pub type Layout = Vec<(String, String, Option<usize>)>;

/// `_match_request`: the `match_cookies` params under `key`, and their layout.
pub fn match_request(candidates: &[(String, String)], key: &[u8]) -> (Value, Layout) {
    let mut entries = Vec::new();
    let mut layout: Layout = Vec::new();
    let mut add = |env_key: &str, index: Option<usize>, cookie: &str, value: &str| {
        let id = format!("c{}", layout.len());
        layout.push((id.clone(), env_key.to_string(), index));
        let mut entry = Map::new();
        entry.insert("id".into(), json!(id));
        entry.insert("length".into(), json!(utf16_length(value)));
        entry.insert("mac".into(), json!(hmac_sha256_hex(key, value.as_bytes())));
        if !cookie.is_empty() {
            entry.insert("name".into(), json!(cookie));
        }
        entries.push(Value::Object(entry));
    };
    for (env_key, value) in candidates {
        if session_like(value) {
            add(env_key, None, "", value);
        }
        for (index, (cookie, cookie_value)) in header_pairs(value).iter().enumerate() {
            if !cookie_value.is_empty() {
                add(env_key, Some(index), cookie, cookie_value);
            }
        }
    }
    (
        json!({"key": base64_encode(key), "candidates": entries}),
        layout,
    )
}

/// One matched cookie's identity: (name, domain, path, hostOnly, httpOnly).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Cookie {
    pub name: String,
    pub domain: String,
    pub path: String,
    pub host_only: bool,
    pub http_only: bool,
}

/// `_path_match` (RFC 6265 §5.1.4).
fn path_match(request: &str, cookie: &str) -> bool {
    request == cookie
        || (request.starts_with(cookie)
            && (cookie.ends_with('/') || request[cookie.len()..].starts_with('/')))
}

fn longest<'a>(items: impl Iterator<Item = &'a str>) -> String {
    let mut best: Option<&str> = None;
    for item in items {
        if best.is_none_or(|b| item.chars().count() > b.chars().count()) {
            best = Some(item);
        }
    }
    best.unwrap_or_default().to_string()
}

/// `_sending_url`: one URL to which the browser sends every cookie.
pub fn sending_url(cookies: &[Cookie]) -> Option<String> {
    let host = longest(cookies.iter().map(|c| c.domain.trim_start_matches('.')));
    let path = longest(cookies.iter().map(|c| c.path.as_str()));
    for cookie in cookies {
        let domain = cookie.domain.trim_start_matches('.');
        if cookie.host_only && host != domain {
            return None;
        }
        if !(host == domain || host.ends_with(&format!(".{domain}"))) {
            return None;
        }
        if !path_match(&path, &cookie.path) {
            return None;
        }
    }
    Some(format!(
        "https://{host}{}",
        if path.starts_with('/') {
            path.as_str()
        } else {
            "/"
        }
    ))
}

fn dedup(items: impl Iterator<Item = String>) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for item in items {
        if !out.contains(&item) {
            out.push(item);
        }
    }
    out
}

/// `_bindings_from_matches`: the bindings the browser's `matches` prove.
pub fn bindings_from_matches(
    candidates: &[(String, String)],
    layout: &Layout,
    matches: Option<&Value>,
    anchors: &[String],
) -> Vec<(String, Value)> {
    let mut found: Vec<(String, Vec<Cookie>)> = Vec::new();
    for item in matches.and_then(Value::as_array).into_iter().flatten() {
        let Some(id) = item.get("id").and_then(Value::as_str) else {
            continue;
        };
        if !item.is_object() || !layout.iter().any(|(cid, _, _)| cid == id) {
            continue;
        }
        let text = |key: &str, default: &str| {
            item.get(key)
                .filter(|v| crate::pyos::truthy(Some(v)))
                .map_or(default.to_string(), crate::pyos::py_str)
        };
        let cookie = Cookie {
            name: text("name", ""),
            domain: text("domain", ""),
            path: text("path", "/"),
            host_only: crate::pyos::truthy(item.get("hostOnly")),
            http_only: crate::pyos::truthy(item.get("httpOnly")),
        };
        if cookie.name.is_empty() || cookie.domain.is_empty() {
            continue;
        }
        match found.iter_mut().find(|(cid, _)| cid == id) {
            Some((_, cookies)) => {
                if !cookies.contains(&cookie) {
                    cookies.push(cookie);
                }
            }
            None => found.push((id.to_string(), vec![cookie])),
        }
    }
    let unique = |cid: &str| {
        found
            .iter()
            .find(|(id, cookies)| id == cid && cookies.len() == 1)
            .map(|(_, cookies)| cookies[0].clone())
    };
    let mut learned: Vec<(String, Value)> = Vec::new();
    for (env_key, value) in candidates {
        let pairs = header_pairs(value);
        let matched: Vec<(usize, Cookie)> = layout
            .iter()
            .filter(|(_, key, index)| key == env_key && index.is_some())
            .filter_map(|(cid, _, index)| Some(((*index)?, unique(cid)?)))
            .collect();
        let required: Vec<String> = matched
            .iter()
            .filter(|(index, _)| session_like(&pairs[*index].1))
            .map(|(index, _)| pairs[*index].0.clone())
            .collect();
        if required.is_empty() {
            continue;
        }
        let cookies: Vec<Cookie> = matched.into_iter().map(|(_, c)| c).collect();
        if let Some(url) = sending_url(&cookies) {
            learned.push((
                env_key.clone(),
                json!({"kind": "cookie_header", "url": url,
                       "names": dedup(pairs.iter().map(|(n, _)| n.clone())),
                       "required": dedup(required.into_iter())}),
            ));
        }
    }
    let mut singles: Vec<(String, Cookie)> = Vec::new();
    for (cid, key, index) in layout {
        if index.is_some() || learned.iter().any(|(k, _)| k == key) {
            continue;
        }
        if let Some(cookie) = unique(cid) {
            match singles.iter_mut().find(|(k, _)| k == key) {
                Some(slot) => slot.1 = cookie,
                None => singles.push((key.clone(), cookie)),
            }
        }
    }
    let mut anchors: Vec<String> = anchors.to_vec();
    anchors.extend(
        learned
            .iter()
            .map(|(_, b)| host(b["url"].as_str().unwrap_or_default())),
    );
    anchors.extend(
        singles
            .iter()
            .filter(|(_, c)| c.http_only)
            .map(|(_, c)| c.domain.clone()),
    );
    for (env_key, cookie) in singles {
        if cookie.http_only || anchors.iter().any(|a| same_site(&cookie.domain, a)) {
            let path = if cookie.path.is_empty() {
                "/"
            } else {
                cookie.path.as_str()
            };
            learned.push((
                env_key,
                json!({"kind": "cookie", "name": cookie.name,
                       "url": format!("https://{}{path}", cookie.domain.trim_start_matches('.'))}),
            ));
        }
    }
    learned
}

/// `_browser_value`: the renewed env value the browser's cookies give, else None.
pub fn browser_value(binding: &Value, cookies: &[Value]) -> Option<String> {
    if binding["kind"] == "cookie" {
        return cookies
            .iter()
            .find(|row| row.get("name") == binding.get("name") && usable(row.get("value")))
            .and_then(|row| row["value"].as_str().map(str::to_string));
    }
    let names = iter_items(binding.get("names"));
    let mut first: Vec<(Value, Value)> = Vec::new();
    for row in cookies {
        let name = row.get("name").cloned().unwrap_or(Value::Null);
        if names.contains(&name) && !first.iter().any(|(n, _)| *n == name) {
            first.push((name, row.get("value").cloned().unwrap_or(Value::Null)));
        }
    }
    let lookup = |name: &Value| first.iter().find(|(n, _)| n == name).map(|(_, v)| v);
    if !iter_items(binding.get("required"))
        .iter()
        .all(|n| usable(lookup(n)))
    {
        return None;
    }
    Some(
        names
            .iter()
            .filter(|n| usable(lookup(n)))
            .map(|n| {
                format!(
                    "{}={}",
                    crate::pyos::py_str(n),
                    lookup(n).and_then(Value::as_str).unwrap_or_default()
                )
            })
            .collect::<Vec<_>>()
            .join("; "),
    )
}

/// `failure_notice` over a row's bindings.
pub fn failure_notice_text(
    name: &str,
    bindings: &Map<String, Value>,
    renewed: bool,
    reason: &str,
) -> String {
    let sites: BTreeSet<String> = bindings
        .values()
        .map(|b| host(b["url"].as_str().unwrap_or_default()))
        .collect();
    let joined = sites.into_iter().collect::<Vec<_>>().join(", ");
    let site = if joined.is_empty() {
        "its site".to_string()
    } else {
        joined
    };
    if renewed {
        return format!(
            "[Tofu] The {name} MCP server's credential copies the user's browser session on {site}. \
             Tofu replaced the expired copy with the browser's current session and reconnected; repeat the call."
        );
    }
    let action = if reason == "no_browser" {
        format!("connect the Tofu browser extension and sign in to {site} there")
    } else {
        format!("sign in to {site} in the browser connected to Tofu")
    };
    format!(
        "[Tofu] The {name} MCP server's credential copies the user's browser session on {site}, and Tofu \
         found no newer session there. Ask the user to {action}; Tofu then copies the session itself. \
         Do not ask the user for cookie values."
    )
}

/// `RefreshOutcome`.
#[derive(Clone, Debug, PartialEq)]
pub struct RefreshOutcome {
    pub row: Option<Value>,
    pub changed: Vec<String>,
    /// `unbound`, `no_browser`, `no_session`, `current` or `renewed`.
    pub reason: &'static str,
}

impl RefreshOutcome {
    fn new(row: Option<Value>, reason: &'static str) -> Self {
        RefreshOutcome {
            row,
            changed: Vec::new(),
            reason,
        }
    }

    pub fn to_value(&self) -> Value {
        json!({"row": self.row, "changed": self.changed, "reason": self.reason})
    }
}

/// The owners one owner's credential operations reach.
pub struct Owners<'a> {
    pub binding: &'a dyn BindingCalls,
    pub hooks: &'a dyn Hooks,
    pub owner: u64,
}

impl Owners<'_> {
    fn owner_text(&self) -> String {
        self.owner.to_string()
    }

    /// `registry.get_catalog_entry(name)`.
    pub async fn catalog_entry(&self, name: &str) -> Result<Option<Value>, CallError> {
        crate::registry::get_catalog_entry(self.binding, name).await
    }

    /// `load_mcp_config(user_id=owner).get(name)`.
    pub async fn stored_row(&self, name: &str) -> Result<Option<Value>, CallError> {
        let rows = self
            .binding
            .ask("mcp.config_rows", json!({"owner_user_id": self.owner}))
            .await?;
        Ok(rows.get(name).filter(|row| row.is_object()).cloned())
    }

    /// One locked compare-and-set of the stored row (with vault preconditions
    /// and writes): `(resulting row, applied)`; the row is None when absent.
    async fn update_row(
        &self,
        name: &str,
        expect: &Value,
        row: Value,
        vault_expect: Map<String, Value>,
        vault_set: Map<String, Value>,
    ) -> Result<(Option<Value>, bool), CallError> {
        let answer = self
            .binding
            .ask(
                "mcp.update_row",
                json!({"owner_user_id": self.owner, "name": name, "expect": expect, "row": row,
                       "vault_expect": vault_expect, "vault_set": vault_set}),
            )
            .await?;
        Ok((
            answer.get("row").filter(|r| r.is_object()).cloned(),
            answer.get("applied") == Some(&Value::Bool(true)),
        ))
    }

    /// `_credential_values`: env with each `vault:<entry>` replaced by the entry's value.
    pub async fn credential_values(&self, row: &Value) -> Result<Map<String, Value>, CallError> {
        let mut values = Map::new();
        for (key, value) in env_of(row) {
            let resolved = match value.as_str().and_then(vault_reference_entry) {
                None => value,
                Some(entry) => {
                    self.binding
                        .ask("vault.get_entry", json!({"entry": entry}))
                        .await?
                }
            };
            values.insert(key, resolved);
        }
        Ok(values)
    }

    /// `credential_bindings(name, row)`.
    pub async fn credential_bindings(
        &self,
        name: &str,
        row: &Value,
    ) -> Result<Map<String, Value>, CallError> {
        let entry = self.catalog_entry(name).await?;
        let values = self.credential_values(row).await?;
        Ok(bindings_for(entry.as_ref(), row, &values))
    }

    /// `failure_notice(name, row, renewed=, reason=)`.
    pub async fn failure_notice(
        &self,
        name: &str,
        row: &Value,
        renewed: bool,
        reason: &str,
    ) -> Result<String, CallError> {
        let bindings = self.credential_bindings(name, row).await?;
        Ok(failure_notice_text(name, &bindings, renewed, reason))
    }

    /// `_devices`: the owner's connected devices; an unreachable bridge has none.
    async fn devices(&self) -> Result<Vec<Value>, CallError> {
        match self
            .binding
            .ask(
                "browser.connected_clients",
                json!({"owner": self.owner_text()}),
            )
            .await
        {
            Ok(Value::Array(devices)) => Ok(devices),
            Ok(_) => Ok(Vec::new()),
            Err(CallError::Unavailable(message)) => {
                self.hooks.log(
                    "info",
                    &format!("[MCP] Browser bridge unreachable for credentials: {message}"),
                );
                Ok(Vec::new())
            }
            Err(error) => Err(error),
        }
    }

    /// `select_owner_client(owner, owned, preferred=)`.
    async fn select_client(&self, owned: &[Value], preferred: &str) -> Result<String, CallError> {
        let chosen = self
            .binding
            .ask(
                "browser.select_client",
                json!({"owner": self.owner_text(), "owned": owned, "preferred": preferred}),
            )
            .await?;
        Ok(chosen.as_str().unwrap_or_default().to_string())
    }

    /// `_command`: one bounded browser command; a failure is logged and None.
    async fn command(
        &self,
        kind: &str,
        params: Value,
        client_id: &str,
    ) -> Result<Option<Value>, CallError> {
        let answer = self
            .binding
            .ask(
                "browser.command",
                json!({"owner": self.owner_text(), "client_id": client_id, "type": kind,
                       "params": params, "timeout": COOKIE_COMMAND_TIMEOUT_S}),
            )
            .await?;
        let error = answer.get("error").filter(|e| crate::pyos::truthy(Some(e)));
        if let Some(error) = error {
            self.hooks.log(
                "info",
                &format!(
                    "[MCP] Browser {kind} on {} failed: {}",
                    crate::pyjson::py_prefix(client_id, 12),
                    crate::pyjson::py_prefix(&crate::pyos::py_str(error), 160)
                ),
            );
            return Ok(None);
        }
        Ok(answer.get("result").cloned())
    }

    /// `learn_bindings(name, owner_user_id=, skip=)`: the fingerprints a device answered for.
    pub async fn learn_bindings(
        &self,
        name: &str,
        skip: &BTreeSet<String>,
    ) -> Result<BTreeSet<String>, CallError> {
        let Some(row) = self.stored_row(name).await? else {
            return Ok(BTreeSet::new());
        };
        if row
            .get("enabled")
            .is_some_and(|e| !crate::pyos::truthy(Some(e)))
        {
            return Ok(BTreeSet::new());
        }
        let entry = self.catalog_entry(name).await?;
        let values = self.credential_values(&row).await?;
        let bound = bindings_for(entry.as_ref(), &row, &values);
        let candidates =
            learning_candidates(&bound, &non_secret_keys(entry.as_ref()), &values, skip);
        if candidates.is_empty() {
            return Ok(BTreeSet::new());
        }
        let connected = self.devices().await?;
        let capable: Vec<String> = connected
            .iter()
            .filter(|device| match device.get("capabilities") {
                Some(Value::Array(caps)) => caps.contains(&json!(COOKIE_MATCH_CAPABILITY)),
                Some(Value::String(caps)) => caps.contains(COOKIE_MATCH_CAPABILITY),
                _ => false,
            })
            .map(|device| {
                device
                    .get("client_id")
                    .filter(|c| crate::pyos::truthy(Some(c)))
                    .map(crate::pyos::py_str)
                    .unwrap_or_default()
            })
            .collect();
        if capable.is_empty() {
            return Ok(BTreeSet::new());
        }
        let first = self.select_client(&connected, "").await?;
        let mut devices = capable;
        devices.sort_by_key(|client| *client != first);
        let anchors: Vec<String> = bound
            .values()
            .map(|b| host(b["url"].as_str().unwrap_or_default()))
            .collect();
        let key = urandom_key().map_err(|e| CallError::Error(e.to_string()))?;
        let (request, layout) = match_request(&candidates, &key);
        let mut learned: Vec<(String, Value)> = Vec::new();
        let mut answered = false;
        for client_id in &devices {
            let result = self
                .command("match_cookies", request.clone(), client_id)
                .await?;
            let Some(result) = result.filter(Value::is_object) else {
                continue;
            };
            answered = true;
            for (env_key, binding) in
                bindings_from_matches(&candidates, &layout, result.get("matches"), &anchors)
            {
                if learned.iter().any(|(k, _)| *k == env_key) {
                    continue;
                }
                let value = &candidates
                    .iter()
                    .find(|(k, _)| *k == env_key)
                    .expect("candidate")
                    .1;
                let mut entry = binding;
                entry["client_id"] = json!(client_id);
                entry["fingerprint"] = json!(fingerprint(value));
                learned.push((env_key, entry));
            }
            if learned.len() == candidates.len() {
                break;
            }
        }
        if !learned.is_empty() {
            self.bind_learned(name, row, &learned).await?;
            let keys: BTreeSet<&str> = learned.iter().map(|(k, _)| k.as_str()).collect();
            let hosts: BTreeSet<String> = learned
                .iter()
                .map(|(_, b)| host(b["url"].as_str().unwrap_or_default()))
                .collect();
            self.hooks.log(
                "info",
                &format!(
                    "[MCP] {name}: {} copy browser cookies ({})",
                    keys.into_iter().collect::<Vec<_>>().join(", "),
                    hosts.into_iter().collect::<Vec<_>>().join(", ")
                ),
            );
        }
        if !answered {
            return Ok(BTreeSet::new());
        }
        Ok(candidates.iter().map(|(_, v)| fingerprint(v)).collect())
    }

    /// `learn_bindings`' `_bind` transaction: persist the learned entries whose
    /// value is still the one the browser matched.
    async fn bind_learned(
        &self,
        name: &str,
        row: Value,
        learned: &[(String, Value)],
    ) -> Result<(), CallError> {
        let mut current = row;
        loop {
            let values = self.credential_values(&current).await?;
            let raw = env_of(&current);
            let mut stored = stored_entries(&current);
            let fresh: Vec<&(String, Value)> = learned
                .iter()
                .filter(|(key, entry)| {
                    values
                        .get(key)
                        .and_then(Value::as_str)
                        .is_some_and(|v| entry["fingerprint"] == json!(fingerprint(v)))
                })
                .collect();
            if fresh.is_empty() {
                return Ok(());
            }
            let mut vault_expect = Map::new();
            for (key, entry) in &fresh {
                if let Some(vault) = raw
                    .get(key)
                    .and_then(Value::as_str)
                    .and_then(vault_reference_entry)
                {
                    vault_expect.insert(vault.to_string(), values[key].clone());
                }
                stored.insert(key.clone(), entry.clone());
            }
            let mut updated = current.clone();
            updated["browser_credentials"] = Value::Object(stored);
            match self
                .update_row(name, &current, updated, vault_expect, Map::new())
                .await?
            {
                (Some(_), true) | (None, _) => return Ok(()),
                (Some(next), false) => current = next,
            }
        }
    }

    /// `refresh_from_browser(name, owner_user_id=)`.
    pub async fn refresh_from_browser(&self, name: &str) -> Result<RefreshOutcome, CallError> {
        let Some(row) = self.stored_row(name).await? else {
            return Ok(RefreshOutcome::new(None, "unbound"));
        };
        let bindings = self.credential_bindings(name, &row).await?;
        if bindings.is_empty() {
            return Ok(RefreshOutcome::new(Some(row), "unbound"));
        }
        let devices = self.devices().await?;
        if devices.is_empty() {
            return Ok(RefreshOutcome::new(Some(row), "no_browser"));
        }
        let mut fresh: Vec<(String, String)> = Vec::new();
        let mut read_from: Map<String, Value> = Map::new();
        for (key, binding) in &bindings {
            let url = binding["url"].as_str().unwrap_or_default();
            let allowed = self
                .binding
                .ask(
                    "browser.read_allowed",
                    json!({"owner": self.owner_text(), "url": url}),
                )
                .await?;
            if allowed != Value::Bool(true) {
                continue;
            }
            let preferred = binding
                .get("client_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let client_id = self.select_client(&devices, preferred).await?;
            let mut params = json!({"url": url});
            if binding["kind"] == "cookie" {
                params["name"] = binding["name"].clone();
            }
            let cookies = self.command("get_cookies", params, &client_id).await?;
            let value = match cookies {
                Some(Value::Array(list)) => {
                    let rows: Vec<Value> = list.into_iter().filter(Value::is_object).collect();
                    browser_value(binding, &rows)
                }
                _ => None,
            };
            if let Some(value) = value {
                fresh.push((key.clone(), value));
                read_from.insert(key.clone(), json!(client_id));
            }
        }
        let raw = env_of(&row);
        let values = self.credential_values(&row).await?;
        let stored_before = stored_entries(&row);
        let fresh_value = |key: &str| {
            fresh
                .iter()
                .find(|(k, _)| k == key)
                .map(|(_, v)| v.clone())
                .unwrap_or_default()
        };
        let mut changed: Vec<String> = fresh
            .iter()
            .filter(|(key, value)| values.get(key) != Some(&json!(value)))
            .map(|(key, _)| key.clone())
            .collect();
        changed.sort();
        if changed.is_empty() {
            let reason = if fresh.is_empty() {
                "no_session"
            } else {
                "current"
            };
            return Ok(RefreshOutcome::new(Some(row), reason));
        }
        let learned: BTreeSet<&String> = changed
            .iter()
            .filter(|key| {
                stored_before
                    .get(*key)
                    .is_some_and(|e| e.get("fingerprint").is_some())
            })
            .collect();
        // Each changed key's (stored env value, credential value) as read.
        type Before = (Option<Value>, Option<Value>);
        let expected: Vec<(String, Before)> = changed
            .iter()
            .map(|key| {
                (
                    key.clone(),
                    (raw.get(key).cloned(), values.get(key).cloned()),
                )
            })
            .collect();
        let mut current = row;
        let (row, applied) = loop {
            let current_env = env_of(&current);
            let current_values = self.credential_values(&current).await?;
            let mut stored = stored_entries(&current);
            let keys: Vec<&String> = expected
                .iter()
                .filter(|(key, before)| {
                    (
                        current_env.get(key).cloned(),
                        current_values.get(key).cloned(),
                    ) == *before
                })
                .map(|(key, _)| key)
                .collect();
            if keys.is_empty() {
                break (current, Vec::new());
            }
            let mut new_env = current_env.clone();
            let mut vault_expect = Map::new();
            let mut vault_set = Map::new();
            for key in &keys {
                let value = fresh_value(key);
                match current_env
                    .get(*key)
                    .and_then(Value::as_str)
                    .and_then(vault_reference_entry)
                {
                    None => {
                        new_env.insert((*key).clone(), json!(value));
                    }
                    Some(vault) => {
                        vault_expect.insert(vault.to_string(), current_values[*key].clone());
                        vault_set.insert(vault.to_string(), json!(value));
                    }
                }
                let mut entry = stored
                    .get(*key)
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                entry.insert("client_id".into(), read_from[*key].clone());
                if learned.contains(key) {
                    entry.insert("fingerprint".into(), json!(fingerprint(&value)));
                }
                stored.insert((*key).clone(), Value::Object(entry));
            }
            let mut updated = current.clone();
            updated["env"] = Value::Object(new_env);
            updated["browser_credentials"] = Value::Object(stored);
            match self
                .update_row(name, &current, updated, vault_expect, vault_set)
                .await?
            {
                (None, _) => return Ok(RefreshOutcome::new(None, "unbound")),
                (Some(row), true) => break (row, keys.into_iter().cloned().collect::<Vec<_>>()),
                (Some(next), false) => current = next,
            }
        };
        if applied.is_empty() {
            return Ok(RefreshOutcome::new(Some(row), "current"));
        }
        let mut fields = Map::new();
        fields.insert("server".into(), json!(name));
        fields.insert("env_keys".into(), json!(applied));
        fields.insert("source".into(), json!("browser_cookie"));
        self.hooks.audit("mcp_credential_refreshed", fields);
        self.hooks.log(
            "info",
            &format!(
                "[MCP] Saved {} for {name} from the owner's browser",
                applied.join(", ")
            ),
        );
        Ok(RefreshOutcome {
            row: Some(row),
            changed: applied,
            reason: "renewed",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hmac_and_base64_match_the_rfc_vectors() {
        assert_eq!(
            hmac_sha256_hex(b"key", b"The quick brown fox jumps over the lazy dog"),
            "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
        );
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
    }

    #[test]
    fn session_likeness_needs_entropy_and_cookie_octets() {
        assert!(session_like("fresh-session-value-1111"));
        assert!(!session_like("your-username"));
        assert!(!session_like("https://example.com/abcdefghijklmnop"));
        assert!(!session_like("has space in it 1234567890abcdef"));
        assert_eq!(
            header_pairs("a1=x; webId=abc; web_session=y"),
            vec![
                ("a1".into(), "x".into()),
                ("webId".into(), "abc".into()),
                ("web_session".into(), "y".into())
            ]
        );
        assert!(header_pairs("not a header=1; bad name=2").is_empty());
    }

    #[test]
    fn urls_parse_like_urllib() {
        assert_eq!(
            url_scheme_host("HTTPS://User@WWW.Overleaf.com:443/x"),
            ("https".into(), Some("www.overleaf.com".into()))
        );
        assert_eq!(url_scheme_host("www.overleaf.com").1, None);
        assert_eq!(url_scheme_host("http://[::1]:80/").1, Some("::1".into()));
    }
}
