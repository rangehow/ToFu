//! `operations_pkg/_automations.py` (`automation.run.admit`,
//! `automation.run.advance`, `automation.run.cancel`), with private ports of
//! `lib/automations/approval.py::validate_approval_progress`,
//! `transitions.py::request_cancel`, `presentation_index.py` and
//! `_automation_presentations.py::publish_presentations`.

use std::sync::OnceLock;

use serde_json::{json, Map, Value};

use super::py::{
    attempt_get, check_json_bounds, clock_ahead, concat, get, get_or_none, in_set, in_tuple, item, item_by, iterate,
    or_empty, owner_prefix, record_delete_owned, sha256_hex, starts_with, value_error, worker_job_claim_state,
    worker_job_enqueue_owned, worker_job_get, worker_job_request_cancel, Principal, Py, PyExc,
};
use super::pyjson;
use super::schema::{storage_input_valid, storage_schema, Validator};
use super::{namespace, record_get, record_put_owned};
use crate::codec::py_eq;
use crate::error::{fail, internal, OpResult, StorageError};
use crate::json::truthy;
use crate::payload::{integer, required_text, Payload};
use crate::session::Session;

const SAFE_INTEGER_MAX: i64 = 9_007_199_254_740_991;
const IMMUTABLE: [&str; 17] = [
    "contract",
    "id",
    "ownerId",
    "tenantId",
    "definitionId",
    "definitionRevision",
    "definitionDigest",
    "snapshot",
    "actions",
    "stepOrder",
    "trigger",
    "triggerDigest",
    "automationChain",
    "createdAt",
    "deadlineAt",
    "jobId",
    "principal",
];

pub trait Raise<T> {
    /// An exception the Python handler does not catch.
    fn raise(self) -> OpResult<T>;
}

impl<T> Raise<T> for Py<T> {
    fn raise(self) -> OpResult<T> {
        self.map_err(PyExc::raise)
    }
}

fn definition_schema() -> &'static Value {
    static SCHEMA: OnceLock<Value> = OnceLock::new();
    SCHEMA.get_or_init(|| {
        serde_json::from_str(include_str!("../../../../../contracts/automation_v1.schema.json"))
            .expect("automation schema")
    })
}

/// `lib/automations/contract.py::RUNTIME[name]`.
pub fn runtime(name: &str) -> Vec<&'static str> {
    definition_schema()["x-runtime"][name]
        .as_array()
        .expect("runtime states")
        .iter()
        .filter_map(Value::as_str)
        .collect()
}

/// `lib/automations/contract.py::LIMITS[name]`.
pub fn limit(name: &str) -> i64 {
    definition_schema()["x-limits"][name].as_i64().expect("automation limit")
}

pub fn protocol<T>(message: &str) -> OpResult<T> {
    fail("database_protocol_error", message)
}

/// `json.dumps(value, sort_keys=True, separators=(',', ':'))` compared as text.
pub fn canonical(value: &Value) -> String {
    pyjson::dumps(value, true)
}

pub fn same(left: &Value, right: &Value) -> bool {
    canonical(left) == canonical(right)
}

/// `{**saved, 'value': value}`.
pub fn with_value(mut saved: Value, value: Value) -> Value {
    saved["value"] = value;
    saved
}

pub fn record(session: &Session<'_>, kind: &str, key: &str) -> OpResult<Option<Value>> {
    Ok(record_get(session, namespace(kind), key)?.map(Value::Object))
}

pub fn put(session: &Session<'_>, kind: &str, key: &str, value: &Value, version: i64) -> OpResult<Value> {
    record_put_owned(session, namespace(kind), key, value, version)
}

fn version_of(record: &Value) -> i64 {
    record["version"].as_i64().unwrap_or(0)
}

/// `int(value * 1000)` of a JSON number.
fn milliseconds(value: &Value) -> Py<i64> {
    match value {
        Value::Number(number) => match number.as_i64() {
            Some(integer) => Ok(integer * 1000),
            None => Ok((number.as_f64().unwrap_or(0.0) * 1000.0).trunc() as i64),
        },
        _ => Err(PyExc::Type),
    }
}

