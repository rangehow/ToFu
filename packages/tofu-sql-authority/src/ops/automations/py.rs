//! Python semantics shared by the automation operations: exception classes,
//! container access with Python's KeyError/TypeError/AttributeError split,
//! `PrincipalContext`, `check_json_bounds`, and private copies of the
//! record/worker-job/attempt owners these operations compose in-transaction.

use std::collections::BTreeSet;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::schema::repr;
use crate::codec;
use crate::error::{fail, internal, Failure, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

/// A raised Python exception, by the class the handlers catch.
#[derive(Debug)]
pub enum PyExc {
    Value(String),
    /// `AutomationConflict` (a `ValueError` subclass).
    Conflict(String),
    /// `str(KeyError(key))`, i.e. `repr(key)`.
    Key(String),
    Permission(String),
    /// TypeError, AttributeError, IndexError: never caught by these handlers'
    /// semantic `except` clauses.
    Type,
    Storage(Failure),
}

impl From<Failure> for PyExc {
    fn from(failure: Failure) -> Self {
        PyExc::Storage(failure)
    }
}

impl PyExc {
    /// The exception escaping uncaught to the adapter.
    pub fn raise(self) -> Failure {
        match self {
            PyExc::Storage(failure) => failure,
            _ => Failure::Internal("uncaught automation exception".into()),
        }
    }

    /// `except (ValueError, KeyError[, PermissionError]): _fail(str(exc))`.
    pub fn message(&self, permission: bool) -> Option<String> {
        match self {
            PyExc::Value(message) | PyExc::Conflict(message) | PyExc::Key(message) => Some(message.clone()),
            PyExc::Permission(message) if permission => Some(message.clone()),
            _ => None,
        }
    }
}

pub type Py<T> = Result<T, PyExc>;

pub fn value_error<T>(message: &str) -> Py<T> {
    Err(PyExc::Value(message.to_owned()))
}

fn key_error(key: &str) -> PyExc {
    PyExc::Key(repr(&Value::String(key.to_owned())))
}

/// `value[key]` on a JSON-decoded container.
pub fn item<'v>(value: &'v Value, key: &str) -> Py<&'v Value> {
    match value {
        Value::Object(map) => map.get(key).ok_or_else(|| key_error(key)),
        _ => Err(PyExc::Type),
    }
}

/// `value[key]` where `key` is itself a decoded value.
pub fn item_by<'v>(value: &'v Value, key: &Value) -> Py<&'v Value> {
    match (value, key) {
        (Value::Object(map), Value::String(text)) => map.get(text).ok_or_else(|| key_error(text)),
        (Value::Object(_), Value::Array(_) | Value::Object(_)) => Err(PyExc::Type),
        (Value::Object(_), other) => Err(PyExc::Key(repr(other))),
        _ => Err(PyExc::Type),
    }
}

/// `value.get(key)` (AttributeError on a non-dict).
pub fn get<'v>(value: &'v Value, key: &str) -> Py<Option<&'v Value>> {
    match value {
        Value::Object(map) => Ok(map.get(key)),
        _ => Err(PyExc::Type),
    }
}

static NULL: Value = Value::Null;

/// `value.get(key)` with `None` for a missing key.
pub fn get_or_none<'v>(value: &'v Value, key: &str) -> Py<&'v Value> {
    Ok(get(value, key)?.unwrap_or(&NULL))
}

/// `value in {'a', 'b'}`: unhashable containers are a TypeError.
pub fn in_set(value: &Value, members: &[&str]) -> Py<bool> {
    match value {
        Value::Array(_) | Value::Object(_) => Err(PyExc::Type),
        Value::String(text) => Ok(members.contains(&text.as_str())),
        _ => Ok(false),
    }
}

/// `value in ('a', 'b')` (tuple membership compares with `==`).
pub fn in_tuple(value: &Value, members: &[&str]) -> bool {
    value.as_str().is_some_and(|text| members.contains(&text))
}

/// `value.startswith(prefix)` (AttributeError on a non-str).
pub fn starts_with(value: &Value, prefix: &str) -> Py<bool> {
    match value {
        Value::String(text) => Ok(text.starts_with(prefix)),
        _ => Err(PyExc::Type),
    }
}

