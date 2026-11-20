//! The supervisor HTTP door (`supervisor.py`), protocol-identical.
//!
//! Endpoints: `GET /health`, `GET /status?projectPath=&probe=`, `POST /start`,
//! `/stop`, `/restart`, `/restart-deferred`, `/reload`. No separate auth: the
//! only guard is the exact-realpath project allow-list (see supervisor.py's
//! header for why). One accept loop; each accepted request runs on its own
//! thread, as `ThreadingHTTPServer` does, so a long restart never queues a
//! status poll behind it and nothing is sized from the host.

use crate::bridge::Bridge;
use crate::envpolicy::path_is_pytest_owned;
use crate::fingerprint::{self, SUPERVISOR_PROTOCOL_VERSION, SUPERVISOR_VERSION};
use crate::json::{Object, ObjectExt};
use crate::manager::LifecycleManager;
use crate::probe;
use crate::timing::{now, sleep, Deadline};
use crate::{resident, slog};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};

const DEFAULT_PORT: u16 = 15001;
const DEFER_QUIESCE_DEFAULT_MAX_WAIT_S: f64 = 3600.0;
const APPLICATION_DEFER_QUIESCE_DEFAULT_MAX_WAIT_S: f64 = 0.0;
const DEFER_QUIESCE_DEFAULT_POLL_S: f64 = 2.0;
const APPLICATION_RESTART_SOURCES: [&str; 2] = ["application-update", "application-restart"];
const MAX_REQUEST_BODY: usize = 1024 * 1024;
const OWNERSHIP_POLL_SECONDS: f64 = 0.5;

pub struct Config {
    pub host: String,
    pub port: u16,
    pub allowlist: BTreeSet<String>,
    pub python: String,
    pub source_root: PathBuf,
    pub owner_pid: Option<i64>,
}

fn realpath(path: &str) -> String {
    std::fs::canonicalize(path)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| path.to_owned())
}

/// `parse_allowlist`.
pub fn parse_allowlist(raw: &str) -> BTreeSet<String> {
    raw.split(':')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(realpath)
        .collect()
}

/// `parse_owner_pid`.
pub fn parse_owner_pid(raw: Option<&str>) -> Result<Option<i64>, String> {
    let Some(raw) = raw.map(str::trim).filter(|raw| !raw.is_empty()) else {
        return Ok(None);
    };
    match raw.parse::<i64>() {
        Ok(pid) if pid > 0 => Ok(Some(pid)),
        _ => Err("TOFU_SUPERVISOR_OWNER_PID must be a positive PID".into()),
    }
}

impl Config {
    pub fn from_environment(source_root: Option<PathBuf>) -> Result<Config, String> {
        let host = std::env::var("TOFU_SUPERVISOR_HOST").unwrap_or_else(|_| "127.0.0.1".into());
        let port = std::env::var("TOFU_SUPERVISOR_PORT")
            .ok()
            .and_then(|raw| raw.trim().parse().ok())
            .unwrap_or(DEFAULT_PORT);
        let configured = std::env::var("TOFU_SUPERVISOR_PROJECTS").unwrap_or_default();
        let mut allowlist = parse_allowlist(&configured);
        let source_root = source_root
            .or_else(|| allowlist.iter().next().map(PathBuf::from))
            .ok_or("--source-root is required when TOFU_SUPERVISOR_PROJECTS is empty")?;
        let source_root = std::fs::canonicalize(&source_root)
            .map_err(|e| format!("{}: {e}", source_root.display()))?;
        if configured.trim().is_empty() {
            allowlist.insert(source_root.to_string_lossy().into_owned());
        }
        let python = std::env::var("TOFU_SUPERVISOR_PYTHON")
            .ok()
            .filter(|p| !p.is_empty())
            .unwrap_or_else(|| "python3".into());
        let owner_pid =
            parse_owner_pid(std::env::var("TOFU_SUPERVISOR_OWNER_PID").ok().as_deref())?;
        Ok(Config {
            host,
            port,
            allowlist,
            python,
            source_root,
            owner_pid,
        })
    }
}

/// `is_allowed`: allow-listed and still a Tofu checkout.
pub fn is_allowed(project: &str, allowlist: &BTreeSet<String>) -> bool {
    if project.is_empty() {
        return false;
    }
    let canonical = realpath(project);
    allowlist.contains(&canonical)
        && Path::new(&canonical).join("server.py").is_file()
        && Path::new(&canonical).join("stop.sh").is_file()
}