/// `_index`: small active/history pointers for recovery and listing.
pub fn index(session: &Session<'_>, prefix: &str, run: &Value) -> OpResult<()> {
    let pointer = (|| -> Py<Value> {
        Ok(json!({
            "id": item(run, "id")?, "ownerId": item(run, "ownerId")?, "tenantId": item(run, "tenantId")?,
            "definitionId": item(run, "definitionId")?, "jobId": item(run, "jobId")?,
            "status": item(run, "status")?, "createdAt": item(run, "createdAt")?,
        }))
    })()
    .raise()?;
    let active_key = concat(prefix, &run["id"]).raise()?;
    let active = record(session, "active", &active_key)?;
    if in_tuple(&run["status"], &runtime("terminalRunStates")) {
        if let Some(active) = active {
            record_delete_owned(session, namespace("active"), &active_key, version_of(&active))?;
        }
    } else {
        put(session, "active", &active_key, &pointer, active.as_ref().map(version_of).unwrap_or(0))?;
    }
    let history_key = format!(
        "{}:{:016}:{}",
        concat(prefix, &run["definitionId"]).raise()?,
        milliseconds(&run["createdAt"]).raise()?,
        run["id"].as_str().unwrap_or("")
    );
    let history = record(session, "history", &history_key)?;
    put(session, "history", &history_key, &pointer, history.as_ref().map(version_of).unwrap_or(0))?;
    Ok(())
}

struct Envelope {
    owner: i64,
    tenant: String,
    prefix: String,
    run: Value,
    key: String,
    job_id: String,
    now: i64,
}

/// `_envelope(payload, advance=...)`.
fn envelope(payload: &Payload, advance: bool) -> OpResult<Envelope> {
    let document = Value::Object(payload.clone());
    if check_json_bounds(&document, 44, 65536).is_err() {
        return protocol("Automation mutation exceeds JSON bounds");
    }
    if !storage_input_valid(if advance { "advance" } else { "admit" }, &document) {
        return protocol("Automation mutation does not match its storage contract");
    }
    let mut fields = vec!["user_id", "tenant_id", "run_key", "run", "now_ms"];
    if advance {
        fields.extend(["expected_version", "worker_id", "fencing_token"]);
    } else {
        fields.extend(["definition_key", "definition_revision", "definition_digest"]);
        if payload.contains_key("producer_fence") {
            fields.push("producer_fence");
        }
    }
    if payload.len() != fields.len() || !payload.keys().all(|key| fields.contains(&key.as_str())) {
        return protocol("Invalid automation mutation fields");
    }
    let owner = integer(payload, "user_id", None, Some(1), None)?;
    let tenant = match &payload["tenant_id"] {
        Value::String(tenant) if tenant.chars().count() <= 256 => tenant.clone(),
        _ => return protocol("Invalid automation tenant"),
    };
    let prefix = owner_prefix(&Value::from(owner), &tenant);
    let run = payload["run"].clone();
    let identity_ok = match &run {
        Value::Object(map) => {
            pyjson::dumps_ascii_sorted(&run).len() <= 524288
                && matches!(map.get("ownerId"), Some(Value::Number(n)) if n.is_i64() || n.is_u64())
                && py_eq(&map["ownerId"], &Value::from(owner))
                && py_eq(&or_empty(map.get("tenantId").unwrap_or(&Value::Null)), &Value::String(tenant.clone()))
                && matches!(map.get("definitionRevision"), Some(Value::Number(n)) if n.is_i64() || n.is_u64())
                && py_eq(map.get("contract").unwrap_or(&Value::Null), &json!("tofu.automation-run/v1"))
                && map.get("id").is_some_and(Value::is_string)
        }
        _ => false,
    };
    if !identity_ok {
        return protocol("Invalid automation run identity");
    }
    if !advance {
        let steps = item(&run, "steps").raise()?;
        let Value::Object(steps) = steps else { return internal("AttributeError: values") };
        for state in steps.values() {
            match state {
                Value::Object(state) if state.contains_key("approval") => {
                    return protocol("Initial automation state cannot carry approval")
                }
                Value::Object(_) => {}
                Value::String(text) if text.contains("approval") => {
                    return protocol("Initial automation state cannot carry approval")
                }
                Value::Array(items) if items.iter().any(|item| item == &json!("approval")) => {
                    return protocol("Initial automation state cannot carry approval")
                }
                Value::String(_) | Value::Array(_) => {}
                _ => return internal("TypeError: argument of type is not iterable"),
            }
        }
    }
    let key = required_text(payload, "run_key", 512)?;
    if key != format!("{prefix}{}", run["id"].as_str().unwrap_or("")) {
        return protocol("Automation run key escaped its owner");
    }
    let job_id = format!("automation-run:{}", sha256_hex(&key));
    if !py_eq(run.get("jobId").unwrap_or(&Value::Null), &Value::String(job_id.clone())) {
        return protocol("Automation job identity differs");
    }
    let now = integer(payload, "now_ms", None, Some(1), Some(SAFE_INTEGER_MAX))?;
    if clock_ahead(&Value::from(now), 86_400_000) {
        return protocol("Automation clock is too far ahead");
    }
    Ok(Envelope { owner, tenant, prefix, run, key, job_id, now })
}