/// `prefix + value` (TypeError unless `value` is a str).
pub fn concat(prefix: &str, value: &Value) -> Py<String> {
    match value {
        Value::String(text) => Ok(format!("{prefix}{text}")),
        _ => Err(PyExc::Type),
    }
}

/// `value or ''` compared as a value.
pub fn or_empty(value: &Value) -> Value {
    if truthy(value) {
        value.clone()
    } else {
        Value::String(String::new())
    }
}

pub fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// `f'{owner}:{sha256(tenant)[:32]}:'`.
pub fn owner_prefix(owner: &Value, tenant: &str) -> String {
    format!("{}:{}:", python_str(owner), &sha256_hex(tenant)[..32])
}

pub fn wall_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or(0)
}

/// `value / 1000` for an int/float JSON value (Python true division).
pub fn seconds(value: &Value) -> Value {
    let milliseconds = match value {
        Value::Number(number) => number.as_f64().unwrap_or(0.0),
        Value::Bool(flag) => *flag as i64 as f64,
        _ => 0.0,
    };
    crate::json::float_value(milliseconds / 1000.0)
}

/// `value - int(time.time() * 1000) > bound` for a numeric payload value.
pub fn clock_ahead(value: &Value, bound: i64) -> bool {
    let now = wall_ms();
    match value {
        Value::Number(number) => match number.as_i64() {
            Some(integer) => integer - now > bound,
            None => number.as_f64().unwrap_or(0.0) - now as f64 > bound as f64,
        },
        Value::Bool(flag) => *flag as i64 - now > bound,
        _ => false,
    }
}

/// `str.strip()` with Python's whitespace set.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// `str(value or '')`.
fn str_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(value) if truthy(value) => python_str(value),
        _ => String::new(),
    }
}

/// `lib/automations/conditions.py::check_json_bounds`.
pub fn check_json_bounds(value: &Value, max_depth: usize, max_nodes: usize) -> Py<()> {
    const SAFE_INTEGER_MAX: u64 = 9_007_199_254_740_991;
    let mut pending = vec![(value, 0usize)];
    let mut nodes = 0usize;
    while let Some((current, depth)) = pending.pop() {
        nodes += 1;
        if depth > max_depth || nodes > max_nodes {
            return value_error("JSON exceeds depth or node budget");
        }
        match current {
            Value::Object(map) => pending.extend(map.values().map(|child| (child, depth + 1))),
            Value::Array(items) => pending.extend(items.iter().map(|child| (child, depth + 1))),
            Value::Number(number) => {
                let magnitude = match (number.as_i64(), number.as_u64()) {
                    (Some(integer), _) => Some(integer.unsigned_abs()),
                    (None, Some(integer)) => Some(integer),
                    _ => None,
                };
                if magnitude.is_some_and(|magnitude| magnitude > SAFE_INTEGER_MAX) {
                    return value_error("JSON integers must round-trip exactly through browser clients");
                }
            }
            _ => {}
        }
    }
    Ok(())
}

/// `lib/identity.py::PrincipalContext` (private port; shared owner: identity).
#[derive(Clone, Debug)]
pub struct Principal {
    pub kind: String,
    pub subject_id: String,
    pub owner_user_id: Option<i64>,
    pub tenant_id: Option<String>,
    pub scopes: BTreeSet<String>,
}

/// Python `int(text, 10)` for ASCII digit text.
fn parse_python_int(text: &str) -> Option<i64> {
    let text = py_strip(text);
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text.strip_prefix('+').unwrap_or(text)),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    let clean: String = digits.chars().filter(|c| *c != '_').collect();
    if !clean.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let value: i64 = clean.parse().ok()?;
    Some(if negative { -value } else { value })
}

/// `lib/identity.py::require_user_id`.
fn require_user_id(value: &Value, context: &str) -> Py<i64> {
    let numeric = || PyExc::Value(format!("{context} requires a numeric user_id"));
    let user_id = match value {
        Value::Number(number) => number.as_i64().ok_or_else(numeric)?,
        Value::String(text) => parse_python_int(text).ok_or_else(numeric)?,
        _ => return Err(numeric()),
    };
    if user_id <= 0 {
        return Err(PyExc::Value(format!("{context} requires a positive user_id")));
    }
    Ok(user_id)
}

