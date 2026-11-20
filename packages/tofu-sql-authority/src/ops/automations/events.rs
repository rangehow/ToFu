//! `operations_pkg/_automation_events.py` (`automation.event.settle`) with
//! private ports of `lib/automations/events.py` (`initial_delivery_cursor`,
//! `validate_event_job`, `validate_delivery_advance`, `task_started_job`,
//! `subscription_prefixes`).

use serde_json::{json, Value};

use super::py::{check_json_bounds, clock_ahead, owner_prefix, sha256_hex, Principal, Py, PyExc};
use super::pyjson;
use super::runs::{canonical, limit, protocol, same};
use super::schema::{storage_input_valid, storage_schema};
use super::{namespace, record_get, record_put_owned};
use crate::codec::py_eq;
use crate::error::{fail, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::Payload;
use crate::session::Session;

fn initial_delivery_cursor() -> Value {
    json!({"index": 0, "afterKey": "", "processed": 0})
}

fn invalid<T>() -> Py<T> {
    Err(PyExc::Value("invalid".into()))
}

/// `round(value * 1000)` (banker's rounding) for an int/float JSON number.
fn round_milliseconds(value: &Value) -> Option<i64> {
    match value.as_i64() {
        Some(integer) => integer.checked_mul(1000),
        None => Some((value.as_f64()? * 1000.0).round_ties_even() as i64),
    }
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

/// `events.subscription_prefixes(owner, context)`.
fn subscription_prefixes(owner: i64, context: &Value) -> Py<Vec<String>> {
    if !storage_input_valid("taskStartedContext", context) {
        return invalid();
    }
    let prefix = format!("{}task.started:", owner_prefix(&json!(owner), text(&context["tenantId"])));
    let mut projects = vec!["*".to_owned()];
    if truthy(context.get("projectId").unwrap_or(&Value::Null)) {
        projects.push(sha256_hex(text(&context["projectId"])));
    }
    let mut prefixes = Vec::new();
    for scope in ["all", text(&context["origin"])] {
        for project in &projects {
            prefixes.push(format!("{prefix}{scope}:{project}:"));
        }
    }
    Ok(prefixes)
}

/// `events.task_started_job(...)` without its delivery cursor.
fn task_started_document(owner: i64, context: &Value, evidence: &Value) -> Py<(String, Value)> {
    if !storage_input_valid("taskStartedContext", context) {
        return invalid();
    }
    let started_at_ms = round_milliseconds(&evidence["startedAt"]).ok_or(PyExc::Type)?;
    if !(0 < started_at_ms && started_at_ms <= limit("safeIntegerMax") / 1000 * 1000) {
        return invalid();
    }
    let (attempt_id, task_id) = (text(&evidence["attemptId"]), text(&evidence["taskId"]));
    let event_id = format!("task-start:{}", sha256_hex(&format!("{attempt_id}:{task_id}")));
    let tenant = text(&context["tenantId"]);
    let scoped = format!("{}{event_id}", owner_prefix(&json!(owner), tenant));
    let job_id = format!("automation-event:{}", sha256_hex(&scoped));
    let principal = Principal::system(&job_id, owner, tenant, &["automations:read", "automations:execute"])?;
    let started = crate::json::float_value(started_at_ms as f64 / 1000.0);
    let mut event = json!({
        "id": event_id, "kind": "task.started", "occurredAt": started,
        "origin": context["origin"], "automationChain": context["automationChain"],
        "evidence": {"startedAt": started, "taskId": task_id, "attemptId": attempt_id,
            "turnId": evidence["turnId"], "conversationId": evidence["conversationId"]},
    });
    if truthy(context.get("projectId").unwrap_or(&Value::Null)) {
        event["projectId"] = context["projectId"].clone();
    }
    let contract = storage_schema()["$defs"]["taskStartedJob"]["properties"]["contract"]["const"].clone();
    let document = json!({"contract": contract, "schemaVersion": 1, "principal": principal.to_payload(),
        "event": event, "delivery": initial_delivery_cursor()});
    if !storage_input_valid("taskStartedJob", &document) {
        return invalid();
    }
    let mut document = document;
    document.as_object_mut().expect("document").shift_remove("delivery");
    Ok((job_id, document))
}

/// `events.validate_event_job(owner, tenant, job_id, payload)`.
fn validate_event_job(owner: i64, tenant: &str, job_id: &str, payload: &Value) -> Py<Vec<String>> {
    if !storage_input_valid("taskStartedJob", payload) {
        return invalid();
    }
    let event = &payload["event"];
    let mut context =
        json!({"tenantId": tenant, "origin": event["origin"], "automationChain": event["automationChain"]});
    if let Some(project) = event.get("projectId") {
        context["projectId"] = project.clone();
    }
    let (expected_id, expected) = task_started_document(owner, &context, &event["evidence"])?;
    let mut frozen = payload.clone();
    frozen.as_object_mut().expect("validated object").shift_remove("delivery");
    if job_id != expected_id || !same(&frozen, &expected) {
        return invalid();
    }
    let prefixes = subscription_prefixes(owner, &context)?;
    let initial = initial_delivery_cursor();
    let cursor = payload.get("delivery").unwrap_or(&initial);
    let index = cursor["index"].as_u64().unwrap_or(0) as usize;
    let after_key = text(&cursor["afterKey"]);
    if index > prefixes.len()
        || (index == prefixes.len() && !after_key.is_empty())
        || (!after_key.is_empty() && !after_key.starts_with(&prefixes[index]))
    {
        return invalid();
    }
    Ok(prefixes)
}

/// `events.validate_delivery_advance(prefixes, current, next_cursor)`.
fn validate_delivery_advance(prefixes: &[String], current: &Value, next: &Value) -> Py<()> {
    if !storage_input_valid("eventDeliveryCursor", next) {
        return invalid();
    }
    let delta = next["processed"].as_i64().unwrap_or(0) - current["processed"].as_i64().unwrap_or(0);
    let (index, next_index) = (current["index"].as_i64().unwrap_or(0), next["index"].as_i64().unwrap_or(0));
    let (after, next_after) = (text(&current["afterKey"]), text(&next["afterKey"]));
    let same_prefix = next_index == index
        && (index as usize) < prefixes.len()
        && next_after.starts_with(&prefixes[index as usize])
        && next_after > after
        && delta > 0;
    let following = next_index == index + 1 && next_index as usize <= prefixes.len() && next_after.is_empty();
    let batch = storage_schema()["x-event"]["batchRows"].as_i64().unwrap_or(0);
    if !((0..=batch).contains(&delta) && (same_prefix || following)) {
        return invalid();
    }
    Ok(())
}

fn with_default_delivery(value: &Value) -> Value {
    let mut value = value.clone();
    if let Value::Object(map) = &mut value {
        if !map.contains_key("delivery") {
            map.insert("delivery".into(), initial_delivery_cursor());
        }
    }
    value
}

/// `_automation_event_settle`.
pub fn event_settle(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let document = Value::Object(payload.clone());
    if check_json_bounds(&document, 24, 16384).is_err()
        || pyjson::dumps_ascii_sorted(&document).len() as i64
            > storage_schema()["x-event"]["settlementRequestBytes"].as_i64().unwrap_or(0)
    {
        return protocol("Event settlement exceeds its bounds");
    }
    if !storage_input_valid("eventSettle", &document) {
        return protocol("Invalid event settlement request");
    }
    let owner = payload["user_id"].as_i64().unwrap_or(0);
    let tenant = text(&payload["tenant_id"]).to_owned();
    let job_id = text(&payload["job_id"]).to_owned();
    let expected = with_default_delivery(&payload["expected_payload"]);
    let mode = text(&payload["mode"]).to_owned();
    let next_cursor = &payload["next_delivery"];
    let now = &payload["now_ms"];
    let binding = (|| -> Py<()> {
        let prefixes = validate_event_job(owner, &tenant, &job_id, &expected)?;
        if mode == "advance" {
            validate_delivery_advance(&prefixes, &expected["delivery"], next_cursor)?;
        } else if !next_cursor.is_null()
            || (mode == "succeeded" && expected["delivery"]["index"].as_u64() != Some(prefixes.len() as u64))
        {
            return invalid();
        }
        if truthy(&payload["error_kind"]) != (mode == "failed") {
            return invalid();
        }
        Ok(())
    })();
    match binding {
        Ok(()) => {}
        Err(PyExc::Storage(failure)) => return Err(failure),
        Err(_) => return protocol("Invalid event settlement binding"),
    }
    if clock_ahead(now, storage_schema()["x-limits"]["clockSkewMs"].as_i64().unwrap_or(0)) {
        return protocol("Invalid event settlement clock");
    }
    session.lock_key("worker_job", &job_id);
    let job = super::py::worker_job_get(session, &json!(job_id), &json!(owner))?;
    let failure_key = format!("{}{job_id}", owner_prefix(&json!(owner), &tenant));
    if job.is_null() {
        if mode == "advance" {
            return Ok(json!({"ok": false, "error": "producer_retired"}));
        }
        let failure = record_get(session, namespace("eventFailure"), &failure_key)?;
        if let Some(failure) = failure {
            if mode == "failed" && !py_eq(&failure["value"]["kind"], &payload["error_kind"]) {
                return fail("database_conflict", "Event failure evidence changed");
            }
        }
        return Ok(json!({"ok": true, "applied": false, "retired": true}));
    }
    let mut current = with_default_delivery(&job["payload"]);
    if !py_eq(&job["tenantId"], &json!(tenant))
        || !py_eq(&job["taskKind"], &json!("automation-event"))
        || !py_eq(&job["idempotencyKey"], &json!(job_id))
    {
        return protocol("Event transport escaped its owner");
    }
    if mode == "advance" {
        let mut advanced = expected.clone();
        advanced["delivery"] = next_cursor.clone();
        if canonical(&current) == canonical(&advanced) {
            return Ok(json!({"ok": true, "applied": false, "retired": false, "payload": current}));
        }
    }
    if canonical(&current) != canonical(&expected) {
        return fail("database_conflict", "Event cursor changed");
    }
    let proof = super::py::worker_job_claim_state(
        session,
        &json!(job_id),
        &payload["worker_id"],
        &payload["fencing_token"],
        now,
    )?;
    if !truthy(&proof["ok"]) {
        return Ok(json!({"ok": false, "error": "producer_fence_lost"}));
    }
    if matches!(mode.as_str(), "advance" | "succeeded" | "failed") && truthy(&proof["cancelSequence"]) {
        return Ok(json!({"ok": false, "error": "cancellation_pending"}));
    }
    if mode == "cancelled" && !truthy(&proof["cancelSequence"]) {
        return protocol("Event cancellation requires owner intent");
    }
    if mode == "advance" {
        current["delivery"] = next_cursor.clone();
        let changed = session.execute(
            "UPDATE storage_worker_jobs SET payload_json=?,updated_at_ms=? \
             WHERE task_id=? AND user_id=? AND tenant_id=?",
            &params![pyjson::dumps_ascii_sorted(&current), crate::session::json_param(now)?, &job_id, owner, &tenant],
        )?;
        if changed != 1 {
            return fail("database_conflict", "Event transport changed");
        }
        return Ok(json!({"ok": true, "applied": true, "retired": false, "payload": current}));
    }
    if mode == "failed" {
        let diagnostic = json!({
            "id": job_id, "ownerId": owner, "tenantId": tenant, "event": expected["event"],
            "delivery": expected["delivery"], "kind": payload["error_kind"],
            "failedAt": super::py::seconds(now),
        });
        record_put_owned(session, namespace("eventFailure"), &failure_key, &diagnostic, 0)?;
    }
    let deleted = session.execute(
        "DELETE FROM storage_worker_jobs WHERE task_id=? AND user_id=? AND tenant_id=?",
        &params![&job_id, owner, &tenant],
    )?;
    if deleted != 1 {
        return fail("database_conflict", "Event transport changed during retirement");
    }
    Ok(json!({"ok": true, "applied": true, "retired": true}))
}