/// The admission grant must be an exact least-privilege principal.
fn grant_is_least_privilege(raw: &Value, owner: i64, tenant: &str, run: &Value) -> bool {
    let check = || -> Py<bool> {
        let principal = Principal::from_payload(raw)?;
        if !py_eq(&principal.to_payload(), raw)
            || principal.require_owner("operation")? != owner
            || principal.tenant_id.as_deref().unwrap_or("") != tenant
            || principal.scopes.contains("admin")
        {
            return Ok(false);
        }
        for scope in ["automations:read", "automations:execute"] {
            principal.require_scope(&json!(scope))?;
        }
        let Value::Object(actions) = item(run, "actions")? else { return Err(PyExc::Type) };
        for action in actions.values() {
            for scope in iterate(item(action, "requiredScopes")?)? {
                principal.require_scope(&scope)?;
            }
        }
        Ok(true)
    };
    matches!(check(), Ok(true))
}

// private port of lib/storage_authority_sql/operations_pkg/_automation_projects.py::require_project; shared owner: automations
pub fn require_project(session: &Session<'_>, owner: i64, tenant: &str, identity: &Value) -> OpResult<()> {
    if identity.is_null() {
        return Ok(());
    }
    let key = concat(&owner_prefix(&Value::from(owner), tenant), identity).raise()?;
    session.lock_key("automation_project", &key);
    let value = record(session, "target", &key)?.map(|row| row["value"].clone()).unwrap_or_else(|| json!({}));
    let available = (|| -> Py<bool> {
        let tenant_or_none = if tenant.is_empty() { Value::Null } else { Value::String(tenant.to_owned()) };
        Ok(py_eq(get_or_none(&value, "contract")?, &storage_schema()["x-project"]["contract"])
            && py_eq(get_or_none(&value, "id")?, identity)
            && matches!(get_or_none(&value, "ownerId")?, Value::Number(n) if n.is_i64() || n.is_u64())
            && py_eq(&value["ownerId"], &Value::from(owner))
            && py_eq(get_or_none(&value, "tenantId")?, &tenant_or_none)
            && value["root"]
                .as_str()
                .is_some_and(|root| py_eq(&Value::String(format!("project_{}", sha256_hex(root))), identity))
            && value["active"] == Value::Bool(true))
    })()
    .raise()?;
    if !available {
        return fail("database_conflict", "Owned automation project is unavailable");
    }
    Ok(())
}

