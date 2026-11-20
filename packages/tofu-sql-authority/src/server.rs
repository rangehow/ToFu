//! Authenticated loopback RPC server for `storage.v1`
//! (`lib/storage_authority_sql/server.py`, `lib/storage/protocol.py`).
//!
//! Framing: a 4-byte big-endian length, then one JSON object of at most
//! 64 MiB. Authentication, protocol version and deadline are inside every
//! frame. One thread serves each accepted connection (no admission cap:
//! "Hardware never limits work"); `keep_alive: true` keeps an authenticated
//! socket for the next frame, otherwise the server half-closes and waits for
//! the client's EOF before closing (response delivery before close).

use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::backend::Backend;
use crate::control::{CONVERSATION_WATCH_OPERATION, WORKER_WATCH_OPERATION};
use crate::error::StorageError;
use crate::json;
use crate::ops::{self, Kind};

pub const PROTOCOL_VERSION: &str = "storage.v1";
pub const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;
pub const FRAME_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_REQUEST_LIFETIME: Duration = Duration::from_secs(3600);
/// `tofu_db.generated_checkpoint_control.OPERATIONS`.
const NATIVE_CHECKPOINT_OPERATIONS: &[&str] = &[
    "begin",
    "complete",
    "create",
    "finish",
    "forget",
    "frontier",
    "goal_claim",
    "goal_handoff",
    "handoff",
    "invoice",
    "load",
    "pending",
    "recover",
    "release",
    "seal",
];

pub struct Server {
    pub backend: Arc<Backend>,
    token: String,
    read_only_preview: bool,
    native_checkpoint_runtime_id: String,
    active: AtomicU64,
    peak_active: AtomicU64,
    rejected: AtomicU64,
    stopping: AtomicBool,
}

fn closed_mid_frame() -> StorageError {
    StorageError::retryable("database_unavailable", "Storage connection closed mid-frame", 100)
}

/// Read one frame; `Ok(None)` is a clean EOF before any byte.
fn recv_frame(stream: &mut TcpStream) -> Result<Option<Value>, (StorageError, bool)> {
    let mut header = [0u8; 4];
    let mut read = 0;
    while read < 4 {
        match stream.read(&mut header[read..]) {
            Ok(0) if read == 0 => return Ok(None),
            Ok(0) => return Err((closed_mid_frame(), false)),
            Ok(count) => read += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) if matches!(error.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => {
                return Err((StorageError::retryable("database_timeout", "Storage frame timed out", 25), true));
            }
            Err(_) => return Err((closed_mid_frame(), false)),
        }
    }
    let size = u32::from_be_bytes(header) as usize;
    if size == 0 || size > MAX_FRAME_BYTES {
        return Err((StorageError::protocol("Invalid storage frame length"), true));
    }
    let mut body = vec![0u8; size];
    let mut offset = 0;
    while offset < size {
        match stream.read(&mut body[offset..]) {
            Ok(0) => return Err((closed_mid_frame(), false)),
            Ok(count) => offset += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) if matches!(error.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => {
                return Err((StorageError::retryable("database_timeout", "Storage frame timed out", 25), true));
            }
            Err(_) => return Err((closed_mid_frame(), false)),
        }
    }
    match json::loads(&body) {
        Ok(value @ Value::Object(_)) => Ok(Some(value)),
        Ok(_) => Err((StorageError::protocol("Storage frame must be an object"), true)),
        Err(_) => Err((StorageError::protocol("Invalid storage frame JSON"), true)),
    }
}

fn encode_frame(message: &Value) -> Result<Vec<u8>, StorageError> {
    let body = json::dumps(message);
    if body.is_empty() || body.len() > MAX_FRAME_BYTES {
        return Err(StorageError::protocol("Storage frame exceeds the size limit"));
    }
    let mut frame = Vec::with_capacity(body.len() + 4);
    frame.extend_from_slice(&(body.len() as u32).to_be_bytes());
    frame.extend_from_slice(&body);
    Ok(frame)
}

