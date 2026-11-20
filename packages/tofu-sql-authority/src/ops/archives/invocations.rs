//! `operations_pkg/_invocations.py`: immutable child birth evidence in the
//! task-result authority, with a private port of
//! `operations_pkg/_worker_jobs.py::_require_conversation_publication_claim`
//! (shared owner: worker_jobs).

use serde_json::{json, Map, Value};

use super::task_result_codec::{encoded_task_result_document, task_result_get};
use super::worker_process::process_claim_protected;
use crate::codec::py_eq;
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::ops::conversations::support::{load, now_ms, py_int, py_str};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Session, SqlValue};

const WORKER_JOB_COLUMNS: &str = "task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, \
request_digest, status, priority, available_at_ms, claim_owner, \
lease_deadline_ms, lease_authority_epoch, fencing_token, attempt_no, heartbeat_at_ms, \
cancel_sequence, wake_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, \
result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms";
const WORKER_LEASE_EPOCH_CHARACTERS: usize = 32;
const WORKER_LEASE_MAX_NOW_MS: i64 = 9223372036854475000;

/// Python `mapping.get(key)` on a value that must be a dict (`AttributeError`
/// otherwise, an internal failure).
fn dict_get<'v>(value: &'v Value, key: &str) -> OpResult<&'v Value> {
    static NULL: Value = Value::Null;
    match value {
        Value::Object(map) => Ok(map.get(key).unwrap_or(&NULL)),
        _ => internal(format!("AttributeError: .get({key}) on a non-dict")),
    }
}

/// `x or {}` for JSON values.
fn or_empty(value: &Value) -> Value {
    if truthy(value) {
        value.clone()
    } else {
        Value::Object(Map::new())
    }
}

/// `worker_root_for_task(session, task_id, user_id=)`.
fn worker_root_for_task(session: &Session<'_>, task_id: &str, user_id: Option<i64>) -> OpResult<Option<String>> {
    let mut current = task_id.to_owned();
    let mut seen = std::collections::HashSet::new();
    let mut conversation: Option<String> = None;
    let mut user_id = user_id;
    loop {
        if !seen.insert(current.clone()) {
            return fail("database_integrity", "Child invocation ancestry is cyclic");
        }
        if current.starts_with("conversation-attempt:") {
            return Ok(Some(current));
        }
        let Some(row) = session.fetch_one(
            "SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?",
            &params!["task_results", current.as_str()],
        )?
        else {
            return Ok(None);
        };
        let value = load(row.get("value_json"))?;
        let identity = match &value {
            Value::Object(map) => map.get("child_invocation").cloned().unwrap_or(Value::Null),
            _ => Value::Null,
        };
        if !truthy(&identity) {
            return Ok(None);
        }
        let Value::Object(identity) = identity else {
            return fail("database_integrity", "Child invocation identity is malformed");
        };
        let owner = match identity.get("user_id") {
            Some(Value::Number(number)) => number.as_i64(),
            _ => None,
        };
        let Some(owner) = owner.filter(|owner| *owner >= 1 && user_id.is_none_or(|user| user == *owner)) else {
            return fail("database_not_found", "Task not found");
        };
        user_id = Some(owner);
        let null = Value::Null;
        let conv = identity.get("conversation_id").unwrap_or(&null);
        let parent = identity.get("parent_task_id").unwrap_or(&null);
        let value_map = value.as_object().expect("child identity implies a mapping");
        let malformed = !py_eq(identity.get("child_task_id").unwrap_or(&null), &Value::String(current.clone()))
            || !py_eq(value_map.get("user_id").unwrap_or(&null), &Value::from(owner))
            || !py_eq(value_map.get("conv_id").unwrap_or(&null), conv)
            || !matches!(conv, Value::String(text) if !text.is_empty())
            || conversation.as_ref().is_some_and(|known| Some(known.as_str()) != conv.as_str())
            || !matches!(parent, Value::String(text) if !text.is_empty());
        if malformed {
            return fail("database_integrity", "Child invocation ancestry is malformed");
        }
        conversation = conv.as_str().map(str::to_owned);
        current = parent.as_str().expect("validated parent").to_owned();
    }
}

