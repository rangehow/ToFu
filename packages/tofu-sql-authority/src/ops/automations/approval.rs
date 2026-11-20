//! `operations_pkg/_automation_approval.py` (`automation.run.approve`) with
//! private ports of `lib/automations/approval.py::prepare_approval` and
//! `registry.py::ActionContract.from_document`.

use std::collections::BTreeSet;
use std::sync::OnceLock;

use serde_json::{json, Value};

use super::py::{
    check_json_bounds, clock_ahead, concat, get_or_none, in_set, item, item_by, iterate, or_empty, owner_prefix,
    seconds, sha256_hex, value_error, worker_job_wake_owned, Principal, Py, PyExc,
};
use super::pyjson::{self, LoadError};
use super::runs::{canonical, index, limit, protocol, put, record, with_value, Raise};
use super::schema::storage_input_valid;
use crate::codec::py_eq;
use crate::error::{fail, OpResult};
use crate::json::{python_str, truthy};
use crate::payload::Payload;
use crate::session::Session;

/// `approval.request_digest`: the payload without `now_ms`.
fn request_digest(payload: &Payload) -> String {
    let mut basis = payload.clone();
    basis.shift_remove("now_ms");
    sha256_hex(&canonical(&Value::Object(basis)))
}

fn regex_identity(text: &str) -> bool {
    let mut characters = text.chars();
    characters.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && text.chars().count() <= 160
        && characters.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
}

/// Canonical input/output schemas of the closed action catalogue.
fn catalogue_schemas() -> &'static BTreeSet<String> {
    static SCHEMAS: OnceLock<BTreeSet<String>> = OnceLock::new();
    SCHEMAS.get_or_init(|| {
        let catalog: Value =
            serde_json::from_str(include_str!("../../../../../contracts/automation_actions_v1.json")).expect("catalog");
        catalog["actions"]
            .as_array()
            .expect("actions")
            .iter()
            .flat_map(|action| [canonical(&action["input"]), canonical(&action["output"])])
            .collect()
    })
}

/// `ActionContract.from_document(value)`: `(effect, required_scopes)`.
fn action_contract(value: &Value) -> Py<(String, Vec<Value>)> {
    check_json_bounds(value, limit("jsonDepth") as usize, limit("jsonNodes") as usize)?;
    const KEYS: [&str; 7] = ["id", "version", "input", "output", "effect", "recovery", "requiredScopes"];
    let Value::Object(map) = value else { return value_error("Invalid action descriptor") };
    if map.len() != KEYS.len() || !KEYS.iter().all(|key| map.contains_key(*key)) {
        return value_error("Invalid action descriptor");
    }
    let identity_ok = map["id"].as_str().is_some_and(regex_identity)
        && map["version"].as_i64().is_some_and(|version| version >= 1)
        && in_set(&map["effect"], &["read", "presentation", "write", "agent"])?
        && in_set(&map["recovery"], &["replay", "reconcile"])?;
    if !identity_ok {
        return value_error("Invalid action identity or effect policy");
    }
    let scopes_ok = match &map["requiredScopes"] {
        Value::Array(scopes) if !scopes.is_empty() => {
            scopes.iter().all(|scope| scope.as_str().is_some_and(|s| (1..=128).contains(&s.chars().count())))
                && scopes.iter().enumerate().all(|(index, scope)| !scopes[..index].contains(scope))
        }
        _ => false,
    };
    if !scopes_ok {
        return value_error("Action requires explicit unique scopes");
    }
    let effect = map["effect"].as_str().unwrap_or("").to_owned();
    if (effect == "write" || effect == "agent") && map["recovery"] != "reconcile" {
        return value_error("External effects must declare reconciliation");
    }
    for schema in [&map["input"], &map["output"]] {
        if !schema.is_object() {
            return value_error("Action schema must be an object");
        }
        let mut pending = vec![schema];
        while let Some(node) = pending.pop() {
            match node {
                Value::Object(node) => {
                    if ["$id", "$anchor", "$dynamicAnchor"].iter().any(|key| node.contains_key(*key)) {
                        return value_error("Action schemas cannot redefine resource scope");
                    }
                    for key in ["$ref", "$dynamicRef"] {
                        if node
                            .get(key)
                            .is_some_and(|reference| !reference.as_str().is_some_and(|r| r.starts_with("#/")))
                        {
                            return value_error("Action schemas cannot fetch remote resources");
                        }
                    }
                    pending.extend(node.values());
                }
                Value::Array(items) => pending.extend(items.iter()),
                _ => {}
            }
        }
        // `Draft202012Validator.check_schema`: publication pins every stored
        // action to the closed catalogue, whose schemas are valid; a schema
        // outside it cannot reach a run through any storage operation.
        if !catalogue_schemas().contains(&canonical(schema)) {
            return Err(PyExc::Type);
        }
    }
    let scopes = map["requiredScopes"].as_array().cloned().unwrap_or_default();
    Ok((effect, scopes))
}