/// `_automation_run_admit`.
pub fn run_admit(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let Envelope { owner, tenant, prefix, run, key, job_id, now } = envelope(payload, false)?;
    session.lock_key("automation_run", &key);
    if let Some(existing) = record(session, "run", &key)? {
        let stored = get_or_none(&existing["value"], "triggerDigest").raise()?;
        if !py_eq(stored, get_or_none(&run, "triggerDigest").raise()?) {
            return fail("database_conflict", "Automation occurrence evidence changed");
        }
        let job = worker_job_get(session, &json!(job_id), &json!(owner))?;
        if job.is_null() {
            return fail("database_integrity", "Accepted automation has no durable job");
        }
        return Ok(json!({"created": false, "run": existing, "job": job}));
    }
    if !py_eq(&run["status"], &json!("queued")) || !run["fence"].is_null() {
        return protocol("A new automation must be queued without a claim");
    }
    let raw_principal = run["principal"].clone();
    if !grant_is_least_privilege(&raw_principal, owner, &tenant, &run) {
        return protocol("Automation requires an explicit least-privilege grant");
    }
    let definition_key = required_text(payload, "definition_key", 512)?;
    let definition_id = run.get("definitionId").cloned().unwrap_or_else(|| json!(""));
    if definition_key != concat(&prefix, &definition_id).raise()? {
        return protocol("Automation definition key escaped its owner");
    }
    let revision = integer(payload, "definition_revision", None, Some(1), None)?;
    session.lock_key("automation_definition", &definition_key);
    let definition = record(session, "definition", &definition_key)?;
    let digest = required_text(payload, "definition_digest", 64)?;
    let empty = json!({});
    let changed = (|| -> Py<bool> {
        let Some(definition) = &definition else { return Ok(true) };
        let value = &definition["value"];
        if !py_eq(&definition["version"], &json!(revision)) {
            return Ok(true);
        }
        let compiled = get(value, "compiled")?.unwrap_or(&empty);
        Ok(!py_eq(get_or_none(compiled, "digest")?, &json!(digest))
            || !py_eq(get_or_none(&run, "definitionRevision")?, &json!(revision))
            || !py_eq(get_or_none(&run, "definitionDigest")?, &json!(digest))
            || truthy(get_or_none(value, "deleted")?)
            || (!truthy(item(item(value, "definition")?, "enabled")?)
                && !py_eq(item(item(&run, "trigger")?, "kind")?, &json!("manual"))))
    })()
    .raise()?;
    if changed {
        return fail("database_conflict", "Automation definition changed before admission");
    }
    let definition = definition.expect("unchanged definition exists");
    let value = &definition["value"];
    let project = get_or_none(item(value, "definition").raise()?, "projectId").raise()?.clone();
    require_project(session, owner, &tenant, &project)?;
    let trigger = item(&run, "trigger").raise()?;
    if !project.is_null() && !py_eq(get_or_none(trigger, "projectId").raise()?, &project) {
        return protocol("Automation trigger escaped its project binding");
    }
    let differs = (|| -> Py<bool> {
        let compiled = item(value, "compiled")?;
        let grant = get(value, "grant")?.unwrap_or(&empty);
        Ok(!same(item(&run, "snapshot")?, item(value, "definition")?)
            || !same(item(&run, "actions")?, item(compiled, "actions")?)
            || !same(item(&run, "stepOrder")?, item(compiled, "stepOrder")?)
            || !same(&raw_principal, get_or_none(grant, "principal")?))
    })()
    .raise()?;
    if differs {
        return protocol("Automation snapshot differs from admitted definition");
    }
    let producer = payload.get("producer_fence").cloned().unwrap_or(Value::Null);
    let kind = item(trigger, "kind").raise()?;
    if py_eq(kind, &json!("schedule")) {
        let state = get(value, "triggerState").raise()?.unwrap_or(&empty);
        let clock_id = get_or_none(state, "clockJobId").raise()?.clone();
        if !truthy(&producer) || !py_eq(item(&producer, "jobId").raise()?, &clock_id) {
            return fail("database_conflict", "A scheduled occurrence requires its published clock producer");
        }
        let clock = worker_job_get(session, &clock_id, &json!(owner))?;
        let proof = worker_job_claim_state(session, &clock_id, &producer["workerId"], &producer["token"], &json!(now))?;
        let stale = (|| -> Py<bool> {
            if clock.is_null() {
                return Ok(true);
            }
            let source = &clock["payload"];
            Ok(!py_eq(&clock["taskKind"], &json!("automation-clock"))
                || !py_eq(&clock["tenantId"], &json!(tenant))
                || !py_eq(get_or_none(source, "definitionKey")?, &payload["definition_key"])
                || !py_eq(get_or_none(source, "definitionRevision")?, &json!(revision))
                || !truthy(&proof["ok"])
                || truthy(&proof["cancelSequence"]))
        })()
        .raise()?;
        if stale {
            return fail("database_conflict", "Automation clock producer claim is stale");
        }
    } else if py_eq(kind, &json!("task.started"))
        && (!producer.is_null() || starts_with(item(trigger, "id").raise()?, "task-start:").raise()?)
    {
        if producer.is_null() {
            return fail("database_conflict", "A canonical task-start occurrence requires its event producer");
        }
        let event_job = worker_job_get(session, &producer["jobId"], &json!(owner))?;
        let proof = worker_job_claim_state(
            session,
            &producer["jobId"],
            &producer["workerId"],
            &producer["token"],
            &json!(now),
        )?;
        let stale = (|| -> Py<bool> {
            if event_job.is_null() {
                return Ok(true);
            }
            Ok(!py_eq(&event_job["taskKind"], &json!("automation-event"))
                || !py_eq(&event_job["tenantId"], &json!(tenant))
                || !same(get_or_none(&event_job["payload"], "event")?, trigger)
                || !truthy(&proof["ok"])
                || truthy(&proof["cancelSequence"]))
        })()
        .raise()?;
        if stale {
            return fail("database_conflict", "Automation event producer claim is stale");
        }
    } else if !producer.is_null() {
        return protocol("Only a scheduled or canonical task-start occurrence may carry a producer fence");
    }
    let saved = put(session, "run", &key, &run, 0)?;
    let enqueue = json!({
        "task_id": job_id, "user_id": owner, "tenant_id": tenant,
        "task_kind": "automation-run", "idempotency_key": job_id,
        "payload": {"contract": "tofu.automation-job/v1", "runId": run["id"], "principal": raw_principal},
        "priority": 200, "available_at_ms": now, "now_ms": now,
    });
    let job = worker_job_enqueue_owned(session, enqueue.as_object().expect("enqueue payload"))?["job"].clone();
    index(session, &prefix, &run)?;
    Ok(json!({"created": true, "run": with_value(saved, run), "job": job}))
}

