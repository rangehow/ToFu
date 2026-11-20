//! Egress executor: `egress_http`, `egress_http_stream`, `egress_cancel`.
//! Port of `lib/desktop_agent/_egress.py`.
//!
//! Executes an HTTP request from the user's machine (which has the working
//! proxy/VPN) for the Tofu server, whose own egress may be geo-blocked. Only
//! subscription-provider hosts are reachable: the whitelist is hardcoded here
//! and re-checked on every command, never widened by a server field. The
//! `loopback` target class reaches only this machine's loopback on the port
//! the node's own adapter policy occupies (`super::adapter::loopback_port`).
//!
//! Proxy discovery for `proxy_mode` `env`/`auto` (the default): process proxy
//! variables → the OS system proxy (Windows registry, macOS `scutil --proxy`;
//! Clash's "system proxy" writes there, not into env vars) → direct. `direct`
//! bypasses everything; `auto` also retries direct after a network failure.
//!
//! Streams: frames `meta` (status + headers JSON) → `body` (base64, ≤64 KiB
//! raw each) → `error` (network failure), then `on_exit` exactly once.
//! `egress_cancel` (by stream id, idempotent) shuts the socket down; frames of
//! a settled (cancelled or finished) stream are dropped.
//!
//! Log hygiene: headers and bodies carry the user's subscription token; only
//! method, host, status, sizes and timings are logged.

pub mod wire;

use super::{ChunkSink, ExitSink};
use crate::log;
use crate::util::Stop;
use base64::Engine;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::net::{Shutdown, TcpStream};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

/// Agent-side whitelist; deliberately a second copy of `lib/desktop/egress.py`'s
/// so a compromised server cannot widen it.
const ALLOWED_HOSTS: &[&str] = &[
    "api.anthropic.com",
    "console.anthropic.com",
    "platform.claude.com",
    "claude.ai",
    "auth.openai.com",
    "auth0.openai.com",
    "chatgpt.com",
    "api.openai.com",
];
const LOOPBACK_HOSTS: &[&str] = &["127.0.0.1", "localhost", "::1"];
/// Request body cap (design §7.3) and non-stream response cap.
const MAX_BODY_BYTES: usize = 2 * 1024 * 1024;
const MAX_RESPONSE_BYTES: usize = 10 * 1024 * 1024;
/// Raw bytes per body frame (design §4.2).
const FRAME_BYTES: usize = 64 * 1024;

pub fn host_allowed(url: &str, target: &str) -> bool {
    let Ok(parsed) = wire::parse_url(url) else {
        return false;
    };
    if target == "loopback" {
        if !LOOPBACK_HOSTS.contains(&parsed.host.as_str()) {
            return false;
        }
        let port = super::adapter::loopback_port();
        return port != 0 && parsed.explicit_port == Some(port);
    }
    ALLOWED_HOSTS.contains(&parsed.host.as_str())
}

fn host_of(url: &str) -> String {
    wire::parse_url(url)
        .map(|u| u.host)
        .unwrap_or_else(|_| "?".into())
}

// ── proxy resolution ────────────────────────────────────────────────────────

/// `ProxyServer` of the Windows Internet Settings: `host:port` or
/// `http=h:1;https=h:2` (https preferred). Empty when disabled.
pub fn windows_proxy_from(enable: bool, server: &str) -> String {
    let mut server = server.trim().to_string();
    if !enable || server.is_empty() {
        return String::new();
    }
    if server.contains('=') && !server.contains("://") {
        let parts: HashMap<&str, &str> = server
            .split(';')
            .filter_map(|p| p.split_once('='))
            .collect();
        server = parts
            .get("https")
            .filter(|v| !v.is_empty())
            .or_else(|| parts.get("http"))
            .unwrap_or(&"")
            .to_string();
    }
    if server.is_empty() {
        String::new()
    } else if server.contains("://") {
        server
    } else {
        format!("http://{server}")
    }
}