/// `_lease_authority(session)`: `(epoch, now)`.
fn lease_authority(session: &Session<'_>) -> OpResult<(String, i64)> {
    let snapshot = session.worker_lease_authority()?;
    let valid = snapshot.epoch.len() == WORKER_LEASE_EPOCH_CHARACTERS
        && snapshot.epoch.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        && 0 < snapshot.lease_now_ms
        && snapshot.lease_now_ms <= WORKER_LEASE_MAX_NOW_MS
        && 0 < snapshot.utc_now_ms
        && snapshot.utc_now_ms <= snapshot.lease_now_ms;
    if !valid {
        return fail("database_internal", "Worker lease authority clock is invalid");
    }
    Ok((snapshot.epoch, snapshot.lease_now_ms))
}

/// `_require_conversation_publication_claim(..., terminal=False, drain_entered=False)`.
fn require_conversation_publication_claim(
    session: &Session<'_>,
    attempt_id: &str,
    task_id: &str,
    user_id: i64,
    proof: &Value,
) -> OpResult<()> {
    let proof = match proof {
        Value::Null => None,
        Value::Object(map)
            if map.len() == 2
                && matches!(map.get("worker_id"), Some(Value::String(worker)) if !worker.is_empty() && worker.chars().count() <= 256)
                && matches!(map.get("fencing_token"), Some(Value::Number(number)) if number.as_i64().is_some_and(|token| token > 0)) =>
        {
            Some((map["worker_id"].as_str().unwrap().to_owned(), map["fencing_token"].as_i64().unwrap()))
        }
        _ => return fail("database_protocol_error", "Invalid worker publication claim"),
    };
    if task_id != format!("conversation-attempt:{attempt_id}") {
        if proof.is_some() {
            return fail("database_conflict", "Attempt has no durable worker binding");
        }
        return Ok(());
    }
    let Some((worker_id, fence)) = proof else {
        return fail("database_conflict", "Durable attempt requires a worker claim");
    };
    session.lock_key("worker_job", task_id);
    let row = session.fetch_one_for_update(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id=? AND user_id=?"),
        &params![task_id, user_id],
    )?;
    let (epoch, now) = lease_authority(session)?;
    let refused = || fail("database_conflict", "Worker claim cannot publish this attempt");
    let Some(row) = row else { return refused() };
    let live = py_str(row.get("status")) == "running"
        && py_str(row.get("claim_owner")) == worker_id
        && py_int(row.get("fencing_token"))? == fence
        && py_str(row.get("lease_authority_epoch")) == epoch
        && (py_int(row.get("lease_deadline_ms"))? > now || process_claim_protected(&py_str(row.get("claim_owner"))));
    if !live || *row.get("task_kind") != SqlValue::Text("conversation-attempt".into()) {
        return refused();
    }
    let job_payload = or_empty(&load(row.get("payload_json"))?);
    if !py_eq(dict_get(&job_payload, "attemptId")?, &Value::String(attempt_id.to_owned())) {
        return refused();
    }
    if py_int(row.get("cancel_requested_at_ms"))? > 0 {
        return refused();
    }
    Ok(())
}

/// `require_child(session, scope, conversation_id, user_id)`.
pub fn require_child(session: &Session<'_>, scope: &str, conversation_id: &str, user_id: i64) -> OpResult<Value> {
    let mut payload = Map::new();
    payload.insert("archive_scope".into(), Value::String(scope.to_owned()));
    payload.insert("user_id".into(), Value::from(user_id));
    let identity = resolve_child(session, &payload)?;
    let mismatched = !py_eq(dict_get(&identity, "archive_scope")?, &Value::String(scope.to_owned()))
        || !py_eq(dict_get(&identity, "conversation_id")?, &Value::String(conversation_id.to_owned()))
        || !py_eq(dict_get(&identity, "user_id")?, &Value::from(user_id));
    if mismatched {
        return fail("database_not_found", "Child invocation not found");
    }
    Ok(identity)
}