/// `_normalized_scopes(values)`.
fn normalized_scopes(values: &Value) -> Py<BTreeSet<String>> {
    let items: Vec<Value> = match values {
        value if !truthy(value) => Vec::new(),
        Value::Array(items) => items.clone(),
        Value::String(text) => text.chars().map(|c| Value::String(c.to_string())).collect(),
        Value::Object(map) => map.keys().map(|key| Value::String(key.clone())).collect(),
        _ => return Err(PyExc::Type),
    };
    let mut scopes = BTreeSet::new();
    for value in &items {
        let scope = py_strip(&str_or_empty(Some(value))).to_owned();
        if scope.is_empty() || scope.chars().count() > 128 {
            return value_error("principal scopes must be 1..128 characters");
        }
        scopes.insert(scope);
    }
    Ok(scopes)
}

impl Principal {
    /// `PrincipalContext.from_payload(payload)` and `__post_init__`.
    pub fn from_payload(payload: &Value) -> Py<Principal> {
        let Value::Object(map) = payload else { return Err(PyExc::Type) };
        let kind = str_or_empty(map.get("kind"));
        let subject_id = str_or_empty(map.get("subject_id"));
        let owner = map.get("owner_user_id").unwrap_or(&NULL);
        let tenant = map.get("tenant_id").unwrap_or(&NULL);
        let scopes = normalized_scopes(map.get("scopes").unwrap_or(&NULL))?;
        Self::construct(kind, subject_id, owner, tenant, scopes)
    }

    fn construct(kind: String, subject: String, owner: &Value, tenant: &Value, scopes: BTreeSet<String>) -> Py<Self> {
        if kind != "user" && kind != "system" {
            return value_error("principal kind must be user or system");
        }
        let subject_id = py_strip(&subject).to_owned();
        if subject_id.is_empty() || subject_id.chars().count() > 256 {
            return value_error("principal subject_id must be 1..256 characters");
        }
        if kind == "user" && owner.is_null() {
            return value_error("user principal requires owner_user_id");
        }
        let owner_user_id =
            if owner.is_null() { None } else { Some(require_user_id(owner, "principal owner_user_id")?) };
        let tenant = py_strip(&str_or_empty(Some(tenant))).to_owned();
        if tenant.chars().count() > 256 {
            return value_error("principal tenant_id must be at most 256 characters");
        }
        let tenant_id = if tenant.is_empty() { None } else { Some(tenant) };
        Ok(Principal { kind, subject_id, owner_user_id, tenant_id, scopes })
    }

    /// `PrincipalContext.system(subject_id=, owner_user_id=, tenant_id=, scopes=)`.
    pub fn system(subject_id: &str, owner: i64, tenant: &str, scopes: &[&str]) -> Py<Principal> {
        let scopes = scopes.iter().map(|scope| (*scope).to_owned()).collect();
        Self::construct("system".into(), subject_id.into(), &Value::from(owner), &Value::from(tenant), scopes)
    }

    pub fn to_payload(&self) -> Value {
        json!({
            "kind": self.kind,
            "subject_id": self.subject_id,
            "owner_user_id": self.owner_user_id,
            "tenant_id": self.tenant_id,
            "scopes": self.scopes.iter().collect::<Vec<_>>(),
        })
    }

    pub fn require_owner(&self, context: &str) -> Py<i64> {
        self.owner_user_id.ok_or_else(|| PyExc::Permission(format!("{context} requires an owning user principal")))
    }

    pub fn has_scope(&self, scope: &Value) -> bool {
        let requested = py_strip(&str_or_empty(Some(scope))).to_owned();
        !requested.is_empty() && (self.scopes.contains("admin") || self.scopes.contains(&requested))
    }

    pub fn require_scope(&self, scope: &Value) -> Py<()> {
        if self.has_scope(scope) {
            return Ok(());
        }
        Err(PyExc::Permission(format!(
            "principal {} lacks scope {}",
            repr(&Value::String(self.subject_id.clone())),
            repr(scope)
        )))
    }
}