/// The system proxy in `scutil --proxy` output (HTTPS preferred).
pub fn scutil_proxy_from(output: &str) -> String {
    let mut kv = HashMap::new();
    for line in output.lines() {
        if let Some((k, v)) = line.trim().split_once(" : ") {
            kv.insert(k.trim().to_string(), v.trim().to_string());
        }
    }
    for proto in ["HTTPS", "HTTP"] {
        let enabled = kv.get(&format!("{proto}Enable")).map(String::as_str) == Some("1");
        if let Some(host) = kv
            .get(&format!("{proto}Proxy"))
            .filter(|h| enabled && !h.is_empty())
        {
            let port = kv
                .get(&format!("{proto}Port"))
                .map(String::as_str)
                .unwrap_or("8080");
            return format!("http://{host}:{port}");
        }
    }
    String::new()
}

#[cfg(windows)]
fn os_proxy_url() -> String {
    use std::ffi::c_void;
    use windows_sys::Win32::System::Registry::{
        RegGetValueW, HKEY_CURRENT_USER, RRF_RT_REG_DWORD, RRF_RT_REG_SZ,
    };
    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }
    let key = wide(r"Software\Microsoft\Windows\CurrentVersion\Internet Settings");
    let mut enable: u32 = 0;
    let mut size = std::mem::size_of::<u32>() as u32;
    // SAFETY: valid NUL-terminated wide strings and an output buffer of `size` bytes.
    let rc = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            wide("ProxyEnable").as_ptr(),
            RRF_RT_REG_DWORD,
            std::ptr::null_mut(),
            &mut enable as *mut u32 as *mut c_void,
            &mut size,
        )
    };
    if rc != 0 {
        return String::new();
    }
    let value = wide("ProxyServer");
    let mut bytes: u32 = 0;
    // SAFETY: a null data pointer asks only for the size.
    let rc = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut bytes,
        )
    };
    if rc != 0 || bytes == 0 {
        return String::new();
    }
    let mut buffer = vec![0u16; (bytes as usize).div_ceil(2)];
    // SAFETY: `buffer` holds `bytes` bytes.
    let rc = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            buffer.as_mut_ptr() as *mut c_void,
            &mut bytes,
        )
    };
    if rc != 0 {
        return String::new();
    }
    let end = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
    windows_proxy_from(enable != 0, &String::from_utf16_lossy(&buffer[..end]))
}

#[cfg(target_os = "macos")]
fn os_proxy_url() -> String {
    use std::process::{Command, Stdio};
    use std::sync::mpsc;
    let Ok(mut child) = Command::new("scutil")
        .arg("--proxy")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return String::new();
    };
    let mut stdout = child.stdout.take();
    let (send, receive) = mpsc::channel();
    std::thread::spawn(move || {
        let mut text = String::new();
        if let Some(out) = stdout.as_mut() {
            let _ = out.read_to_string(&mut text);
        }
        let _ = send.send(text);
    });
    // `_egress.py` gives scutil five seconds.
    match receive.recv_timeout(Duration::from_secs(5)) {
        Ok(text) => match child.wait() {
            Ok(status) if status.success() => scutil_proxy_from(&text),
            _ => String::new(),
        },
        Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            String::new()
        }
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
fn os_proxy_url() -> String {
    String::new()
}

/// The proxy for one request under `proxy_mode`, or None for direct.
pub(super) fn resolve_proxy(mode: &str, url: &str) -> Option<String> {
    if mode == "direct" {
        return None;
    }
    let env_has = ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]
        .iter()
        .any(|k| std::env::var(k).is_ok_and(|v| !v.is_empty()));
    if env_has {
        return crate::http::proxy_for(url);
    }
    // The OS proxy is a route to the internet; this machine's loopback never needs it.
    if !LOOPBACK_HOSTS.contains(&host_of(url).as_str()) {
        let os_proxy = os_proxy_url();
        if !os_proxy.is_empty() {
            return Some(os_proxy);
        }
    }
    crate::http::proxy_for(url)
}

// ── params (Python truthiness: `str(params.get(k) or default)`) ─────────────