pub fn register_child(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let conv = required_text(payload, "conversation_id", 512)?;
    let parent = required_text(payload, "parent_task_id", 512)?;
    let child = required_text(payload, "child_task_id", 512)?;
    if child == parent || child.starts_with("conversation-attempt:") {
        return fail("database_conflict", "Child identity conflicts with its ancestry");
    }
    let scope = format!("invocation:{user_id}:{child}");
    let identity = json!({
        "user_id": user_id,
        "conversation_id": conv,
        "parent_task_id": parent,
        "child_task_id": child,
        "archive_scope": scope,
    });
    session.lock_key("conversation", &format!("{user_id}:{conv}"));
    if session
        .fetch_one(
            "SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?",
            &params![conv.as_str(), user_id],
        )?
        .is_none()
    {
        return fail("database_not_found", "Conversation not found");
    }
    let parent_row = task_result_get(session, &parent)?;
    let parent_value = parent_row.map(|row| or_empty(&row["value"])).unwrap_or(Value::Object(Map::new()));
    if !py_eq(dict_get(&parent_value, "user_id")?, &Value::from(user_id))
        || !py_eq(dict_get(&parent_value, "conv_id")?, &Value::String(conv.clone()))
    {
        let bound = session.fetch_one(
            "SELECT 1 AS present FROM storage_generation_attempts AS a \
             JOIN storage_conversations AS c ON c.id=a.conversation_id \
             WHERE a.task_id=? AND a.conversation_id=? AND c.user_id=?",
            &params![parent.as_str(), conv.as_str(), user_id],
        )?;
        if bound.is_none() {
            return fail("database_not_found", "Parent task not found");
        }
    }
    let root = worker_root_for_task(session, &parent, Some(user_id))?;
    let claim = get(payload, "worker_claim");
    if root.is_some() || !claim.is_null() {
        let task_id = root.clone().unwrap_or_else(|| parent.clone());
        let attempt_id = task_id.strip_prefix("conversation-attempt:").unwrap_or(&task_id).to_owned();
        require_conversation_publication_claim(session, &attempt_id, &task_id, user_id, claim)?;
    }
    session.lock_key("record", &format!("task_results:{child}"));
    let current_child = task_result_get(session, &child)?;
    if let Some(current_child) = current_child {
        if !py_eq(dict_get(&current_child["value"], "child_invocation")?, &identity) {
            return fail("database_conflict", "Child identity is immutable");
        }
    } else {
        let document = json!({
            "task_id": child,
            "user_id": user_id,
            "conv_id": conv,
            "status": "registered",
            "created_at": now_ms() / 1000,
            "child_invocation": identity,
        });
        session.execute(
            "INSERT INTO storage_records(namespace,record_key,value_json,version,updated_at_ms) \
             VALUES (?,?,?,?,?)",
            &params!["task_results", child.as_str(), encoded_task_result_document(&document)?, 1i64, now_ms()],
        )?;
    }
    Ok(identity)
}

pub fn resolve_child(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let scope = required_text(payload, "archive_scope", 512)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let prefix = format!("invocation:{user_id}:");
    let child = scope.strip_prefix(&prefix).unwrap_or("");
    let key = if child.is_empty() { scope.as_str() } else { child };
    let row = task_result_get(session, key)?;
    let value = row.map(|row| or_empty(&row["value"])).unwrap_or(Value::Object(Map::new()));
    let identity = or_empty(dict_get(&value, "child_invocation")?);
    if !py_eq(dict_get(&identity, "archive_scope")?, &Value::String(scope.clone()))
        || !py_eq(dict_get(&identity, "user_id")?, &Value::from(user_id))
    {
        return fail("database_not_found", "Child invocation not found");
    }
    Ok(identity)
}
