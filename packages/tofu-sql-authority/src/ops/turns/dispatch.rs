//! `operations_pkg/_worker_dispatch.py::_turn_attempt_dispatch_worker` with
//! private ports of `_worker_jobs._worker_job_enqueue{,_owned}`,
//! `_job_row`/`_job_document` (shared owner: worker_jobs) and
//! `lib/identity.py::PrincipalContext` (shared owner: identity).

use std::collections::BTreeSet;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::core::{attempt_public, now_ms, Obj};
use super::normalize::py_strip;
use crate::codec::{dump, load, py_eq};
use crate::error::{fail, OpResult};
use crate::json::{python_float_repr, python_str, truthy};
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const JOB_CONTRACT: &str = "tofu.conversation-attempt-job/v1";
const JOB_KIND: &str = "conversation-attempt";
const MAX_CLOCK_MS: i64 = 9_223_372_036_854_775_000;
const MAX_JOB_PAYLOAD_BYTES: usize = 1024 * 1024;
const MAX_CLOCK_SKEW_MS: i64 = 24 * 60 * 60 * 1000;
const RESERVED_TASK_KINDS: &[&str] = &["automation-clock", "automation-run", "automation-event"];
const WORKER_JOB_COLUMNS: &str = "task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, request_digest, status, priority, available_at_ms, claim_owner, lease_deadline_ms, lease_authority_epoch, fencing_token, attempt_no, heartbeat_at_ms, cancel_sequence, wake_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms";

// ── lib/identity.py::PrincipalContext ────────────────────────────────────

struct Principal {
    kind: String,
    subject_id: String,
    owner_user_id: Option<i64>,
    tenant_id: Option<String>,
    scopes: BTreeSet<String>,
}

/// Python `str(value or "")`.
fn str_or_empty(value: &Value) -> String {
    if !truthy(value) {
        return String::new();
    }
    match value {
        Value::Number(number) if number.is_f64() => python_float_repr(number.as_f64().unwrap_or(0.0)),
        other => python_str(other),
    }
}

/// `require_user_id`: `None` is any raised error.
fn require_user_id(value: &Value) -> Option<i64> {
    let user_id = match value {
        Value::Number(number) => number.as_i64()?,
        Value::String(text) => {
            let text = py_strip(text);
            let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
            if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
                return None;
            }
            let clean: String = text.chars().filter(|c| *c != '_').collect();
            if !clean.trim_start_matches(['+', '-']).chars().all(|c| c.is_ascii_digit()) {
                return None;
            }
            clean.parse().ok()?
        }
        _ => return None,
    };
    (user_id > 0).then_some(user_id)
}

impl Principal {
    /// `PrincipalContext.from_payload`; `None` is any raised error.
    fn from_payload(map: &Obj) -> Option<Principal> {
        let null = Value::Null;
        let kind = str_or_empty(map.get("kind").unwrap_or(&null));
        let subject = str_or_empty(map.get("subject_id").unwrap_or(&null));
        let owner = map.get("owner_user_id").unwrap_or(&null);
        let tenant = map.get("tenant_id").unwrap_or(&null);
        let raw_scopes = map.get("scopes").unwrap_or(&null);
        let items: Vec<Value> = match raw_scopes {
            value if !truthy(value) => vec![],
            Value::Array(items) => items.clone(),
            Value::String(text) => text.chars().map(|c| Value::String(c.to_string())).collect(),
            Value::Object(map) => map.keys().map(|key| Value::String(key.clone())).collect(),
            _ => return None,
        };
        let mut scopes = BTreeSet::new();
        for item in &items {
            let scope = py_strip(&str_or_empty(item)).to_owned();
            if scope.is_empty() || scope.chars().count() > 128 {
                return None;
            }
            scopes.insert(scope);
        }
        if kind != "user" && kind != "system" {
            return None;
        }
        let subject_id = py_strip(&subject).to_owned();
        if subject_id.is_empty() || subject_id.chars().count() > 256 {
            return None;
        }
        if kind == "user" && owner.is_null() {
            return None;
        }
        let owner_user_id = if owner.is_null() { None } else { Some(require_user_id(owner)?) };
        let tenant = py_strip(&str_or_empty(tenant)).to_owned();
        if tenant.chars().count() > 256 {
            return None;
        }
        let tenant_id = if tenant.is_empty() { None } else { Some(tenant) };
        Some(Principal { kind, subject_id, owner_user_id, tenant_id, scopes })
    }