pub(super) fn falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(b) => !b,
        Value::Number(n) => n.as_f64() == Some(0.0),
        Value::String(s) => s.is_empty(),
        Value::Array(a) => a.is_empty(),
        Value::Object(o) => o.is_empty(),
    }
}

pub(super) fn text(params: &Value, key: &str, default: &str) -> String {
    match params.get(key).filter(|v| !falsy(v)) {
        None => default.to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(_)) => "True".into(),
        Some(other) => other.to_string(),
    }
}

/// `min(max(int(params.get('timeout_ms') or default) / 1000, low), high)` seconds.
fn timeout_seconds(params: &Value, default_ms: i64, low: f64, high: f64) -> Result<f64, String> {
    let raw = match params.get("timeout_ms").filter(|v| !falsy(v)) {
        None => default_ms,
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f.trunc() as i64))
            .unwrap_or(default_ms),
        Some(Value::Bool(_)) => 1,
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| format!("invalid literal for int() with base 10: {s:?}"))?,
        Some(other) => return Err(format!("int() argument must be a number, not {other}")),
    };
    Ok((raw as f64 / 1000.0).max(low).min(high))
}

fn decode_body(params: &Value) -> Result<Vec<u8>, String> {
    match params.get("body_b64").filter(|v| !falsy(v)) {
        None => Ok(Vec::new()),
        Some(Value::String(s)) => {
            // binascii's lenient mode discards characters outside the alphabet.
            let cleaned: String = s
                .chars()
                .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '='))
                .collect();
            base64::engine::general_purpose::STANDARD
                .decode(cleaned)
                .map_err(|e| e.to_string())
        }
        Some(_) => Err("argument should be a bytes-like object or ASCII string".into()),
    }
}

fn header_list(headers: &Map<String, Value>) -> Result<Vec<(String, String)>, wire::Error> {
    headers
        .iter()
        .map(|(name, value)| match value {
            Value::String(v) => Ok((name.clone(), v.clone())),
            _ => Err(wire::Error::Invalid(format!(
                "Header part of {name:?} must be of type str"
            ))),
        })
        .collect()
}

fn response_headers(response: &wire::Response) -> Map<String, Value> {
    response
        .merged_headers()
        .into_iter()
        .filter(|(name, _)| !name.eq_ignore_ascii_case("set-cookie"))
        .map(|(name, value)| (name, Value::String(value)))
        .collect()
}

struct Request<'a> {
    method: &'a str,
    url: &'a wire::Url,
    headers: &'a [(String, String)],
    body: &'a [u8],
}

// ── egress_http ─────────────────────────────────────────────────────────────

fn do_request(
    request: &Request,
    timeout: Duration,
    proxy: Option<&str>,
) -> Result<Value, wire::Error> {
    let started = Instant::now();
    let options = wire::Options {
        proxy,
        connect_timeout: timeout,
        read_timeout: timeout,
    };
    let response = wire::send(
        request.method,
        request.url,
        request.headers,
        request.body,
        &options,
        &mut |_| {},
    )?;
    let status = response.status;
    let headers = response_headers(&response);
    let mut content = Vec::new();
    response
        .into_body()
        .take(MAX_RESPONSE_BYTES as u64 + 1)
        .read_to_end(&mut content)
        .map_err(wire::Error::from_io)?;
    if content.len() > MAX_RESPONSE_BYTES {
        return Ok(json!({"status": 0,
            "error": format!("response too large (more than {MAX_RESPONSE_BYTES} bytes)")}));
    }
    Ok(json!({
        "status": status,
        "headers": headers,
        "body_b64": base64::engine::general_purpose::STANDARD.encode(&content),
        "elapsed_ms": started.elapsed().as_millis() as u64,
    }))
}

