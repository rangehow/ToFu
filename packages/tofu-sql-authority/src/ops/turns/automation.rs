//! What `_turn_attempt_start` composes beyond the turn tables: private ports
//! of `lib/automations/events.py` (`validate_start_context`,
//! `subscription_prefixes`, `task_started_job`; shared owner: automations),
//! `operations_pkg/_worker_jobs.py::_worker_job_enqueue_owned` (shared owner:
//! worker_jobs) and `lib/storage/worker_execution.py::execution_witness`.
//! The subscription probe is the core domain's registered `record.list`
//! handler, which Python calls directly (`_record_list`).

use std::sync::OnceLock;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::replay::py_strip;
use crate::codec::dump;
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::params;
use crate::session::Session;

const STORAGE_SCHEMA: &str = include_str!("../../../../../contracts/automation_storage_v1.schema.json");
const DEFINITION_SCHEMA: &str = include_str!("../../../../../contracts/automation_v1.schema.json");
/// `generated_worker_job_contract.WORKER_EXECUTION_*`.
pub const WORKER_EXECUTION_CONFIG_KEY: &str = "_conversationWorkerExecution";
const WORKER_EXECUTION_WITNESS_FORMAT: &str = "tofu.conversation-worker-execution/v1";
const WORKER_EXECUTION_PROCESS_FIELDS: [&str; 5] = ["host", "boot", "pid_namespace", "pid", "started"];
/// `_worker_jobs.py` bounds.
const MAX_CLOCK_MS: i64 = 9_223_372_036_854_775_000;
const MAX_CLOCK_SKEW_MS: i64 = 24 * 60 * 60 * 1000;
const MAX_JOB_PAYLOAD_BYTES: usize = 1024 * 1024;

fn storage_schema() -> &'static Value {
    static SCHEMA: OnceLock<Value> = OnceLock::new();
    SCHEMA.get_or_init(|| serde_json::from_str(STORAGE_SCHEMA).expect("automation storage schema"))
}

fn safe_integer_max() -> i64 {
    static MAX: OnceLock<i64> = OnceLock::new();
    *MAX.get_or_init(|| {
        let schema: Value = serde_json::from_str(DEFINITION_SCHEMA).expect("automation schema");
        schema["x-limits"]["safeIntegerMax"].as_i64().expect("safeIntegerMax")
    })
}

fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

fn chars(text: &str) -> usize {
    text.chars().count()
}

/// `contract._matches_small_context_schema(value, schema)`.
fn matches_small_schema(value: &Value, schema: &Value) -> bool {
    if let Some(Value::Array(members)) = schema.get("enum") {
        match value {
            Value::String(text) if members.iter().any(|member| member.as_str() == Some(text)) => {}
            _ => return false,
        }
    }
    match schema.get("type").and_then(Value::as_str) {
        Some("object") => {
            let Value::Object(map) = value else { return false };
            let required = schema.get("required").and_then(Value::as_array).cloned().unwrap_or_default();
            if !required.iter().all(|key| key.as_str().is_some_and(|key| map.contains_key(key))) {
                return false;
            }
            let empty = Map::new();
            let properties = schema.get("properties").and_then(Value::as_object).unwrap_or(&empty);
            if schema.get("additionalProperties") == Some(&Value::Bool(false))
                && map.keys().any(|key| !properties.contains_key(key))
            {
                return false;
            }
            properties.iter().all(|(key, child)| map.get(key).is_none_or(|item| matches_small_schema(item, child)))
        }
        Some("array") => {
            let Value::Array(items) = value else { return false };
            if schema.get("maxItems").and_then(Value::as_u64).is_some_and(|maximum| items.len() as u64 > maximum) {
                return false;
            }
            schema.get("items").is_none_or(|child| items.iter().all(|item| matches_small_schema(item, child)))
        }
        Some("string") => {
            let Value::String(text) = value else { return false };
            let length = chars(text) as u64;
            schema.get("minLength").and_then(Value::as_u64).unwrap_or(0) <= length
                && schema.get("maxLength").and_then(Value::as_u64).is_none_or(|maximum| length <= maximum)
        }
        _ => true,
    }
}