/// `dict.get(key, default)` where `key` is a decoded value.
fn dict_get<'v>(map: &'v Value, key: &Value) -> Py<Option<&'v Value>> {
    match (map, key) {
        (Value::Object(map), Value::String(text)) => Ok(map.get(text)),
        (Value::Object(_), Value::Array(_) | Value::Object(_)) => Err(PyExc::Type),
        (Value::Object(_), _) => Ok(None),
        _ => Err(PyExc::Type),
    }
}

/// `lib/automations/approval.py::digest`.
pub fn approval_digest(value: &Value) -> String {
    sha256_hex(&canonical(value))
}

/// `approval.validate_approved_dispatch`.
fn validate_approved_dispatch(state: &Value, action: &Value, arguments: &Value) -> Py<()> {
    let grant = get_or_none(state, "approval")?;
    let Value::Object(grant) = grant else { return value_error("Approved action or arguments changed") };
    let field = |key: &str| grant.get(key).unwrap_or(&Value::Null);
    if !py_eq(field("argumentsDigest"), &json!(approval_digest(arguments)))
        || !py_eq(field("actionDigest"), &json!(approval_digest(action)))
        || !py_eq(field("effectKey"), get_or_none(state, "effectKey")?)
    {
        return value_error("Approved action or arguments changed");
    }
    Ok(())
}

/// `approval.validate_approval_progress`.
fn validate_approval_progress(current: &Value, updated: &Value) -> Py<()> {
    let current_steps = item(current, "steps")?;
    let updated_steps = item(updated, "steps")?;
    let (Value::Object(current_map), Value::Object(updated_map)) = (current_steps, updated_steps) else {
        return Err(PyExc::Type);
    };
    if current_map.len() != updated_map.len() || !current_map.keys().all(|key| updated_map.contains_key(key)) {
        return value_error("Worker cannot add or remove frozen steps");
    }
    let snapshot_steps = iterate(item(item(current, "snapshot")?, "steps")?)?;
    let current_actions = item(current, "actions")?;
    let mut actions: Map<String, Value> = Map::new();
    for step in &snapshot_steps {
        let id = item(step, "id")?;
        let action = item_by(current_actions, item(step, "action")?)?;
        match id {
            Value::String(id) => {
                actions.insert(id.clone(), action.clone());
            }
            Value::Array(_) | Value::Object(_) => return Err(PyExc::Type),
            _ => {}
        }
    }
    let empty = json!({});
    let mut active = runtime("activeEffectStepStates");
    active.push("succeeded");
    for (step_id, previous) in current_map {
        let state = updated_map.get(step_id).unwrap_or(&empty);
        let Some(action) = actions.get(step_id) else {
            return Err(PyExc::Key(super::schema::repr(&json!(step_id))));
        };
        if in_set(item(action, "effect")?, &["write", "agent"])?
            && in_set(item(previous, "status")?, &active)?
            && in_set(get_or_none(state, "status")?, &["pending", "waiting_permission", "skipped"])?
        {
            return value_error("External intent cannot become dispatchable again");
        }
        if !py_eq(get_or_none(previous, "effectKey")?, get_or_none(state, "effectKey")?) {
            return value_error("Automation effect identity changed");
        }
        if !py_eq(get_or_none(previous, "approval")?, get_or_none(state, "approval")?) {
            return value_error("Only owner approval can authorize a step");
        }
        if truthy(get_or_none(previous, "approval")?) {
            validate_approved_dispatch(state, action, get_or_none(state, "arguments")?)?;
        }
    }
    if !py_eq(item(item(item(current, "snapshot")?, "policy")?, "permissionMode")?, &json!("manual")) {
        return Ok(());
    }
    for step in &snapshot_steps {
        let state = dict_get(updated_steps, item(step, "id")?)?.unwrap_or(&empty);
        let action = item_by(current_actions, item(step, "action")?)?;
        if in_set(item(action, "effect")?, &["write", "agent"])?
            && in_set(
                get_or_none(state, "status")?,
                &["dispatching", "waiting_agent", "reconciliation_required", "succeeded"],
            )?
            && !truthy(get_or_none(state, "approval")?)
        {
            return value_error("Manual external effects require owned approval");
        }
    }
    Ok(())
}

fn show_time_output_schema() -> &'static Value {
    static SCHEMA: OnceLock<Value> = OnceLock::new();
    SCHEMA.get_or_init(|| {
        let catalog: Value =
            serde_json::from_str(include_str!("../../../../../contracts/automation_actions_v1.json")).expect("catalog");
        catalog["actions"]
            .as_array()
            .expect("actions")
            .iter()
            .find(|action| action["id"] == "task.show_time")
            .expect("task.show_time")["output"]
            .clone()
    })
}