/// `for scope in value: ...` over a decoded value (TypeError if not iterable).
pub fn iterate(value: &Value) -> Py<Vec<Value>> {
    match value {
        Value::Array(items) => Ok(items.clone()),
        Value::String(text) => Ok(text.chars().map(|c| Value::String(c.to_string())).collect()),
        Value::Object(map) => Ok(map.keys().map(|key| Value::String(key.clone())).collect()),
        _ => Err(PyExc::Type),
    }
}

// private port of lib/storage_authority_sql/operations_pkg/_records.py::_record_delete_owned; shared owner: core
pub fn record_delete_owned(session: &Session<'_>, namespace: &str, key: &str, expected: i64) -> OpResult<bool> {
    let count = session.execute(
        "DELETE FROM storage_records WHERE namespace = ? AND record_key = ? AND version = ?",
        &params![namespace, key, expected],
    )?;
    if count == 0
        && session
            .fetch_one(
                "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
                &params![namespace, key],
            )?
            .is_some()
    {
        return fail("database_conflict", "Storage record version conflict");
    }
    Ok(count > 0)
}

/// Invoke another domain's registered handler in this transaction (the
/// Python owner calls the same function directly).
fn call(session: &Session<'_>, operation: &str, payload: Value) -> OpResult<Value> {
    let Some(spec) = crate::ops::registry().get(operation) else {
        return internal(format!("{operation} is not registered"));
    };
    let Value::Object(payload) = payload else { return internal("payload") };
    (spec.handler)(session, &payload)
}

/// `_worker_job_get(session, {'task_id':, 'user_id':}, lock=...)`; the
/// enclosing BEGIN IMMEDIATE already serializes the row.
pub fn worker_job_get(session: &Session<'_>, task_id: &Value, owner: &Value) -> OpResult<Value> {
    call(session, "worker_job.get", json!({"task_id": task_id, "user_id": owner}))
}

/// `_worker_job_claim_state`.
pub fn worker_job_claim_state(
    session: &Session<'_>,
    task_id: &Value,
    worker_id: &Value,
    fence: &Value,
    now: &Value,
) -> OpResult<Value> {
    call(
        session,
        "worker_job.claim_state",
        json!({"task_id": task_id, "worker_id": worker_id, "fencing_token": fence, "now_ms": now}),
    )
}

/// `_worker_job_request_cancel`.
pub fn worker_job_request_cancel(session: &Session<'_>, payload: Value) -> OpResult<Value> {
    call(session, "worker_job.request_cancel", payload)
}

const WORKER_JOB_COLUMNS: &str = "task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, \
     request_digest, status, priority, available_at_ms, claim_owner, \
     lease_deadline_ms, lease_authority_epoch, fencing_token, attempt_no, heartbeat_at_ms, \
     cancel_sequence, wake_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, \
     result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms";
const TERMINAL_JOB_STATUSES: &[&str] = &["succeeded", "failed", "cancelled"];
const MAX_CLOCK_MS: i64 = 9_223_372_036_854_775_000;
const MAX_JOB_PAYLOAD_BYTES: usize = 1024 * 1024;
const MAX_CLOCK_SKEW_MS: i64 = 24 * 60 * 60 * 1000;
const WORKER_LEASE_MAX_NOW_MS: i64 = 9_223_372_036_854_475_000;

fn column_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(integer) => integer.to_string(),
        SqlValue::Real(real) => crate::json::python_float_repr(*real),
        SqlValue::Text(text) => text.clone(),
        SqlValue::Blob(bytes) => format!("b{:?}", String::from_utf8_lossy(bytes)),
    }
}

fn column_text(value: &SqlValue) -> String {
    if value.truthy() {
        column_str(value)
    } else {
        String::new()
    }
}

fn load_or_empty(value: &SqlValue) -> OpResult<Value> {
    let loaded = codec::load(value)?;
    Ok(if truthy(&loaded) { loaded } else { Value::Object(Map::new()) })
}

fn col_str(row: &Row, column: &str) -> OpResult<String> {
    Ok(column_str(row.require(column)?))
}