/// `events.validate_start_context(value)` without raising.
pub fn start_context_is_valid(value: &Value) -> bool {
    matches_small_schema(value, &storage_schema()["$defs"]["taskStartedContext"])
}

fn text<'v>(value: &'v Value, key: &str) -> &'v str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

fn owner_prefix(owner: i64, tenant: &str) -> String {
    format!("{owner}:{}:", &sha256_hex(tenant)[..32])
}

/// `events.subscription_prefixes(owner, context)` for a validated context.
fn subscription_prefixes(owner: i64, context: &Value) -> Vec<String> {
    let prefix = format!("{}task.started:", owner_prefix(owner, text(context, "tenantId")));
    let mut projects = vec!["*".to_owned()];
    if context.get("projectId").is_some_and(truthy) {
        projects.push(sha256_hex(text(context, "projectId")));
    }
    let mut prefixes = Vec::new();
    for scope in ["all", text(context, "origin")] {
        for project in &projects {
            prefixes.push(format!("{prefix}{scope}:{project}:"));
        }
    }
    prefixes
}

pub struct TaskStart<'a> {
    pub attempt_id: &'a str,
    pub task_id: &'a str,
    pub conversation_id: &'a str,
    pub turn_id: &'a str,
    pub started_at_ms: i64,
}

/// `events.task_started_job(owner, context, ...)`; an `AutomationError`
/// escapes the storage operation uncaught.
fn task_started_job(owner: i64, context: &Value, start: &TaskStart<'_>) -> OpResult<(String, Value)> {
    let started_at_ms = start.started_at_ms;
    if !(0 < started_at_ms && started_at_ms <= safe_integer_max() / 1000 * 1000) {
        return internal("AutomationError: Invalid actual task-start timestamp");
    }
    let event_id = format!("task-start:{}", sha256_hex(&format!("{}:{}", start.attempt_id, start.task_id)));
    let tenant = text(context, "tenantId");
    let job_id = format!("automation-event:{}", sha256_hex(&format!("{}{event_id}", owner_prefix(owner, tenant))));
    let principal_tenant = py_strip(tenant);
    let principal = json!({
        "kind": "system",
        "subject_id": job_id,
        "owner_user_id": owner,
        "tenant_id": if principal_tenant.is_empty() { Value::Null } else { json!(principal_tenant) },
        "scopes": ["automations:execute", "automations:read"],
    });
    let started = crate::json::float_value(started_at_ms as f64 / 1000.0);
    let mut event = json!({
        "id": event_id, "kind": "task.started", "occurredAt": started,
        "origin": context["origin"], "automationChain": context["automationChain"],
        "evidence": {"startedAt": started, "taskId": start.task_id, "attemptId": start.attempt_id,
            "turnId": start.turn_id, "conversationId": start.conversation_id},
    });
    if context.get("projectId").is_some_and(truthy) {
        event["projectId"] = context["projectId"].clone();
    }
    // `storage_input_validator('taskStartedJob')`: every other field is
    // fixed or already validated; the stored turn/conversation identities
    // are the only unchecked bounds.
    let turn_chars = chars(start.turn_id);
    let conversation_chars = chars(start.conversation_id);
    if !(1..=128).contains(&turn_chars) || !(1..=256).contains(&conversation_chars) {
        return internal("AutomationError: Invalid task-start event evidence");
    }
    let contract = storage_schema()["$defs"]["taskStartedJob"]["properties"]["contract"]["const"].clone();
    let document = json!({"contract": contract, "schemaVersion": 1, "principal": principal, "event": event,
        "delivery": {"index": 0, "afterKey": "", "processed": 0}});
    Ok((job_id, document))
}