/// `presentation_index.validate_target`.
fn validate_target(event: &Value, attempt: &Value) -> Py<()> {
    let evidence = item(event, "evidence")?;
    let mismatch = || value_error("Presentation has no matching owned physical start");
    if !truthy(attempt) {
        return mismatch();
    }
    for key in ["attemptId", "taskId", "turnId", "conversationId"] {
        if !py_eq(get_or_none(attempt, key)?, get_or_none(evidence, key)?) {
            return mismatch();
        }
    }
    let started = get_or_none(attempt, "startedAt")?;
    if !matches!(started, Value::Number(n) if n.is_i64() || n.is_u64())
        || !py_eq(&super::py::seconds(started), get_or_none(evidence, "startedAt")?)
        || !py_eq(item(event, "occurredAt")?, get_or_none(evidence, "startedAt")?)
    {
        return mismatch();
    }
    Ok(())
}

/// `presentation_index.presentation_prefix`.
fn presentation_prefix(owner_prefix: &str, conversation_id: &str, attempt_id: &str) -> String {
    let prefix = format!("{owner_prefix}{}:", sha256_hex(conversation_id));
    if attempt_id.is_empty() {
        prefix
    } else {
        format!("{prefix}{}:", sha256_hex(attempt_id))
    }
}

/// `completed_presentations` interleaved with `publish_presentations`, in
/// the generator's order.
fn presentations(session: &Session<'_>, prefix: &str, previous: &Value, run: &Value) -> Py<()> {
    let event = item(run, "trigger")?;
    if !starts_with(item(event, "id")?, "task-start:")? {
        return Ok(());
    }
    if !py_eq(item(event, "kind")?, &json!("task.started")) {
        return value_error("Presentation producer kind differs");
    }
    let empty = json!({});
    for step in iterate(item(item(run, "snapshot")?, "steps")?)? {
        if !py_eq(item(&step, "action")?, &json!("task.show_time")) {
            continue;
        }
        let step_id = item(&step, "id")?;
        let current = item_by(item(run, "steps")?, step_id)?;
        let old = item_by(item(previous, "steps")?, step_id)?;
        if py_eq(item(old, "status")?, &json!("succeeded")) {
            if !py_eq(current, old) {
                return value_error("Published time output is immutable");
            }
            continue;
        }
        if !py_eq(item(current, "status")?, &json!("succeeded")) {
            continue;
        }
        let evidence = get(event, "evidence")?.unwrap_or(&empty);
        for (key, maximum) in [("attemptId", 128), ("taskId", 200), ("turnId", 256), ("conversationId", 256)] {
            let valid = match get_or_none(evidence, key)? {
                Value::String(text) => (1..=maximum).contains(&text.chars().count()),
                _ => false,
            };
            if !valid {
                return value_error("Presentation source identity is missing");
            }
        }
        let text = |key: &str| evidence[key].as_str().unwrap_or("").to_owned();
        let source_id = format!("task-start:{}", sha256_hex(&format!("{}:{}", text("attemptId"), text("taskId"))));
        if !py_eq(item(event, "id")?, &json!(source_id)) {
            return value_error("Presentation source identity differs");
        }
        if !py_eq(item(old, "status")?, &json!("dispatching"))
            || !truthy(get_or_none(current, "effectKey")?)
            || !py_eq(get_or_none(old, "effectKey")?, item(current, "effectKey")?)
        {
            return value_error("Presentation requires its committed effect intent");
        }
        let output = get_or_none(current, "output")?;
        check_json_bounds(output, 24, 16384)?;
        let schema = show_time_output_schema();
        if let Some(message) = Validator::new(schema).first_error(schema, output) {
            return Err(PyExc::Value(message));
        }
        let timezone = get(item(current, "arguments")?, "timezone")?.cloned().unwrap_or_else(|| json!("UTC"));
        if !py_eq(item(output, "startedAt")?, get_or_none(evidence, "startedAt")?)
            || !py_eq(item(output, "timezone")?, &timezone)
        {
            return value_error("Presentation output differs from frozen start evidence");
        }
        let Value::String(effect_key) = item(current, "effectKey")? else { return Err(PyExc::Type) };
        let pointer = json!({
            "id": sha256_hex(effect_key), "runId": item(run, "id")?, "stepId": step_id,
            "ownerId": item(run, "ownerId")?, "tenantId": item(run, "tenantId")?,
            "conversationId": evidence["conversationId"], "attemptId": evidence["attemptId"],
            "turnId": evidence["turnId"],
        });
        let attempt = attempt_get(session, &run["ownerId"], &pointer["attemptId"])?;
        validate_target(event, &attempt)?;
        let key = presentation_prefix(prefix, &text("conversationId"), &text("attemptId"))
            + pointer["id"].as_str().unwrap_or("");
        if let Some(existing) = record(session, "presentation", &key)? {
            if !py_eq(&existing["value"], &pointer) {
                return value_error("Presentation identity collision");
            }
            continue;
        }
        put(session, "presentation", &key, &pointer, 0)?;
    }
    Ok(())
}