// private port of lib/storage_authority_sql/operations_pkg/_worker_jobs.py::_job_document; shared owner: worker_jobs
fn job_document(row: &Row) -> OpResult<Value> {
    Ok(json!({
        "taskId": col_str(row, "task_id")?,
        "userId": row.int("user_id")?,
        "tenantId": column_text(row.require("tenant_id")?),
        "taskKind": col_str(row, "task_kind")?,
        "payload": load_or_empty(row.require("payload_json")?)?,
        "idempotencyKey": col_str(row, "idempotency_key")?,
        "status": col_str(row, "status")?,
        "priority": row.int("priority")?,
        "availableAtMs": row.int("available_at_ms")?,
        "claimOwner": column_text(row.require("claim_owner")?),
        "leaseDeadlineMs": row.int("lease_deadline_ms")?,
        "fencingToken": row.int("fencing_token")?,
        "attempt": row.int("attempt_no")?,
        "heartbeatAtMs": row.int("heartbeat_at_ms")?,
        "cancelSequence": row.int("cancel_sequence")?,
        "wakeSequence": row.int("wake_sequence")?,
        "cancelRequestedAtMs": row.int("cancel_requested_at_ms")?,
        "cancelReason": column_text(row.require("cancel_reason")?),
        "replayCursor": row.int("replay_cursor")?,
        "resultRef": column_text(row.require("result_ref")?),
        "error": load_or_empty(row.require("error_json")?)?,
        "createdAtMs": row.int("created_at_ms")?,
        "updatedAtMs": row.int("updated_at_ms")?,
        "terminalAtMs": row.int("terminal_at_ms")?,
    }))
}

fn job_row(session: &Session<'_>, task_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id = ?"),
        &params![task_id],
    )
}

fn job_row_for_update(session: &Session<'_>, task_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one_for_update(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id = ?"),
        &params![task_id],
    )
}

// private port of lib/storage_authority_sql/operations_pkg/_worker_jobs.py::_worker_job_enqueue_owned; shared owner: worker_jobs
pub fn worker_job_enqueue_owned(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let tenant_id = match payload.get("tenant_id") {
        None => String::new(),
        Some(Value::String(text)) if text.chars().count() <= 256 => text.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid tenant_id in storage request"),
    };
    let task_kind = required_text(payload, "task_kind", 128)?;
    let idempotency_key = required_text(payload, "idempotency_key", 256)?;
    let priority = integer(payload, "priority", Some(100), Some(0), Some(1000))?;
    let now_ms = integer(payload, "now_ms", None, Some(0), Some(MAX_CLOCK_MS))?;
    if now_ms - wall_ms() > MAX_CLOCK_SKEW_MS {
        return fail("database_protocol_error", "Worker job clock is too far ahead of the server clock");
    }
    let available_at_ms = integer(payload, "available_at_ms", Some(now_ms), Some(0), Some(MAX_CLOCK_MS))?;
    let job_payload = match payload.get("payload") {
        None => Value::Object(Map::new()),
        Some(value @ Value::Object(_)) => value.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid payload in worker job"),
    };
    let encoded_payload = codec::dump(&job_payload)?;
    if encoded_payload.len() > MAX_JOB_PAYLOAD_BYTES {
        return fail("storage_payload_too_large", format!("Worker job payload exceeds {MAX_JOB_PAYLOAD_BYTES} bytes"));
    }
    let request_digest = hex::encode(Sha256::digest(codec::dump(&json!({
        "tenantId": tenant_id,
        "taskKind": task_kind,
        "payload": job_payload,
    }))?));
    session.lock_key("worker_job_idempotency", &format!("{user_id}:{idempotency_key}"));
    let existing = session.fetch_one(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE user_id = ? AND idempotency_key = ?"),
        &params![user_id, &idempotency_key],
    )?;
    if let Some(existing) = existing {
        if col_str(&existing, "request_digest")? != request_digest {
            return fail("database_conflict", "Worker job idempotency key was reused for another request");
        }
        return Ok(json!({"created": false, "job": job_document(&existing)?}));
    }
    if job_row(session, &task_id)?.is_some() {
        return fail("database_conflict", "Worker job task_id already exists");
    }
    session.execute(
        "INSERT INTO storage_worker_jobs(\
         task_id, user_id, tenant_id, task_kind, payload_json, \
         idempotency_key, request_digest, status, priority, available_at_ms, \
         created_at_ms, updated_at_ms) \
         VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
        &params![
            &task_id,
            user_id,
            &tenant_id,
            &task_kind,
            encoded_payload,
            &idempotency_key,
            &request_digest,
            priority,
            available_at_ms,
            now_ms,
            now_ms,
        ],
    )?;
    session.hint_queue_changed();
    let Some(created) = job_row(session, &task_id)? else {
        return fail("database_internal", "Worker job insert was not visible");
    };
    Ok(json!({"created": true, "job": job_document(&created)?}))
}