/// One HTTP request for the server. Network failures come back as `status: 0`
/// plus `error`; refusals as `error` alone.
pub fn egress_http(params: &Value) -> Value {
    let url = text(params, "url", "");
    let target = text(params, "target", "subscription");
    if !host_allowed(&url, &target) {
        log::warn(&format!(
            "[Egress] REFUSED non-whitelisted host: {} (target={})",
            crate::util::truncate_chars(&host_of(&url), 80),
            crate::util::truncate_chars(&target, 40)
        ));
        return json!({"error": "egress host not in agent whitelist"});
    }
    let method = text(params, "method", "POST").to_uppercase();
    if method != "GET" && method != "POST" {
        return json!({"error": format!("unsupported method: {method}")});
    }
    let body = match decode_body(params) {
        Ok(body) => body,
        Err(e) => return json!({"error": format!("bad body_b64: {e}")}),
    };
    if body.len() > MAX_BODY_BYTES {
        return json!({"error": format!("request body too large ({} bytes)", body.len())});
    }
    let empty = Map::new();
    let headers = match params.get("headers").filter(|v| !falsy(v)) {
        None => &empty,
        Some(Value::Object(map)) => map,
        Some(_) => return json!({"error": "headers must be a dict"}),
    };
    let timeout = match timeout_seconds(params, 30_000, 1.0, 60.0) {
        Ok(t) => Duration::from_secs_f64(t),
        Err(e) => return json!({"error": e}),
    };
    let mode = text(params, "proxy_mode", "env");
    let host = host_of(&url);
    let outcome = wire::parse_url(&url)
        .map_err(wire::Error::Invalid)
        .and_then(|parsed| {
            let headers = header_list(headers)?;
            let request = Request {
                method: &method,
                url: &parsed,
                headers: &headers,
                body: &body,
            };
            match do_request(&request, timeout, resolve_proxy(&mode, &url).as_deref()) {
                Err(e) if e.is_network() && mode == "auto" => {
                    log::info(&format!(
                        "[Egress] {host} via proxy failed ({}) — retrying direct",
                        e.detail()
                    ));
                    do_request(&request, timeout, None)
                }
                other => other,
            }
        });
    match outcome {
        Ok(out) => {
            log::info(&format!(
                "[Egress] {method} {host} → {} ({}ms)",
                out["status"],
                out.get("elapsed_ms")
                    .map_or("?".to_string(), Value::to_string)
            ));
            out
        }
        Err(e) => {
            log::warn(&format!(
                "[Egress] {method} {host} network error: {}",
                e.detail()
            ));
            json!({"status": 0, "error": e.to_string()})
        }
    }
}

// ── egress_http_stream / egress_cancel ──────────────────────────────────────

#[derive(Default)]
struct Streams {
    /// Live streams: stream id → their socket (a clone that can shut it down).
    inflight: HashMap<String, TcpStream>,
    /// Cancelled or finished stream ids; their late frames are dropped.
    settled: HashSet<String>,
}

fn streams() -> std::sync::MutexGuard<'static, Streams> {
    static STREAMS: OnceLock<Mutex<Streams>> = OnceLock::new();
    STREAMS
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner())
}

fn settled(stream_id: &str) -> bool {
    streams().settled.contains(stream_id)
}

fn short(stream_id: &str) -> String {
    stream_id.chars().take(8).collect()
}

/// Abort a live streamed request; idempotent and silent on unknown ids (the
/// stream may already have finished).
pub fn cancel_inflight(stream_id: &str) -> bool {
    if stream_id.is_empty() {
        return false;
    }
    let socket = {
        let mut state = streams();
        state.settled.insert(stream_id.to_string());
        state.inflight.remove(stream_id)
    };
    match socket {
        None => {
            log::info(&format!(
                "[Egress] cancel for unknown/finished stream {}",
                short(stream_id)
            ));
            false
        }
        Some(socket) => {
            let _ = socket.shutdown(Shutdown::Both);
            log::info(&format!(
                "[Egress] cancelled in-flight stream {}",
                short(stream_id)
            ));
            true
        }
    }
}

/// Push one frame unless the stream is already settled.
fn emit(on_chunk: &ChunkSink, stream_id: &str, stream: &str, data: String) -> bool {
    if settled(stream_id) {
        return false;
    }
    on_chunk(stream, data);
    true
}

fn cancelled(status: u16, bytes: u64) -> Value {
    json!({"status": status, "bytes": bytes, "cancelled": true})
}