/// `_automation_presentations.publish_presentations`.
fn publish_presentations(session: &Session<'_>, prefix: &str, previous: &Value, run: &Value) -> OpResult<()> {
    match presentations(session, prefix, previous, run) {
        Ok(()) => Ok(()),
        Err(PyExc::Storage(failure)) => Err(failure),
        Err(exception) => match exception.message(false) {
            Some(message) => Err(StorageError::protocol(message).into()),
            None => Err(exception.raise()),
        },
    }
}

/// `_automation_run_advance`.
pub fn run_advance(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let Envelope { owner, tenant, prefix, run, key, job_id, now } = envelope(payload, true)?;
    session.lock_key("automation_run", &key);
    let existing = record(session, "run", &key)?;
    let version = integer(payload, "expected_version", None, Some(1), None)?;
    if let Some(existing) = &existing {
        if version_of(existing) == version + 1 && same(&existing["value"], &run) {
            return Ok(existing.clone());
        }
    }
    let job = worker_job_get(session, &json!(job_id), &json!(owner))?;
    let fence = integer(payload, "fencing_token", None, Some(1), None)?;
    let worker = required_text(payload, "worker_id", 200)?;
    let proof = worker_job_claim_state(session, &json!(job_id), &json!(worker), &json!(fence), &json!(now))?;
    if job.is_null()
        || !py_eq(&job["tenantId"], &json!(tenant))
        || !truthy(&proof["ok"])
        || !py_eq(&run["fence"], &json!({"jobId": job_id, "workerId": worker, "token": fence}))
    {
        return fail("database_conflict", "Automation worker claim is stale");
    }
    let terminal = runtime("terminalRunStates");
    let existing = record(session, "run", &key)?;
    let Some(existing) = existing else { return fail("database_conflict", "Automation run is missing or terminal") };
    if in_tuple(item(&existing["value"], "status").raise()?, &terminal) {
        return fail("database_conflict", "Automation run is missing or terminal");
    }
    if version_of(&existing) != version {
        return fail("database_conflict", "Automation run revision changed");
    }
    let current = &existing["value"];
    for field in IMMUTABLE {
        if !same(get_or_none(current, field).raise()?, get_or_none(&run, field).raise()?) {
            return protocol("Automation immutable evidence changed");
        }
    }
    if let Err(exception) = validate_approval_progress(current, &run) {
        return match exception.message(false) {
            Some(message) => protocol(&message),
            None => Err(exception.raise()),
        };
    }
    let current_command = &current["cancelCommand"];
    if !py_eq(current_command, &run["cancelCommand"])
        || (truthy(current_command) && !py_eq(&current["cancelReason"], &run["cancelReason"]))
    {
        return protocol("Automation cancellation evidence changed");
    }
    if truthy(current_command) {
        if !in_set(&run["status"], &["cancelling", "cancelled"]).raise()? {
            return fail("database_conflict", "Owner cancellation requires cancellation settlement");
        }
        if py_eq(&run["status"], &json!("cancelled")) {
            let Value::Object(steps) = &run["steps"] else { return internal("AttributeError: values") };
            let active = runtime("activeEffectStepStates");
            for state in steps.values() {
                if in_tuple(item(state, "status").raise()?, &active) {
                    return protocol("Cancellation cannot settle an unresolved external effect");
                }
            }
        }
    }
    if truthy(&proof["cancelSequence"]) && !in_set(&run["status"], &["cancelling", "cancelled", "failed"]).raise()? {
        return fail("database_conflict", "Automation cancellation requires settlement");
    }
    let version = integer(payload, "expected_version", None, Some(1), None)?;
    publish_presentations(session, &prefix, current, &run)?;
    let saved = put(session, "run", &key, &run, version)?;
    index(session, &prefix, &run)?;
    Ok(with_value(saved, run))
}