// private port of lib/storage_authority_sql/operations_pkg/_worker_jobs.py::_worker_job_wake_owned; shared owner: worker_jobs
pub fn worker_job_wake_owned(
    session: &Session<'_>,
    task_id: &str,
    owner: &Value,
    tenant: &str,
    now_ms: &Value,
) -> OpResult<Value> {
    session.lock_key("worker_job", task_id);
    let Some(row) = job_row_for_update(session, task_id)? else { return Ok(Value::Null) };
    if !codec::py_eq(&Value::from(row.int("user_id")?), owner)
        || row.require("tenant_id")?.as_str() != Some(tenant)
        || TERMINAL_JOB_STATUSES.contains(&row.get("status").as_str().unwrap_or(""))
    {
        return Ok(Value::Null);
    }
    let sequence = row.int("wake_sequence")? + 1;
    if sequence > MAX_CLOCK_MS {
        return fail("database_integrity", "Worker wake sequence overflows");
    }
    session.hint_queue_changed();
    let snapshot = session.worker_lease_authority()?;
    let valid = snapshot.epoch.len() == 32
        && snapshot.epoch.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        && 0 < snapshot.lease_now_ms
        && snapshot.lease_now_ms <= WORKER_LEASE_MAX_NOW_MS
        && 0 < snapshot.utc_now_ms
        && snapshot.utc_now_ms <= snapshot.lease_now_ms;
    if !valid {
        return fail("database_internal", "Worker lease authority clock is invalid");
    }
    let available = row.int("available_at_ms")?.min(snapshot.utc_now_ms);
    session.execute(
        "UPDATE storage_worker_jobs SET wake_sequence=?, available_at_ms=?, updated_at_ms=? WHERE task_id=?",
        &params![sequence, available, crate::session::json_param(now_ms)?, task_id],
    )?;
    match job_row(session, task_id)? {
        Some(row) => job_document(&row),
        None => internal("'NoneType' object is not subscriptable"),
    }
}

// private port of lib/storage_authority_sql/operations_pkg/_turns_read.py::_attempt_get (attempt_id/user_id only); shared owner: turns
pub fn attempt_get(session: &Session<'_>, owner: &Value, attempt_id: &Value) -> OpResult<Value> {
    let mut payload = Payload::new();
    payload.insert("attempt_id".into(), attempt_id.clone());
    payload.insert("user_id".into(), owner.clone());
    let attempt_id = required_text(&payload, "attempt_id", 128)?;
    let user_id = integer(&payload, "user_id", None, Some(1), None)?;
    let row = session.fetch_one(
        "SELECT a.* FROM storage_generation_attempts AS a \
         JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id \
         WHERE a.attempt_id=? AND t.user_id=?",
        &params![&attempt_id, user_id],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    // `_attempt_public`: the resume anchor must decode to an object.
    if !codec::load(row.require("resume_anchor_json")?)?.is_object() {
        return fail("database_integrity", "Stored attempt resume anchor must be an object");
    }
    Ok(json!({
        "attemptId": column_str(row.require("attempt_id")?),
        "conversationId": column_str(row.require("conversation_id")?),
        "turnId": column_str(row.require("turn_id")?),
        "taskId": column_text(row.require("task_id")?),
        "startedAt": row.require("started_at")?.to_json()?,
    }))
}