    fn to_payload(&self) -> Value {
        json!({
            "kind": self.kind,
            "subject_id": self.subject_id,
            "owner_user_id": self.owner_user_id,
            "tenant_id": self.tenant_id,
            "scopes": self.scopes.iter().collect::<Vec<_>>(),
        })
    }
}

/// `_dispatch_principal`.
fn dispatch_principal(payload: &Payload, owner: i64) -> OpResult<Principal> {
    let Some(Value::Object(raw)) = payload.get("principal") else {
        return fail("database_protocol_error", "Conversation worker dispatch requires an explicit principal");
    };
    let Some(principal) = Principal::from_payload(raw) else {
        return fail("database_protocol_error", "Conversation worker dispatch principal is invalid");
    };
    let Some(principal_owner) = principal.owner_user_id else {
        return fail("database_protocol_error", "Conversation worker dispatch principal is invalid");
    };
    if !py_eq(&Value::Object(raw.clone()), &principal.to_payload()) {
        return fail("database_protocol_error", "Conversation worker dispatch principal is not canonical");
    }
    if principal_owner != owner {
        return fail("database_conflict", "Conversation worker dispatch principal does not own the attempt");
    }
    Ok(principal)
}

// ── _worker_jobs.py (private copies) ─────────────────────────────────────

fn column_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(integer) => integer.to_string(),
        SqlValue::Real(real) => python_float_repr(*real),
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
    let loaded = load(value)?;
    Ok(if truthy(&loaded) { loaded } else { Value::Object(Obj::new()) })
}

fn col_str(row: &Row, column: &str) -> OpResult<String> {
    Ok(column_str(row.require(column)?))
}

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

fn optional_text(payload: &Payload, key: &str, maximum: usize) -> OpResult<String> {
    match payload.get(key) {
        None => Ok(String::new()),
        Some(Value::String(text)) if text.chars().count() <= maximum => Ok(text.clone()),
        Some(_) => fail("database_protocol_error", format!("Invalid {key} in storage request")),
    }
}

/// `_worker_job_enqueue` (reserved-kind guard + `_worker_job_enqueue_owned`).
fn worker_job_enqueue(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    if RESERVED_TASK_KINDS.contains(&required_text(payload, "task_kind", 128)?.as_str()) {
        return fail("database_protocol_error", "Automation jobs require their atomic owner");
    }
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let tenant_id = optional_text(payload, "tenant_id", 256)?;
    let task_kind = required_text(payload, "task_kind", 128)?;
    let idempotency_key = required_text(payload, "idempotency_key", 256)?;
    let priority = integer(payload, "priority", Some(100), Some(0), Some(1000))?;
    let client_now = integer(payload, "now_ms", None, Some(0), Some(MAX_CLOCK_MS))?;
    if client_now - now_ms() > MAX_CLOCK_SKEW_MS {
        return fail("database_protocol_error", "Worker job clock is too far ahead of the server clock");
    }
    let available_at_ms = integer(payload, "available_at_ms", Some(client_now), Some(0), Some(MAX_CLOCK_MS))?;
    let job_payload = match payload.get("payload") {
        None => Value::Object(Obj::new()),
        Some(value @ Value::Object(_)) => value.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid payload in worker job"),
    };
    let encoded_payload = dump(&job_payload)?;
    if encoded_payload.len() > MAX_JOB_PAYLOAD_BYTES {
        return fail("storage_payload_too_large", format!("Worker job payload exceeds {MAX_JOB_PAYLOAD_BYTES} bytes"));
    }
    let request_digest = hex::encode(Sha256::digest(dump(&json!({
        "tenantId": tenant_id,
        "taskKind": task_kind,
        "payload": job_payload,
    }))?));
    session.lock_key("worker_job_idempotency", &format!("{user_id}:{idempotency_key}"));
    let existing = session.fetch_one(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE user_id = ? AND idempotency_key = ?"),
        &params![user_id, idempotency_key.clone()],
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
        "INSERT INTO storage_worker_jobs(task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, request_digest, status, priority, available_at_ms, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
        &params![
            task_id.clone(),
            user_id,
            tenant_id,
            task_kind,
            encoded_payload,
            idempotency_key,
            request_digest,
            priority,
            available_at_ms,
            client_now,
            client_now
        ],
    )?;
    session.hint_queue_changed();
    let Some(created) = job_row(session, &task_id)? else {
        return fail("database_internal", "Worker job insert was not visible");
    };
    Ok(json!({"created": true, "job": job_document(&created)?}))
}