/// Stream one response as frames. Runs on the caller's (command) thread and
/// calls `on_exit` exactly once with the final stats or a refusal `{error}`.
pub fn start_stream(
    _cmd_id: &str,
    params: &Value,
    on_chunk: ChunkSink,
    on_exit: ExitSink,
    cancel: Stop,
) -> Result<(), String> {
    let stream_id = text(params, "stream_id", "");
    let outcome = run_stream(params, &stream_id, &on_chunk, &cancel);
    if !stream_id.is_empty() {
        let mut state = streams();
        state.inflight.remove(&stream_id);
        state.settled.insert(stream_id);
    }
    on_exit(outcome);
    Ok(())
}

fn refuse(message: String) -> Value {
    log::warn(&format!(
        "[Egress] stream refused: {}",
        crate::util::truncate_chars(&message, 120)
    ));
    json!({"error": message})
}

fn run_stream(params: &Value, stream_id: &str, on_chunk: &ChunkSink, cancel: &Stop) -> Value {
    let url = text(params, "url", "");
    let target = text(params, "target", "subscription");
    if !host_allowed(&url, &target) {
        return refuse("egress host not in agent whitelist".into());
    }
    if stream_id.is_empty() {
        return refuse("missing stream_id".into());
    }
    let method = text(params, "method", "POST").to_uppercase();
    if method != "GET" && method != "POST" {
        return refuse(format!("unsupported method: {method}"));
    }
    let body = match decode_body(params) {
        Ok(body) => body,
        Err(e) => return refuse(format!("bad body_b64: {e}")),
    };
    if body.len() > MAX_BODY_BYTES {
        return refuse(format!("request body too large ({} bytes)", body.len()));
    }
    let timeout = match timeout_seconds(params, 600_000, 30.0, 600.0) {
        Ok(t) => t,
        Err(e) => return refuse(e),
    };
    if settled(stream_id) {
        // Cancelled before it started: never reach (or bill) the upstream.
        return cancelled(0, 0);
    }
    let mode = text(params, "proxy_mode", "env");
    let host = host_of(&url);
    let started = Instant::now();
    let empty = Map::new();
    let headers = params
        .get("headers")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let options_proxy = resolve_proxy(&mode, &url);
    let options = wire::Options {
        proxy: options_proxy.as_deref(),
        connect_timeout: Duration::from_secs_f64(timeout.min(60.0)),
        read_timeout: Duration::from_secs_f64(timeout),
    };
    let mut register = |socket: &TcpStream| {
        let Ok(clone) = socket.try_clone() else {
            return;
        };
        let mut state = streams();
        if state.settled.contains(stream_id) {
            let _ = clone.shutdown(Shutdown::Both);
        } else {
            state.inflight.insert(stream_id.to_string(), clone);
        }
    };
    let connected = wire::parse_url(&url)
        .map_err(wire::Error::Invalid)
        .and_then(|parsed| {
            let headers = header_list(headers)?;
            wire::send(&method, &parsed, &headers, &body, &options, &mut register)
        });
    let response = match connected {
        Ok(response) => response,
        Err(_) if settled(stream_id) => return cancelled(0, 0),
        Err(e) => {
            log::warn(&format!(
                "[Egress] stream connect {host} failed: {}",
                e.detail()
            ));
            on_chunk(
                "error",
                json!({"message": e.to_string(), "where": "connect"}).to_string(),
            );
            return json!({"error": e.detail(), "status": 0});
        }
    };
    let status = response.status;
    let meta = json!({"status": status, "headers": response_headers(&response)}).to_string();
    if !emit(on_chunk, stream_id, "meta", meta) {
        return cancelled(status, 0);
    }
    log::info(&format!(
        "[Egress] stream {method} {host} → {status} (id={})",
        short(stream_id)
    ));
    let mut body = response.into_body();
    let mut buffer = vec![0u8; FRAME_BYTES];
    let mut total: u64 = 0;
    loop {
        if cancel.is_set() {
            cancel_inflight(stream_id);
        }
        let read = match body.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) if settled(stream_id) => return cancelled(status, total),
            Err(e) => {
                let e = wire::Error::from_io(e);
                log::warn(&format!(
                    "[Egress] stream {} mid-read failed: {}",
                    short(stream_id),
                    e.detail()
                ));
                emit(
                    on_chunk,
                    stream_id,
                    "error",
                    json!({"message": e.to_string(), "where": "read"}).to_string(),
                );
                return json!({"error": e.detail(), "status": status});
            }
        };
        if settled(stream_id) {
            log::info(&format!(
                "[Egress] stream {} dropped (cancelled)",
                short(stream_id)
            ));
            return cancelled(status, total);
        }
        total += read as u64;
        let frame = base64::engine::general_purpose::STANDARD.encode(&buffer[..read]);
        if !emit(on_chunk, stream_id, "body", frame) {
            return cancelled(status, total);
        }
    }
    if settled(stream_id) {
        return cancelled(status, total);
    }
    log::info(&format!(
        "[Egress] stream {} done: {total} bytes in {:.1}s",
        short(stream_id),
        started.elapsed().as_secs_f64()
    ));
    json!({"status": status, "bytes": total, "elapsed_ms": started.elapsed().as_millis() as u64})
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;
    use std::sync::Arc;

    #[test]
    fn subscription_whitelist_is_exact() {
        for host in ALLOWED_HOSTS {
            assert!(host_allowed(&format!("https://{host}/v1"), "subscription"));
        }
        assert!(host_allowed("https://API.OPENAI.COM/x", "subscription"));
        for url in [
            "https://evil.example/",
            "https://api.openai.com.evil.example/",
            "https://api.openai.com@evil.example/",
            "https://chatgpt.com./",
            "not a url",
            "",
        ] {
            assert!(!host_allowed(url, "subscription"), "{url}");
        }
        // The loopback class never accepts a provider host, nor the reverse.
        assert!(!host_allowed("https://api.openai.com/", "loopback"));
        assert!(!host_allowed("http://127.0.0.1:8317/", "subscription"));
    }

    #[test]
    fn proxy_settings_parse_like_the_python_node() {
        assert_eq!(
            windows_proxy_from(true, "127.0.0.1:7890"),
            "http://127.0.0.1:7890"
        );
        assert_eq!(windows_proxy_from(true, "http=h:1;https=h:2"), "http://h:2");
        assert_eq!(windows_proxy_from(true, "http=h:1"), "http://h:1");
        assert_eq!(windows_proxy_from(true, "socks=h:1"), "");
        assert_eq!(windows_proxy_from(false, "127.0.0.1:7890"), "");
        assert_eq!(windows_proxy_from(true, "http://p:3"), "http://p:3");
        let scutil = "<dictionary> {\n  HTTPEnable : 1\n  HTTPPort : 7890\n  HTTPProxy : 127.0.0.1\n  HTTPSEnable : 0\n  HTTPSProxy : 10.0.0.1\n}";
        assert_eq!(scutil_proxy_from(scutil), "http://127.0.0.1:7890");
        let both = "HTTPSEnable : 1\nHTTPSProxy : p\nHTTPEnable : 1\nHTTPProxy : q\nHTTPPort : 1";
        assert_eq!(scutil_proxy_from(both), "http://p:8080");
        assert_eq!(scutil_proxy_from("HTTPEnable : 0\nHTTPProxy : q"), "");
        assert_eq!(resolve_proxy("direct", "https://api.openai.com/"), None);
    }

    #[test]
    fn params_follow_python_truthiness() {
        let params = json!({"method": "", "timeout_ms": 0, "body_b64": "aGk=\n"});
        assert_eq!(text(&params, "method", "POST"), "POST");
        assert_eq!(timeout_seconds(&params, 30_000, 1.0, 60.0).unwrap(), 30.0);
        assert_eq!(
            timeout_seconds(&json!({"timeout_ms": 500}), 30_000, 1.0, 60.0).unwrap(),
            1.0
        );
        assert_eq!(decode_body(&params).unwrap(), b"hi");
        assert!(decode_body(&json!({"body_b64": "aGk"})).is_err());
        assert!(timeout_seconds(&json!({"timeout_ms": "x"}), 1, 1.0, 2.0).is_err());
    }

    #[test]
    fn refusals_never_touch_the_network() {
        let refused = egress_http(&json!({"url": "https://evil.example/"}));
        assert_eq!(refused["error"], "egress host not in agent whitelist");
        let refused = egress_http(&json!({"url": "https://api.openai.com/", "method": "PUT"}));
        assert_eq!(refused["error"], "unsupported method: PUT");
        let big = base64::engine::general_purpose::STANDARD.encode(vec![0u8; MAX_BODY_BYTES + 1]);
        let refused = egress_http(&json!({"url": "https://api.openai.com/", "body_b64": big}));
        assert!(refused["error"]
            .as_str()
            .unwrap()
            .starts_with("request body too large"));
        let refused = egress_http(&json!({"url": "https://api.openai.com/", "headers": [1]}));
        assert_eq!(refused["error"], "headers must be a dict");
    }

    type Frames = Arc<Mutex<Vec<(String, String)>>>;

    fn collect() -> (ChunkSink, Frames) {
        let frames = Arc::new(Mutex::new(Vec::new()));
        let sink = frames.clone();
        let chunk: ChunkSink = Arc::new(move |stream: &str, data: String| {
            sink.lock().unwrap().push((stream.to_string(), data));
        });
        (chunk, frames)
    }

    #[test]
    fn cancel_is_idempotent_and_settles_unknown_ids() {
        assert!(!cancel_inflight(""));
        assert!(!cancel_inflight("never-started"));
        assert!(!cancel_inflight("never-started"));
        assert!(settled("never-started"));
        let (chunk, frames) = collect();
        assert!(!emit(&chunk, "never-started", "body", "x".into()));
        assert!(frames.lock().unwrap().is_empty());
    }

    #[test]
    fn a_stream_cancelled_before_start_exits_once_without_frames() {
        cancel_inflight("pre-cancelled");
        let (chunk, frames) = collect();
        let exits = Arc::new(Mutex::new(Vec::new()));
        let sink = exits.clone();
        let params = json!({"url": "https://api.openai.com/v1", "stream_id": "pre-cancelled"});
        start_stream(
            "pre-cancelled",
            &params,
            chunk,
            Box::new(move |v| sink.lock().unwrap().push(v)),
            Stop::new(),
        )
        .unwrap();
        assert!(frames.lock().unwrap().is_empty());
        let exits = exits.lock().unwrap();
        assert_eq!(exits.len(), 1);
        assert_eq!(exits[0]["cancelled"], true);
    }

    #[test]
    fn a_blocked_read_is_aborted_by_cancel() {
        // A raw upstream that sends headers and one chunk, then hangs.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let _ = sock.read(&mut buf);
            sock.write_all(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n")
                .unwrap();
            std::thread::sleep(Duration::from_secs(30));
        });
        let url = wire::parse_url(&format!("http://127.0.0.1:{port}/s")).unwrap();
        let options = wire::Options {
            proxy: None,
            connect_timeout: Duration::from_secs(5),
            read_timeout: Duration::from_secs(30),
        };
        let id = "blocked-read";
        let mut register = |socket: &TcpStream| {
            streams()
                .inflight
                .insert(id.to_string(), socket.try_clone().unwrap());
        };
        let response = wire::send("GET", &url, &[], b"", &options, &mut register).unwrap();
        let mut body = response.into_body();
        let mut buf = [0u8; 16];
        assert_eq!(body.read(&mut buf).unwrap(), 5);
        let started = Instant::now();
        std::thread::spawn(|| {
            std::thread::sleep(Duration::from_millis(200));
            assert!(cancel_inflight("blocked-read"));
        });
        assert!(body.read(&mut buf).is_err() || body.read(&mut buf).unwrap_or(0) == 0);
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(!cancel_inflight("blocked-read"));
    }
}