/// `lib/automations/transitions.py::request_cancel`.
fn request_cancel(run: &Value, reason: &str, now: &Value) -> Py<Value> {
    if !(1..=200).contains(&reason.chars().count()) {
        return value_error("Cancellation requires a bounded reason");
    }
    if in_tuple(item(run, "status")?, &runtime("terminalRunStates")) || truthy(get_or_none(run, "cancelCommand")?) {
        return Ok(run.clone());
    }
    let mut result = run.clone();
    result["updatedAt"] = now.clone();
    result["cancelReason"] = json!(reason);
    let active_states = runtime("activeEffectStepStates");
    let Value::Object(steps) = item(&result, "steps")? else { return Err(PyExc::Type) };
    let mut active = false;
    for state in steps.values() {
        if in_tuple(item(state, "status")?, &active_states) {
            active = true;
            break;
        }
    }
    result["status"] = json!(if active { "cancelling" } else { "cancelled" });
    let cancellable = runtime("cancellablePendingStepStates");
    if let Some(Value::Object(steps)) = result.get_mut("steps") {
        for state in steps.values_mut() {
            if in_tuple(&state["status"], &cancellable) {
                state["status"] = json!("cancelled");
            }
        }
    }
    if !active {
        result["completedAt"] = now.clone();
    }
    Ok(result)
}

/// `_automation_run_cancel`.
pub fn run_cancel(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    if !storage_input_valid("cancel", &Value::Object(payload.clone())) {
        return protocol("Automation cancellation does not match its storage contract");
    }
    let owner = integer(payload, "user_id", None, Some(1), Some(SAFE_INTEGER_MAX))?;
    let tenant = payload["tenant_id"].as_str().unwrap_or("").to_owned();
    let key = payload["run_key"].as_str().unwrap_or("").to_owned();
    let now = integer(payload, "now_ms", None, Some(1), Some(SAFE_INTEGER_MAX))?;
    let prefix = owner_prefix(&json!(owner), &tenant);
    if !key.starts_with(&prefix) || clock_ahead(&json!(now), 86_400_000) {
        return protocol("Automation cancellation identity or clock is invalid");
    }
    session.lock_key("automation_run", &key);
    let Some(current) = record(session, "run", &key)? else {
        return fail("database_not_found", "Automation run does not exist");
    };
    let run = &current["value"];
    let escaped = (|| -> Py<bool> {
        Ok(concat(&prefix, item(run, "id")?)? != key
            || !py_eq(item(run, "ownerId")?, &json!(owner))
            || !py_eq(&or_empty(get_or_none(run, "tenantId")?), &json!(tenant))
            || !py_eq(item(run, "jobId")?, &json!(format!("automation-run:{}", sha256_hex(&key)))))
    })()
    .raise()?;
    if escaped {
        return protocol("Automation cancellation escaped its owner");
    }
    let command = get_or_none(run, "cancelCommand").raise()?.clone();
    if truthy(&command)
        && py_eq(item(&command, "id").raise()?, &payload["request_id"])
        && !py_eq(item(&command, "reason").raise()?, &payload["reason"])
    {
        return fail("database_conflict", "Automation cancellation request evidence changed");
    }
    if in_tuple(&run["status"], &runtime("terminalRunStates")) {
        return Ok(json!({"accepted": false, "alreadyTerminal": true, "run": current}));
    }
    if truthy(&command) {
        let job = worker_job_get(session, &run["jobId"], &json!(owner))?;
        if job.is_null() || !truthy(&job["cancelSequence"]) {
            return fail("database_integrity", "Automation cancellation has no durable worker command");
        }
        return Ok(json!({"accepted": true, "alreadyRequested": true, "run": current}));
    }
    let cancellation = worker_job_request_cancel(
        session,
        json!({"task_id": run["jobId"], "user_id": owner, "reason": payload["reason"], "now_ms": now}),
    )?;
    if cancellation.is_null()
        || !truthy(&cancellation["accepted"])
        || !py_eq(&cancellation["job"]["tenantId"], &json!(tenant))
    {
        return fail("database_integrity", "An active automation has no cancellable durable job");
    }
    let reason = payload["reason"].as_str().unwrap_or("");
    let requested_at = super::py::seconds(&json!(now));
    let mut cancelled = request_cancel(run, reason, &requested_at).raise()?;
    if py_eq(&cancelled["status"], &json!("cancelling")) && py_eq(&cancellation["job"]["status"], &json!("cancelled")) {
        return fail("database_integrity", "An active external effect cannot have a queued job");
    }
    cancelled["cancelCommand"] =
        json!({"id": payload["request_id"], "reason": payload["reason"], "requestedAt": requested_at});
    let saved = put(session, "run", &key, &cancelled, version_of(&current))?;
    index(session, &prefix, &cancelled)?;
    Ok(json!({"accepted": true, "alreadyRequested": false, "run": with_value(saved, cancelled)}))
}