// ── _worker_dispatch.py ──────────────────────────────────────────────────

/// `_turn_attempt_dispatch_worker`.
pub fn turn_attempt_dispatch_worker(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let now = integer(payload, "now_ms", None, Some(0), Some(MAX_CLOCK_MS))?;
    let priority = integer(payload, "priority", Some(100), Some(0), Some(1000))?;
    let task_id = format!("conversation-attempt:{attempt_id}");
    session.lock_key("attempt_dispatch", &attempt_id);
    let Some(attempt) = session.fetch_one(
        "SELECT a.* FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND t.user_id=?",
        &params![attempt_id.clone(), user_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let Some(turn) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=? AND user_id=?",
        &[attempt.get("turn_id").clone(), SqlValue::Integer(user_id)],
    )?
    else {
        return Ok(Value::Null);
    };
    let principal = dispatch_principal(payload, user_id)?;
    let job_payload = json!({
        "contract": JOB_CONTRACT,
        "conversationId": column_str(attempt.get("conversation_id")),
        "turnId": column_str(attempt.get("turn_id")),
        "attemptId": column_str(attempt.get("attempt_id")),
        "principal": principal.to_payload(),
        "baseProjectionRevision": attempt.opt_int("base_projection_revision").unwrap_or(0),
        "operation": column_str(attempt.get("operation")),
    });
    let mut enqueue = Payload::new();
    enqueue.insert("task_id".into(), Value::String(task_id.clone()));
    enqueue.insert("user_id".into(), Value::from(user_id));
    enqueue.insert("tenant_id".into(), Value::String(principal.tenant_id.clone().unwrap_or_default()));
    enqueue.insert("task_kind".into(), Value::String(JOB_KIND.into()));
    enqueue.insert("idempotency_key".into(), Value::String(task_id.clone()));
    enqueue.insert("payload".into(), job_payload);
    enqueue.insert("priority".into(), Value::from(priority));
    enqueue.insert("available_at_ms".into(), Value::from(now));
    enqueue.insert("now_ms".into(), Value::from(now));
    let existing = column_text(attempt.get("task_id"));
    if !existing.is_empty() {
        if existing != task_id {
            return fail("database_conflict", "Conversation attempt is already bound to another executor");
        }
        if job_row(session, &task_id)?.is_none() {
            return fail("database_conflict", "Conversation attempt binding has no durable worker job");
        }
        let replay = worker_job_enqueue(session, &enqueue)?;
        return Ok(json!({
            "created": false,
            "idempotentReplay": true,
            "attempt": attempt_public(&attempt)?,
            "job": replay["job"].clone(),
        }));
    }
    if column_str(attempt.get("status")) != "pending" || column_text(turn.get("current_attempt_id")) != attempt_id {
        return fail("database_conflict", "Conversation attempt is not dispatchable");
    }
    let enqueued = worker_job_enqueue(session, &enqueue)?;
    let mut bind = Payload::new();
    bind.insert("attempt_id".into(), Value::String(attempt_id));
    bind.insert("task_id".into(), Value::String(task_id));
    bind.insert("user_id".into(), Value::from(user_id));
    let bound = super::write::turn_attempt_bind(session, &bind)?;
    let Value::Object(mut public_attempt) = bound else {
        return fail("database_conflict", "Conversation attempt disappeared during worker dispatch");
    };
    let events = public_attempt.shift_remove("_conversationSyncAttemptEvents").unwrap_or(json!([]));
    Ok(json!({
        "created": truthy(&enqueued["created"]),
        "attempt": Value::Object(public_attempt),
        "job": enqueued["job"].clone(),
        "_conversationSyncAttemptEvents": events,
    }))
}