/// `validate_operation`.
fn valid_operation_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 192
        && bytes[0].is_ascii_lowercase()
        && bytes[1..].iter().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_.:-".contains(b))
}

fn unix_now_ms() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64() * 1000.0).unwrap_or(0.0)
}

/// `_StorageTCPServer.process_metrics` from procfs.
fn process_metrics() -> Value {
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) }.max(1) as u64;
    let rss = std::fs::read_to_string("/proc/self/statm")
        .ok()
        .and_then(|text| text.split_whitespace().nth(1).and_then(|pages| pages.parse::<u64>().ok()))
        .unwrap_or(0)
        * page;
    let handles = std::fs::read_dir("/proc/self/fd").map(|entries| entries.count()).unwrap_or(0);
    let threads = std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|text| {
            text.lines().find_map(|line| line.strip_prefix("Threads:").and_then(|n| n.trim().parse::<u64>().ok()))
        })
        .unwrap_or(0);
    json!({"rss_bytes": rss, "open_fds_or_handles": handles, "threads": threads})
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && left.iter().zip(right).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
}

fn failure_response(request_id: &str, mut error: StorageError) -> Value {
    if error.operation_id.is_empty() {
        error.operation_id = request_id.to_owned();
    }
    json!({"protocol": PROTOCOL_VERSION, "request_id": request_id, "ok": false, "error": error.to_payload()})
}

impl Server {
    pub fn new(backend: Arc<Backend>, token: String, read_only_preview: bool) -> Arc<Self> {
        Arc::new(Self {
            backend,
            token,
            read_only_preview,
            native_checkpoint_runtime_id: uuid::Uuid::new_v4().to_string(),
            active: AtomicU64::new(0),
            peak_active: AtomicU64::new(0),
            rejected: AtomicU64::new(0),
            stopping: AtomicBool::new(false),
        })
    }

    pub fn bind() -> io::Result<TcpListener> {
        TcpListener::bind(("127.0.0.1", 0))
    }