/// `canonical(json.loads(text))` (ValueError text on failure).
fn canonical_loads(text: &Value) -> Py<String> {
    let Value::String(text) = text else { return Err(PyExc::Type) };
    match pyjson::loads(text) {
        Ok(value) => Ok(canonical(&value)),
        Err(LoadError::Unrepresentable) => Err(PyExc::Type),
        Err(error) => Err(PyExc::Value(error.message())),
    }
}

/// `approval.prepare_approval(run, payload)`.
fn prepare_approval(run: &Value, payload: &Payload) -> Py<Value> {
    let actor_payload = &payload["actor_principal"];
    check_json_bounds(actor_payload, limit("recordDepth") as usize, limit("recordNodes") as usize)?;
    if canonical(actor_payload).len() as i64 > limit("recordBytes") {
        return value_error("Approval actor exceeds the record budget");
    }
    let actor = Principal::from_payload(actor_payload)?;
    let owner_matches = py_eq(&json!(actor.owner_user_id), item(run, "ownerId")?);
    if !py_eq(&actor.to_payload(), actor_payload)
        || actor.scopes.contains("admin")
        || !owner_matches
        || !py_eq(&json!(actor.tenant_id), item(run, "tenantId")?)
    {
        return value_error("Approval actor escaped its owner or canonical grant");
    }
    for scope in ["automations:read", "automations:manage", "automations:execute"] {
        actor.require_scope(&json!(scope))?;
    }
    let step_id = &payload["step_id"];
    let mut step = None;
    for candidate in iterate(item(item(run, "snapshot")?, "steps")?)? {
        if py_eq(item(&candidate, "id")?, step_id) {
            step = Some(candidate);
            break;
        }
    }
    let steps = item(run, "steps")?;
    let Value::Object(step_states) = steps else { return Err(PyExc::Type) };
    let state = step_id.as_str().and_then(|id| step_states.get(id));
    let (Some(step), Some(state)) = (step, state) else {
        return Err(PyExc::Conflict("Approval step does not exist".into()));
    };
    let action_document = item_by(item(run, "actions")?, item(&step, "action")?)?;
    let (effect, required_scopes) = action_contract(action_document)?;
    for scope in &required_scopes {
        actor.require_scope(scope)?;
    }
    let now_seconds = seconds(&payload["now_ms"]);
    let expired = || -> Py<bool> {
        match (now_seconds.as_f64(), item(run, "deadlineAt")?.as_f64()) {
            (Some(now), Some(deadline)) => Ok(now >= deadline),
            _ => Err(PyExc::Type),
        }
    };
    if !py_eq(item(run, "status")?, &json!("waiting_permission"))
        || !py_eq(item(state, "status")?, &json!("waiting_permission"))
        || !py_eq(item(item(item(run, "snapshot")?, "policy")?, "permissionMode")?, &json!("manual"))
        || !(effect == "write" || effect == "agent")
        || truthy(get_or_none(state, "approval")?)
        || truthy(get_or_none(run, "cancelCommand")?)
        || expired()?
    {
        return Err(PyExc::Conflict("Approval requires a current unexpired permission wait".into()));
    }
    let arguments = get_or_none(state, "arguments")?;
    if !arguments.is_object() {
        return Err(PyExc::Conflict("Approval requires concrete review arguments".into()));
    }
    if canonical_loads(&payload["arguments_json"])? != canonical(arguments)
        || canonical_loads(&payload["action_json"])? != canonical(action_document)
    {
        return Err(PyExc::Conflict("Reviewed arguments or action changed".into()));
    }
    let mut result = run.clone();
    let effect_key = item(state, "effectKey")?.clone();
    let approved = &mut result["steps"][step_id.as_str().unwrap_or("")];
    approved["approval"] = json!({
        "requestId": payload["request_id"],
        "requestDigest": request_digest(payload),
        "actorSubjectId": actor.subject_id,
        "approvedAt": now_seconds,
        "argumentsDigest": sha256_hex(payload["arguments_json"].as_str().unwrap_or("")),
        "actionDigest": sha256_hex(payload["action_json"].as_str().unwrap_or("")),
        "effectKey": effect_key,
    });
    approved["status"] = json!("pending");
    result["status"] = json!("running");
    result["updatedAt"] = now_seconds;
    check_json_bounds(&result, limit("recordDepth") as usize, limit("recordNodes") as usize)?;
    if canonical(&result).len() as i64 > limit("recordBytes") {
        return value_error("Approval evidence exceeds the run budget");
    }
    Ok(result)
}