/// `lifecycle_control_source_error`.
pub fn lifecycle_control_source_error(project: &str, source: Option<&Value>) -> String {
    let text = match source {
        Some(Value::String(text)) => text.to_lowercase(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string().to_lowercase(),
    };
    let parts: BTreeSet<String> = text
        .replace('_', "-")
        .split('-')
        .map(str::to_owned)
        .collect();
    if ["test", "tests", "pytest"]
        .iter()
        .any(|part| parts.contains(*part))
        && !path_is_pytest_owned(project, Path::new(project))
    {
        return "Test lifecycle control cannot target a production checkout.".into();
    }
    String::new()
}

fn env_seconds(name: &str, default: f64) -> f64 {
    match std::env::var(name)
        .ok()
        .and_then(|raw| raw.trim().parse::<f64>().ok())
    {
        Some(value) if value > 0.0 => value,
        _ => default,
    }
}

/// `deferred_quiesce_max_wait_s`.
pub fn deferred_quiesce_max_wait_s(source: &str) -> f64 {
    if !std::env::var("TOFU_SUPERVISOR_DEFER_QUIESCE_MAX_WAIT_S")
        .unwrap_or_default()
        .is_empty()
    {
        return env_seconds(
            "TOFU_SUPERVISOR_DEFER_QUIESCE_MAX_WAIT_S",
            DEFER_QUIESCE_DEFAULT_MAX_WAIT_S,
        );
    }
    if APPLICATION_RESTART_SOURCES.contains(&source) {
        return env_seconds(
            "TOFU_SUPERVISOR_APPLICATION_DEFER_QUIESCE_MAX_WAIT_S",
            APPLICATION_DEFER_QUIESCE_DEFAULT_MAX_WAIT_S,
        );
    }
    DEFER_QUIESCE_DEFAULT_MAX_WAIT_S
}

/// Python `repr` of a JSON value, for `[audit]` lines written like supervisor.py's.
fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => n.to_string(),
        Value::String(text) => format!("'{}'", text.replace('\\', "\\\\").replace('\'', "\\'")),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("'{k}': {}", py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn audit_log(event: &str, details: Value) {
    slog!(Info, "[audit] {event} {}", py_repr(&details));
}

pub struct Server {
    pub config: Config,
    pub managers: BTreeMap<String, Arc<LifecycleManager>>,
    pub started_at: f64,
    pub source_fingerprint: String,
    reload_request: Mutex<Option<Object>>,
    deferred: Mutex<BTreeSet<String>>,
    shutdown: AtomicBool,
    pub shutdown_signal: AtomicI32,
}

struct Request {
    method: String,
    path: String,
    query: BTreeMap<String, String>,
    body: Vec<u8>,
    version: String,
    raw_target: String,
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => out.push(b' '),
            b'%' if index + 2 < bytes.len() => {
                match u8::from_str_radix(&text[index + 1..index + 3], 16) {
                    Ok(byte) => {
                        out.push(byte);
                        index += 2;
                    }
                    Err(_) => out.push(b'%'),
                }
            }
            byte => out.push(byte),
        }
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn read_request(stream: &TcpStream) -> Result<Request, String> {
    let mut reader = BufReader::new(stream.try_clone().map_err(|e| e.to_string())?);
    let mut line = String::new();
    reader.read_line(&mut line).map_err(|e| e.to_string())?;
    let mut parts = line.split_whitespace();
    let method = parts.next().ok_or("empty request")?.to_owned();
    let target = parts.next().ok_or("no request target")?.to_owned();
    let version = parts.next().unwrap_or("HTTP/1.0").to_owned();
    let mut length = 0usize;
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header).map_err(|e| e.to_string())? == 0 {
            break;
        }
        let header = header.trim_end();
        if header.is_empty() {
            break;
        }
        if let Some((name, value)) = header.split_once(':') {
            if name.trim().eq_ignore_ascii_case("content-length") {
                length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0u8; length.min(MAX_REQUEST_BODY)];
    reader.read_exact(&mut body).map_err(|e| e.to_string())?;
    let (path, query_text) = target.split_once('?').unwrap_or((&target, ""));
    let mut query = BTreeMap::new();
    for pair in query_text.split('&').filter(|pair| !pair.is_empty()) {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        query
            .entry(percent_decode(key))
            .or_insert_with(|| percent_decode(value));
    }
    let trimmed = path.trim_end_matches('/');
    Ok(Request {
        method,
        path: if trimmed.is_empty() {
            "/".into()
        } else {
            trimmed.to_owned()
        },
        query,
        body,
        version,
        raw_target: target,
    })
}

fn reason_phrase(status: u16) -> &'static str {
    match status {
        200 => "OK",
        202 => "Accepted",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        _ => "Error",
    }
}

fn send_json(stream: &mut TcpStream, status: u16, payload: &Value) {
    let body = payload.to_string();
    let head = format!(
        "HTTP/1.0 {status} {}\r\nServer: TofuSupervisor/{SUPERVISOR_VERSION} tofu-manager\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        reason_phrase(status),
        body.len()
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(body.as_bytes());
    let _ = stream.flush();
}

impl Server {
    pub fn new(config: Config) -> Result<Arc<Server>, String> {
        let bridge = Arc::new(Bridge::new(
            config.python.clone(),
            config.source_root.clone(),
        ));
        let mut managers = BTreeMap::new();
        for project in &config.allowlist {
            if is_allowed(project, &config.allowlist) {
                managers.insert(
                    project.clone(),
                    LifecycleManager::new(Path::new(project), &config.python, bridge.clone())?,
                );
            }
        }
        let source_fingerprint = fingerprint::supervisor_source_fingerprint(&config.source_root);
        Ok(Arc::new(Server {
            config,
            managers,
            started_at: now(),
            source_fingerprint,
            reload_request: Mutex::new(None),
            deferred: Mutex::new(BTreeSet::new()),
            shutdown: AtomicBool::new(false),
            shutdown_signal: AtomicI32::new(0),
        }))
    }

    pub fn request_shutdown(&self) {
        self.shutdown.store(true, Ordering::SeqCst);
    }

    pub fn reload_request(&self) -> Option<Object> {
        self.reload_request.lock().unwrap().clone()
    }

    fn manager(&self, project: &str) -> Option<Arc<LifecycleManager>> {
        self.managers.get(&realpath(project)).cloned()
    }

    fn handle(self: &Arc<Self>, mut stream: TcpStream) {
        let peer = stream
            .peer_addr()
            .map(|a| a.ip().to_string())
            .unwrap_or_else(|_| "-".into());
        let request = match read_request(&stream) {
            Ok(request) => request,
            Err(_) => return,
        };
        let (status, payload) = self.route(&request);
        send_json(&mut stream, status, &payload);
        slog!(
            Info,
            "{peer} - \"{} {} {}\" {status} -",
            request.method,
            request.raw_target,
            request.version
        );
        let _ = stream.shutdown(std::net::Shutdown::Write);
    }

    fn not_allowed(project: &str) -> (u16, Value) {
        (
            403,
            json!({"ok": false, "error": "projectPath not in allow-list", "projectPath": project}),
        )
    }

    fn route(self: &Arc<Self>, request: &Request) -> (u16, Value) {
        match request.method.as_str() {
            "GET" => self.route_get(request),
            "POST" => self.route_post(request),
            _ => (404, json!({"ok": false, "error": "not found"})),
        }
    }

    fn route_get(&self, request: &Request) -> (u16, Value) {
        match request.path.as_str() {
            "/health" => (
                200,
                json!({
                    "ok": true, "version": SUPERVISOR_VERSION, "protocolVersion": SUPERVISOR_PROTOCOL_VERSION,
                    "managerPid": std::process::id(), "projects": self.config.allowlist,
                    "sourceFingerprint": self.source_fingerprint,
                    "sourceProjectPath": self.config.source_root.to_string_lossy(),
                    "startedAt": self.started_at, "implementation": "rust",
                    "binarySha256": fingerprint::binary_sha256(),
                }),
            ),
            "/status" => {
                let project = request
                    .query
                    .get("projectPath")
                    .map(|p| p.trim().to_owned())
                    .unwrap_or_default();
                if !is_allowed(&project, &self.config.allowlist) {
                    return Self::not_allowed(&project);
                }
                let Some(manager) = self.manager(&project) else {
                    return Self::not_allowed(&project);
                };
                let probe = request.query.get("probe").map(String::as_str) == Some("1");
                let mut payload = json!({
                    "ok": true, "supervisorVersion": SUPERVISOR_VERSION,
                    "supervisorProtocolVersion": SUPERVISOR_PROTOCOL_VERSION,
                    "supervisorSourceFingerprint": self.source_fingerprint,
                    "supervisorSourceProjectPath": self.config.source_root.to_string_lossy(),
                    "supervisorStartedAt": self.started_at,
                });
                for (key, value) in manager.status(probe) {
                    payload[key] = value;
                }
                (200, payload)
            }
            _ => (404, json!({"ok": false, "error": "not found"})),
        }
    }

    fn route_post(self: &Arc<Self>, request: &Request) -> (u16, Value) {
        if ![
            "/start",
            "/stop",
            "/restart",
            "/reload",
            "/restart-deferred",
        ]
        .contains(&request.path.as_str())
        {
            return (404, json!({"ok": false, "error": "not found"}));
        }
        let body: Value = if request.body.is_empty() {
            json!({})
        } else {
            match serde_json::from_slice::<Value>(&request.body) {
                Ok(value) => value,
                Err(error) => {
                    slog!(Warning, "bad JSON body: {error}");
                    json!({})
                }
            }
        };
        let Some(body) = body.as_object() else {
            return (
                400,
                json!({"ok": false, "error": "JSON body must be an object"}),
            );
        };
        let project = body.str_of("projectPath").trim().to_owned();
        if !is_allowed(&project, &self.config.allowlist) {
            return Self::not_allowed(&project);
        }
        let control_error = lifecycle_control_source_error(&project, body.get("source"));
        if !control_error.is_empty() {
            audit_log(
                "lifecycle_control_refused",
                json!({"projectPath": project, "source": body.get("source"), "reason": control_error}),
            );
            return (403, json!({"ok": false, "message": control_error}));
        }
        let source = |default: &str| -> String {
            match body.get("source") {
                Some(value) if crate::json::truthy(Some(value)) => value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string()),
                _ => default.to_owned(),
            }
        };
        let Some(manager) = self.manager(&project) else {
            return Self::not_allowed(&project);
        };
        let status_of = |result: Object| -> (u16, Value) {
            (
                if result.truthy("ok") { 200 } else { 409 },
                Value::Object(result),
            )
        };
        match request.path.as_str() {
            "/reload" => self.reload(&project, body, &source("http")),
            "/restart-deferred" => {
                let source = source("http-deferred");
                let requested_at = body
                    .get("requestedAt")
                    .and_then(|value| {
                        value
                            .as_f64()
                            .or_else(|| value.as_str().and_then(|t| t.parse().ok()))
                    })
                    .filter(|candidate| (now() - candidate).abs() <= 3600.0);
                let accepted = self.schedule_deferred_restart(&project, &source, requested_at);
                let wait_s = deferred_quiesce_max_wait_s(&source);
                let payload = if accepted {
                    json!({"ok": true, "accepted": true, "waitsForQuiescence": true, "maxWaitS": wait_s})
                } else {
                    json!({"ok": false, "accepted": false, "message": "A deferred worker restart is already pending."})
                };
                (if accepted { 202 } else { 409 }, payload)
            }
            "/start" => status_of(manager.start(
                body.get("serverArgs").filter(|v| !v.is_null()),
                body.get("serverEnv").filter(|v| !v.is_null()),
                &source("http"),
                true,
            )),
            "/restart" => status_of(manager.restart(
                body.get("serverArgs").filter(|v| !v.is_null()),
                body.get("serverEnv").filter(|v| !v.is_null()),
                &source("http"),
                None,
                body.get("engineOverride").filter(|v| !v.is_null()),
            )),
            _ => status_of(manager.stop(&source("http"))),
        }
    }

    fn reload(&self, project: &str, body: &Object, source: &str) -> (u16, Value) {
        let expected = body
            .get("expectedFingerprint")
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
            })
            .unwrap_or_default();
        let expected = expected.trim().to_owned();
        let disk = fingerprint::supervisor_source_fingerprint(&self.config.source_root);
        if expected.is_empty() || expected != disk {
            return (
                409,
                json!({"ok": false, "message": "Supervisor reload fingerprint does not match disk.",
                       "expectedFingerprint": expected, "diskFingerprint": disk}),
            );
        }
        if expected == self.source_fingerprint {
            return (
                200,
                json!({"ok": true, "reloading": false, "sourceFingerprint": expected}),
            );
        }
        let mut pending = self.reload_request.lock().unwrap();
        if pending.is_some() {
            return (
                409,
                json!({"ok": false, "reloading": false, "sourceFingerprint": expected, "message": "Supervisor reload is already in progress."}),
            );
        }
        let request = json!({"projectPath": realpath(project), "expectedFingerprint": expected, "source": source, "requestedAt": now()});
        audit_log("supervisor_reload_requested", request.clone());
        *pending = request.as_object().cloned();
        drop(pending);
        // The accept loop exits after this response flushes; main re-execs.
        self.request_shutdown();
        (
            202,
            json!({"ok": true, "reloading": true, "sourceFingerprint": expected}),
        )
    }

    fn worker_quiescence(manager: &LifecycleManager) -> Option<bool> {
        let port = manager
            .status(false)
            .get("port")
            .and_then(Value::as_u64)
            .unwrap_or(0) as u16;
        match probe::get_json(port, "/health/quiescence", 2.0) {
            Ok((404, _)) => None,
            Ok((_, payload)) => payload.get("quiet").and_then(Value::as_bool),
            Err(_) => None,
        }
    }

    fn schedule_deferred_restart(
        self: &Arc<Self>,
        project: &str,
        source: &str,
        requested_at: Option<f64>,
    ) -> bool {
        let project = realpath(project);
        let control_error = lifecycle_control_source_error(&project, Some(&json!(source)));
        if !control_error.is_empty() {
            slog!(Warning, "{control_error} project={project} source={source}");
            return false;
        }
        if !self.deferred.lock().unwrap().insert(project.clone()) {
            return false;
        }
        let server = self.clone();
        let source = source.to_owned();
        let project_key = project.clone();
        let name = format!(
            "tofu-worker-restart-{}",
            Path::new(&project)
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default()
        );
        let spawned = std::thread::Builder::new().name(name).spawn(move || {
            // The caller can be the worker being replaced: let its client read the 202.
            sleep(0.35);
            let started = std::time::Instant::now();
            if let Some(manager) = server.manager(&project) {
                let wait_s = deferred_quiesce_max_wait_s(&source);
                slog!(Info, "deferred worker restart interruption policy project={project} source={source} maxWaitS={wait_s:.1}");
                let quiesce_started = std::time::Instant::now();
                let outcome = if wait_s == 0.0 && APPLICATION_RESTART_SOURCES.contains(&source.as_str()) {
                    "approved"
                } else {
                    let deadline = Deadline::after(wait_s);
                    let poll = env_seconds("TOFU_SUPERVISOR_DEFER_QUIESCE_POLL_S", DEFER_QUIESCE_DEFAULT_POLL_S);
                    loop {
                        match Self::worker_quiescence(&manager) {
                            Some(true) => break "quiet",
                            None => break "unknown",
                            Some(false) => {}
                        }
                        if deadline.expired() {
                            break "timeout";
                        }
                        sleep(poll.min(deadline.remaining()));
                    }
                };
                let elapsed = quiesce_started.elapsed().as_secs_f64();
                let proceed = if outcome == "quiet" || outcome == "approved" {
                    slog!(Info, "deferred worker restart: interruption={outcome} after {elapsed:.1}s, restarting project={project} source={source}");
                    true
                } else if !APPLICATION_RESTART_SOURCES.contains(&source.as_str()) {
                    slog!(
                        Warning,
                        "deferred worker restart cancelled: quiescence={outcome} after {elapsed:.1}s project={project} source={source}; serving worker retained"
                    );
                    false
                } else {
                    slog!(Warning, "deferred worker restart proceeding with worker quiescence={outcome} after {elapsed:.1}s project={project} source={source}");
                    true
                };
                if proceed {
                    let result = manager.restart(None, None, &source, requested_at, None);
                    let total = requested_at.map(|r| format!(" totalSinceRequest={:.1}s", now() - r)).unwrap_or_default();
                    if result.truthy("ok") {
                        slog!(
                            Info,
                            "deferred worker restart accepted project={project} source={source} managerElapsed={:.1}s{total}",
                            started.elapsed().as_secs_f64()
                        );
                    } else {
                        slog!(
                            Error,
                            "deferred worker restart refused project={project} managerElapsed={:.1}s{total}: {}",
                            started.elapsed().as_secs_f64(),
                            result.str_of("message")
                        );
                    }
                }
            }
            server.deferred.lock().unwrap().remove(&project);
        });
        if spawned.is_err() {
            self.deferred.lock().unwrap().remove(&project_key);
            return false;
        }
        true
    }

    /// Release process/port budget exactly once on ownership loss.
    fn retire_owned_workers(&self) {
        for (project, manager) in &self.managers {
            if !manager.worker_running() {
                continue;
            }
            let result = manager.stop("supervisor-ownership-lost");
            if !result.truthy("ok") {
                slog!(
                    Error,
                    "ownership-loss worker retirement failed project={project}: {}",
                    result.str_of("message")
                );
            }
        }
    }

    /// Bind, start monitors and serve until shutdown, reload or ownership loss.
    pub fn serve(self: &Arc<Self>, listener: TcpListener) {
        for manager in self.managers.values() {
            let manager = manager.clone();
            let name = format!(
                "tofu-manager-{}",
                manager
                    .project
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default()
            );
            let _ = std::thread::Builder::new()
                .name(name)
                .spawn(move || manager.monitor_loop());
        }
        if self.config.allowlist.is_empty() {
            slog!(Warning, "TOFU_SUPERVISOR_PROJECTS is empty — no project is startable/stoppable until you allow-list one.");
        } else {
            slog!(
                Info,
                "Allow-listed projects: {}",
                self.config
                    .allowlist
                    .iter()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }
        slog!(
            Info,
            "Supervisor v{SUPERVISOR_VERSION} (rust tofu-manager) listening on {}:{}",
            self.config.host,
            self.config.port
        );
        let fd = listener.as_raw_fd();
        let mut ownership = crate::ownership::SupervisorOwnership::new(
            &self.config.source_root.join("supervisor.py"),
            self.config.owner_pid,
        );
        let mut last_ownership_check = std::time::Instant::now();
        while !self.shutdown.load(Ordering::SeqCst) {
            let mut poll = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            let ready = unsafe { libc::poll(&mut poll, 1, 100) };
            if ready > 0 {
                if let Ok((stream, _)) = listener.accept() {
                    // A reply is two sends (headers, then body); never hold the body for an ACK.
                    let _ = stream.set_nodelay(true);
                    let server = self.clone();
                    let _ = std::thread::Builder::new()
                        .name("tofu-manager-request".into())
                        .spawn(move || server.handle(stream));
                }
            }
            if last_ownership_check.elapsed().as_secs_f64() >= OWNERSHIP_POLL_SECONDS {
                last_ownership_check = std::time::Instant::now();
                let reason = ownership.lost();
                if !reason.is_empty() {
                    self.retire_owned_workers();
                    slog!(Warning, "{reason}; retiring detached manager.");
                    break;
                }
            }
        }
        // Let an in-flight reload response flush before the image is replaced.
        sleep(0.1);
        for manager in self.managers.values() {
            manager.close();
        }
    }

    /// Descriptors that must survive this manager's own exec.
    pub fn prepare_exec(&self) {
        for manager in self.managers.values() {
            for fd in manager.exec_retained_descriptors() {
                resident::retain_across_exec(fd);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sources_cannot_target_a_production_checkout() {
        assert!(
            !lifecycle_control_source_error("/srv/tofu", Some(&json!("pytest-restart"))).is_empty()
        );
        assert!(
            !lifecycle_control_source_error("/srv/tofu", Some(&json!("parity_test"))).is_empty()
        );
        assert!(lifecycle_control_source_error("/srv/tofu", Some(&json!("cli"))).is_empty());
        assert!(lifecycle_control_source_error(
            "/tmp/pytest-of-me/pytest-1/p",
            Some(&json!("test"))
        )
        .is_empty());
    }

    #[test]
    fn deferred_waits_follow_the_source() {
        assert_eq!(
            deferred_quiesce_max_wait_s("http-deferred"),
            DEFER_QUIESCE_DEFAULT_MAX_WAIT_S
        );
    }

    #[test]
    fn owner_pid_must_be_positive() {
        assert_eq!(parse_owner_pid(None).unwrap(), None);
        assert_eq!(parse_owner_pid(Some(" 12 ")).unwrap(), Some(12));
        assert!(parse_owner_pid(Some("0")).is_err());
        assert!(parse_owner_pid(Some("x")).is_err());
    }

    #[test]
    fn audit_details_render_like_python_dicts() {
        assert_eq!(
            py_repr(&json!({"a": "x'y", "b": null, "c": [true]})),
            "{'a': 'x\\'y', 'b': None, 'c': [True]}"
        );
    }

    #[test]
    fn query_strings_decode_like_parse_qs() {
        assert_eq!(percent_decode("%2Ftmp%2Fa+b"), "/tmp/a b");
        assert_eq!(percent_decode("100%"), "100%");
    }
}