fn wall_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// `_worker_job_enqueue_owned` for the fixed task-start request.
fn worker_job_enqueue_owned(
    session: &Session<'_>,
    task_id: &str,
    user_id: i64,
    tenant_id: &str,
    payload: &Value,
    priority: i64,
    now_ms: i64,
) -> OpResult<()> {
    let task_kind = "automation-event";
    if chars(tenant_id) > 256 {
        return fail("database_protocol_error", "Invalid tenant_id in storage request");
    }
    if !(0..=1000).contains(&priority) {
        return fail("database_protocol_error", "Invalid priority in storage request");
    }
    if !(0..=MAX_CLOCK_MS).contains(&now_ms) {
        return fail("database_protocol_error", "Invalid now_ms in storage request");
    }
    if now_ms - wall_ms() > MAX_CLOCK_SKEW_MS {
        return fail("database_protocol_error", "Worker job clock is too far ahead of the server clock");
    }
    let encoded_payload = dump(payload)?;
    if encoded_payload.len() > MAX_JOB_PAYLOAD_BYTES {
        return fail("storage_payload_too_large", format!("Worker job payload exceeds {MAX_JOB_PAYLOAD_BYTES} bytes"));
    }
    let request_digest = hex::encode(Sha256::digest(dump(&json!({
        "tenantId": tenant_id,
        "taskKind": task_kind,
        "payload": payload,
    }))?));
    session.lock_key("worker_job_idempotency", &format!("{user_id}:{task_id}"));
    let existing = session.fetch_one(
        "SELECT request_digest FROM storage_worker_jobs WHERE user_id = ? AND idempotency_key = ?",
        &params![user_id, task_id],
    )?;
    if let Some(existing) = existing {
        if existing.text("request_digest") != request_digest {
            return fail("database_conflict", "Worker job idempotency key was reused for another request");
        }
        return Ok(());
    }
    if session.fetch_one("SELECT task_id FROM storage_worker_jobs WHERE task_id = ?", &params![task_id])?.is_some() {
        return fail("database_conflict", "Worker job task_id already exists");
    }
    session.execute(
        "INSERT INTO storage_worker_jobs(task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, request_digest, status, priority, available_at_ms, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
        &params![
            task_id,
            user_id,
            tenant_id,
            task_kind,
            encoded_payload,
            task_id,
            request_digest,
            priority,
            now_ms,
            now_ms,
            now_ms
        ],
    )?;
    session.hint_queue_changed();
    Ok(())
}

/// The automation tail of `_turn_attempt_start`: probe the subscription
/// indexes and enqueue the task-start event only when one matches.
pub fn enqueue_task_started(
    session: &Session<'_>,
    user_id: i64,
    context: &Value,
    start: &TaskStart<'_>,
    now: i64,
) -> OpResult<()> {
    let Some(spec) = crate::ops::registry().get("record.list") else {
        return internal("record.list is not registered");
    };
    let namespace = storage_schema()["x-namespaces"]["subscription"].clone();
    let mut subscribed = false;
    for prefix in subscription_prefixes(user_id, context) {
        let mut probe = Map::new();
        probe.insert("namespace".into(), namespace.clone());
        probe.insert("prefix".into(), json!(prefix));
        probe.insert("limit".into(), json!(1));
        if truthy(&(spec.handler)(session, &probe)?) {
            subscribed = true;
            break;
        }
    }
    if !subscribed {
        return Ok(());
    }
    let (job_id, document) = task_started_job(user_id, context, start)?;
    let priority = storage_schema()["x-event"]["priority"].as_i64().expect("x-event priority");
    worker_job_enqueue_owned(session, &job_id, user_id, text(context, "tenantId"), &document, priority, now)
}