/// `_automation_run_approve`.
pub fn run_approve(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    if !storage_input_valid("approve", &Value::Object(payload.clone())) {
        return protocol("Automation approval does not match its storage contract");
    }
    let (owner, tenant) = (&payload["user_id"], payload["tenant_id"].as_str().unwrap_or("").to_owned());
    let key = payload["run_key"].as_str().unwrap_or("").to_owned();
    let prefix = owner_prefix(owner, &tenant);
    if !key.starts_with(&prefix) || clock_ahead(&payload["now_ms"], 86_400_000) {
        return protocol("Automation approval identity or clock is invalid");
    }
    session.lock_key("automation_run", &key);
    let Some(current) = record(session, "run", &key)? else {
        return fail("database_not_found", "Automation run does not exist");
    };
    let run = &current["value"];
    let escaped = (|| -> Py<bool> {
        Ok(concat(&prefix, item(run, "id")?)? != key
            || !py_eq(item(run, "ownerId")?, owner)
            || !py_eq(&or_empty(item(run, "tenantId")?), &json!(tenant))
            || !py_eq(item(run, "jobId")?, &json!(format!("automation-run:{}", sha256_hex(&key)))))
    })()
    .raise()?;
    if escaped {
        return protocol("Automation approval escaped its owner");
    }
    let previous = (|| -> Py<Value> {
        let empty = json!({});
        let steps = item(run, "steps")?;
        let Value::Object(steps) = steps else { return Err(PyExc::Type) };
        let state = payload["step_id"].as_str().and_then(|id| steps.get(id)).unwrap_or(&empty);
        Ok(get_or_none(state, "approval")?.clone())
    })()
    .raise()?;
    if truthy(&previous) {
        let changed = (|| -> Py<bool> {
            Ok(!py_eq(item(&previous, "requestId")?, &payload["request_id"])
                || !py_eq(item(&previous, "requestDigest")?, &json!(request_digest(payload))))
        })()
        .raise()?;
        if changed {
            return fail("database_conflict", "Approval request evidence changed");
        }
        return Ok(json!({"accepted": true, "alreadyApproved": true, "run": current}));
    }
    if !py_eq(&current["version"], &payload["expected_version"]) {
        return fail("database_conflict", "Automation approval revision changed");
    }
    let updated = match prepare_approval(run, payload) {
        Ok(updated) => updated,
        Err(PyExc::Conflict(message)) => return fail("database_conflict", message),
        Err(exception) => {
            return match exception.message(true) {
                Some(message) => protocol(&message),
                None => Err(exception.raise()),
            }
        }
    };
    let job_id = python_str(&run["jobId"]);
    let job = worker_job_wake_owned(session, &job_id, owner, &tenant, &payload["now_ms"])?;
    if job.is_null() || truthy(&job["cancelSequence"]) {
        return fail("database_conflict", "Approval requires a noncancelled canonical job");
    }
    let saved = put(session, "run", &key, &updated, current["version"].as_i64().unwrap_or(0))?;
    index(session, &prefix, &updated)?;
    Ok(json!({"accepted": true, "alreadyApproved": false, "run": with_value(saved, updated)}))
}