    pub fn serve(self: &Arc<Self>, listener: TcpListener) {
        for stream in listener.incoming() {
            if self.stopping.load(Ordering::SeqCst) {
                break;
            }
            let Ok(stream) = stream else { continue };
            let server = Arc::clone(self);
            let spawned = std::thread::Builder::new().name("tofu-storage-rpc".into()).spawn(move || {
                server.handle(stream);
            });
            if spawned.is_err() {
                self.rejected.fetch_add(1, Ordering::SeqCst);
                eprintln!("storage worker could not start");
            }
        }
    }

    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
    }

    fn handle(&self, mut stream: TcpStream) {
        let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak_active.fetch_max(active, Ordering::SeqCst);
        let _ = stream.set_read_timeout(Some(FRAME_TIMEOUT));
        let _ = stream.set_nodelay(true);
        while let Some(true) = self.handle_one(&mut stream) {
            let mut probe = [0u8; 1];
            if !matches!(stream.peek(&mut probe), Ok(count) if count > 0) {
                break;
            }
        }
        self.active.fetch_sub(1, Ordering::SeqCst);
    }

    /// Serve one frame; `Some(true)` keeps the socket for another.
    fn handle_one(&self, stream: &mut TcpStream) -> Option<bool> {
        let mut request_id = String::new();
        let mut keep_alive_requested = false;
        let mut authenticated = false;
        let response = match recv_frame(stream) {
            Ok(None) => return None,
            Err((error, _)) => failure_response("", error),
            Ok(Some(request)) => {
                request_id = match request.get("request_id") {
                    Some(value) if json::truthy(value) => json::python_str(value),
                    _ => String::new(),
                };
                keep_alive_requested = request.get("keep_alive") == Some(&Value::Bool(true));
                match self.dispatch(&request, &request_id, &mut authenticated) {
                    Ok(result) => {
                        json!({"protocol": PROTOCOL_VERSION, "request_id": request_id, "ok": true, "result": result})
                    }
                    Err(error) => failure_response(&request_id, error),
                }
            }
        };
        let keep_alive = keep_alive_requested && authenticated;
        let response = if keep_alive {
            let mut response = response;
            response["keep_alive"] = json!({
                "idle_timeout_ms": FRAME_TIMEOUT.as_millis() as i64,
                "idle_reuse_connections": self.backend.config.rpc_capacity,
            });
            response
        } else {
            response
        };
        let frame = match encode_frame(&response) {
            Ok(frame) => frame,
            Err(error) => match encode_frame(&failure_response(&request_id, error)) {
                Ok(frame) => frame,
                Err(_) => return None,
            },
        };
        if stream.write_all(&frame).is_err() {
            return None;
        }
        if keep_alive {
            return Some(true);
        }
        // Half-close and wait (bounded by the frame timeout) for the reader's EOF.
        let _ = stream.shutdown(Shutdown::Write);
        let mut byte = [0u8; 1];
        let _ = stream.read(&mut byte);
        Some(false)
    }

    fn dispatch(&self, request: &Value, request_id: &str, authenticated: &mut bool) -> Result<Value, StorageError> {
        if request.get("protocol").and_then(Value::as_str) != Some(PROTOCOL_VERSION) {
            return Err(StorageError::protocol("Storage protocol version mismatch"));
        }
        if request_id.is_empty() || request_id.chars().count() > 64 {
            return Err(StorageError::protocol("Invalid storage request id"));
        }
        match request.get("auth") {
            Some(Value::String(supplied)) if constant_time_equal(supplied.as_bytes(), self.token.as_bytes()) => {}
            _ => return Err(StorageError::protocol("Storage authentication failed")),
        }
        *authenticated = true;
        let deadline_ms = match request.get("deadline_unix_ms") {
            Some(Value::Number(number)) if number.is_i64() || number.is_u64() => number.as_f64().unwrap_or(0.0),
            _ => return Err(StorageError::protocol("Storage deadline is required")),
        };
        let remaining_ms = deadline_ms - unix_now_ms();
        if remaining_ms <= 0.0 {
            return Err(StorageError::retryable("database_timeout", "Storage request deadline expired", 25));
        }
        let deadline = Instant::now() + Duration::from_secs_f64(remaining_ms / 1000.0).min(MAX_REQUEST_LIFETIME);
        let kind = match request.get("kind") {
            Some(value) if json::truthy(value) => json::python_str(value),
            _ => String::new(),
        };
        let operation = match request.get("operation") {
            Some(value) if json::truthy(value) => json::python_str(value),
            _ => String::new(),
        };
        if !valid_operation_name(&operation) {
            return Err(StorageError::protocol("Invalid storage operation name"));
        }
        let read_only =
            || StorageError::retryable("database_unavailable", "Distributed preview storage is read-only", 1000);
        if self.read_only_preview && kind == "command" {
            return Err(read_only());
        }
        let payload_value = request.get("payload").cloned().unwrap_or(Value::Null);
        if operation == WORKER_WATCH_OPERATION || operation == CONVERSATION_WATCH_OPERATION {
            if kind != "query" || request.get("command_id").is_some_and(|value| !value.is_null()) {
                return Err(StorageError::protocol("Worker watch requires a query without a receipt"));
            }
            let control = &self.backend.worker_control;
            let channel =
                if operation == CONVERSATION_WATCH_OPERATION { &control.conversations } else { &control.workers };
            return channel.watch(&payload_value, deadline);
        }
        if operation.starts_with("native_checkpoint.") {
            if request.get("command_id").is_some_and(|value| !value.is_null()) {
                return Err(StorageError::protocol("Native checkpoint controls cannot use command receipts"));
            }
            return self.native_checkpoint(&kind, &operation, &payload_value);
        }
        match (kind.as_str(), operation.as_str()) {
            ("health", "system.health") => {
                let mut health = self.backend.health();
                if let Some(pipeline) = self.backend.logical_outbox.get() {
                    health["logical_outbox"] = pipeline.status();
                    if !pipeline.health_ready() {
                        health["ready"] = Value::Bool(false);
                    }
                }
                return Ok(health);
            }
            ("metrics", "system.metrics") => {
                let mut metrics = self.backend.metrics();
                metrics["rpc"] = json!({
                    "active": self.active.load(Ordering::SeqCst),
                    "capacity": 0,
                    "backlog_hint": self.backend.config.rpc_capacity,
                    "peak_active": self.peak_active.load(Ordering::SeqCst),
                    "waiting": 0,
                    "rejected": self.rejected.load(Ordering::SeqCst),
                    // No glibc idle heap trim exists in this process; the
                    // counters stay at their truthful zero.
                    "idle_trim_attempts": 0,
                    "idle_trim_successes": 0,
                    "idle_trim_reclaimed_bytes": 0,
                    "idle_trim_last_before_bytes": 0,
                    "idle_trim_last_after_bytes": 0,
                    "idle_trim_duration_ns_total": 0,
                    "idle_trim_last_duration_ns": 0,
                });
                if let Some(pipeline) = self.backend.logical_outbox.get() {
                    metrics["logical_outbox"] = pipeline.status();
                }
                metrics["process"] = process_metrics();
                metrics["attempt_events"] = crate::attempt_event_metrics::snapshot();
                let faults = crate::faults::status();
                if faults["enabled"] == Value::Bool(true) {
                    metrics["fault_injection"] = faults;
                }
                return Ok(metrics);
            }
            ("maintenance", "system.integrity_check") => return self.backend.integrity_check(deadline),
            ("maintenance", "system.preflight") => return Ok(self.backend.health()["preflight"].clone()),
            ("maintenance", "system.backup") => return self.backend.backup(deadline),
            ("maintenance", "system.baseline") => return self.backend.baseline(deadline),
            _ => {}
        }
        if !matches!(kind.as_str(), "maintenance" | "query" | "command") {
            return Err(StorageError::protocol("Invalid storage request kind"));
        }
        if kind == "maintenance" && self.read_only_preview {
            return Err(read_only());
        }
        let Value::Object(payload) = &payload_value else {
            return Err(StorageError::protocol("Storage payload must be an object"));
        };
        let spec = self.resolve(&operation, &kind)?;
        let receipt_required = spec.requires_receipt(payload);
        let execute = |session: &crate::session::Session<'_>| {
            let result = (spec.handler)(session, payload)?;
            match spec.after {
                Some(after) => after(session, &operation, payload, result),
                None => Ok(result),
            }
        };
        if kind == "query" {
            return self.backend.query(execute, deadline);
        }
        let digest = hex::encode(Sha256::digest(json::canonical_json(&json!({
            "operation": operation,
            "payload": payload_value,
        }))));
        if kind == "maintenance" {
            if receipt_required {
                return Err(StorageError::protocol("Maintenance operations cannot require command receipts"));
            }
            return self.backend.command(
                &operation,
                &digest,
                None,
                &execute,
                deadline,
                false,
                spec.transaction_timeout_s,
            );
        }
        let priority = request.get("priority").filter(|value| json::truthy(value));
        if let Some(priority) = priority {
            if !matches!(priority.as_str(), Some("user" | "event" | "maintenance")) {
                return Err(StorageError::protocol("Invalid storage priority"));
            }
        }
        let command_id = match request.get("command_id") {
            Some(Value::String(id)) => Some(id.as_str()),
            _ => None,
        };
        if receipt_required && !matches!(request.get("command_id"), Some(Value::String(_))) {
            return Err(StorageError::protocol("A valid command_id is required"));
        }
        let captured_record_bytes = std::cell::Cell::new(None);
        let pipeline = self.backend.logical_outbox.get();
        let with_outbox = |session: &crate::session::Session<'_>| {
            if !pipeline.is_some_and(|pipeline| pipeline.capture_enabled()) {
                return execute(session);
            }
            session.begin_logical_capture();
            let response = execute(session);
            let mutations = session.finish_logical_capture();
            let response = response?;
            if mutations.iter().any(|mutation| mutation["rowcount"].as_u64().unwrap_or(0) > 0) {
                captured_record_bytes.set(pipeline.expect("enabled pipeline").capture(
                    session,
                    &operation,
                    request_id,
                    &digest,
                    command_id,
                    &payload_value,
                    &response,
                    &mutations,
                )?);
            }
            Ok(response)
        };
        let result = self.backend.command(
            &operation,
            &digest,
            command_id,
            &with_outbox,
            deadline,
            receipt_required,
            spec.transaction_timeout_s,
        );
        if result.is_ok() {
            if let Some(pipeline) = pipeline {
                pipeline.notify(captured_record_bytes.get());
            }
        }
        result
    }

    fn resolve(&self, operation: &str, kind: &str) -> Result<ops::OperationSpec, StorageError> {
        if let Some(spec) = ops::registry().get(operation) {
            if spec.kind.as_str() != kind {
                return Err(StorageError::protocol("Storage operation kind mismatch"));
            }
            return Ok(*spec);
        }
        if let Some(spec) = ops::plugins::dynamic(operation, kind) {
            return Ok(spec);
        }
        if ops::catalog().iter().any(|(name, ..)| name == operation) {
            return Err(StorageError::new(
                "database_unsupported",
                format!("{operation} is not yet ported to the Rust SQL authority"),
            ));
        }
        let _ = Kind::Query;
        Err(StorageError::protocol("Unknown storage operation"))
    }

    /// `NativeCheckpointService.dispatch` with no configured controller: the
    /// capability is advertised as unavailable and control is unsupported.
    fn native_checkpoint(&self, kind: &str, operation: &str, payload: &Value) -> Result<Value, StorageError> {
        // `_owner`: a positive 64-bit owner; no configuration pins one here.
        let owner_ok =
            matches!(payload.get("owner_user_id"), Some(Value::Number(n)) if n.as_u64().is_some_and(|v| v > 0));
        if !owner_ok {
            return Err(StorageError::new("database_forbidden", "native checkpoint owner differs"));
        }
        let payload = payload.as_object().expect("owner proved an object");
        const MAXIMUM_RESPONSE: i64 = MAX_FRAME_BYTES as i64 - 4096;
        if operation == "native_checkpoint.capabilities" {
            if kind != "query" || payload.len() != 1 || !payload.contains_key("owner_user_id") {
                return Err(StorageError::protocol("invalid checkpoint capability admission"));
            }
            let mut result = Map::new();
            result.insert("format".into(), Value::String("tofu.native-checkpoint-transport/v1".into()));
            result.insert("available".into(), Value::Bool(false));
            result.insert("runtime_id".into(), Value::String(self.native_checkpoint_runtime_id.clone()));
            result.insert("maximum_response_bytes".into(), Value::from(MAXIMUM_RESPONSE));
            return Ok(Value::Object(result));
        }
        let keys: std::collections::BTreeSet<&str> = payload.keys().map(String::as_str).collect();
        let expected: std::collections::BTreeSet<&str> =
            ["owner_user_id", "format", "runtime_id", "operation", "payload", "response_bytes"].into_iter().collect();
        let response_bytes_ok = matches!(&payload.get("response_bytes"), Some(Value::Number(n))
            if n.as_i64().is_some_and(|v| 0 < v && v <= MAXIMUM_RESPONSE));
        if operation != "native_checkpoint.control"
            || kind != "command"
            || keys != expected
            || payload["format"] != Value::String("tofu.native-checkpoint-transport/v1".into())
            || payload["runtime_id"] != Value::String(self.native_checkpoint_runtime_id.clone())
            || !payload["operation"].as_str().is_some_and(|name| NATIVE_CHECKPOINT_OPERATIONS.contains(&name))
            || !payload["payload"].is_object()
            || !response_bytes_ok
        {
            return Err(StorageError::protocol("invalid or stale checkpoint control admission"));
        }
        Err(StorageError::new("database_unsupported", "selected authority lacks durable native checkpoint control"))
    }
}