/// `execution_witness(process, claim)`.
pub fn execution_witness(process: &Value, claim: &Value) -> OpResult<Value> {
    let claim_ok = match claim {
        Value::Object(map) => {
            map.len() == 2
                && matches!(map.get("worker_id"), Some(Value::String(id)) if (1..=256).contains(&chars(id)))
                && matches!(map.get("fencing_token"), Some(Value::Number(n)) if n.as_i64().is_some_and(|token| token > 0))
        }
        _ => false,
    };
    if !claim_ok {
        return fail("database_protocol_error", "Worker entry requires its exact claim");
    }
    if let Value::Object(map) = process {
        let fields_ok = map.len() == WORKER_EXECUTION_PROCESS_FIELDS.len()
            && WORKER_EXECUTION_PROCESS_FIELDS.iter().all(|field| map.contains_key(*field));
        let valid = fields_ok
            && matches!(&map["pid"], Value::Number(n) if (n.is_i64() || n.is_u64()) && n.as_f64().is_some_and(|pid| pid >= 1.0))
            && WORKER_EXECUTION_PROCESS_FIELDS
                .iter()
                .filter(|field| **field != "pid")
                .all(|field| matches!(&map[*field], Value::String(text) if !text.is_empty()))
            && ["started", "pid_namespace"]
                .iter()
                .all(|field| map[*field].as_str().is_some_and(|text| text.bytes().all(|byte| byte.is_ascii_digit())));
        if !valid {
            return fail("database_protocol_error", "Invalid worker process identity");
        }
    } else if !process.is_null() {
        return fail("database_protocol_error", "Invalid worker process identity");
    }
    Ok(json!({"format": WORKER_EXECUTION_WITNESS_FORMAT, "claim": claim, "process": process}))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `contract._supports_small_context_schema`: the closed vocabulary the
    /// matcher implements (otherwise Python falls back to full jsonschema).
    fn supported(schema: &Value) -> bool {
        let Value::Object(map) = schema else { return false };
        let allowed: &[&str] = match map.get("type") {
            None => &["enum"],
            Some(Value::String(kind)) if kind == "object" => {
                &["properties", "required", "additionalProperties", "enum"]
            }
            Some(Value::String(kind)) if kind == "array" => &["items", "maxItems", "enum"],
            Some(Value::String(kind)) if kind == "string" => &["minLength", "maxLength", "enum"],
            _ => return false,
        };
        map.keys().all(|key| key == "type" || allowed.contains(&key.as_str()))
            && map.get("enum").is_none_or(|values| values.as_array().is_some_and(|v| v.iter().all(Value::is_string)))
            && map.get("additionalProperties").is_none_or(Value::is_boolean)
            && map.get("properties").and_then(Value::as_object).is_none_or(|p| p.values().all(supported))
            && map.get("items").is_none_or(supported)
    }

    #[test]
    fn task_start_context_schema_stays_in_the_small_vocabulary() {
        assert!(supported(&storage_schema()["$defs"]["taskStartedContext"]));
        assert!(start_context_is_valid(&json!({"tenantId": "", "origin": "human", "automationChain": []})));
        assert!(!start_context_is_valid(&json!({"tenantId": "", "origin": "robot", "automationChain": []})));
        assert!(!start_context_is_valid(&json!({"tenantId": "", "origin": "human", "automationChain": [""]})));
        assert!(!start_context_is_valid(&json!({"tenantId": "", "origin": "human"})));
    }

    #[test]
    fn task_started_job_matches_the_python_document() {
        let context = json!({"tenantId": " t ", "origin": "human", "automationChain": ["a"]});
        let start = TaskStart {
            attempt_id: "a",
            task_id: "t",
            conversation_id: "c",
            turn_id: "u",
            started_at_ms: 1_700_000_000_123,
        };
        let (job_id, document) = task_started_job(3, &context, &start).unwrap();
        assert_eq!(job_id, "automation-event:99f3b70f0bc64b55813dbadd450c461b113cd2cf1424a1af0679639803210018");
        assert_eq!(document["principal"]["tenant_id"], "t");
        assert_eq!(
            document["event"]["id"],
            "task-start:4870d47a74c0c77b34cc75e131729d0b80015924553179c559e5615c395eba75"
        );
        let prefixes = subscription_prefixes(
            3,
            &json!({"tenantId": "", "origin": "human", "automationChain": [], "projectId": "p"}),
        );
        assert_eq!(prefixes[0], "3:e3b0c44298fc1c149afbf4c8996fb924:task.started:all:*:");
        assert_eq!(prefixes.len(), 4);
    }
}
