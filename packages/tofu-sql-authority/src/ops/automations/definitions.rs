//! `operations_pkg/_automation_definitions.py` (`automation.definition.update`)
//! with private ports of `lib/automations/publication.py`
//! (`validate_publication_record`, `request_digest`, `subscription_key`,
//! `clock_job_id`).

use std::collections::{BTreeSet, HashMap};
use std::sync::OnceLock;

use serde_json::{json, Value};

use super::py::{
    check_json_bounds, clock_ahead, concat, get, get_or_none, in_set, item, iterate, or_empty, owner_prefix,
    record_delete_owned, seconds, sha256_hex, worker_job_enqueue_owned, worker_job_get, Principal, Py, PyExc,
};
use super::pyjson::{self, LoadError};
use super::runs::{canonical, limit, protocol, require_project, same, with_value, Raise};
use super::schema::{storage_input_valid, storage_schema};
use super::{namespace, record_get, record_put_owned};
use crate::codec::py_eq;
use crate::error::{fail, OpResult};
use crate::json::{python_str, truthy};
use crate::payload::{integer, Payload};
use crate::session::Session;

fn catalogue() -> &'static HashMap<String, Value> {
    static CATALOGUE: OnceLock<HashMap<String, Value>> = OnceLock::new();
    CATALOGUE.get_or_init(|| {
        let catalog: Value =
            serde_json::from_str(include_str!("../../../../../contracts/automation_actions_v1.json")).expect("catalog");
        catalog["actions"]
            .as_array()
            .expect("actions")
            .iter()
            .map(|action| (action["id"].as_str().expect("action id").to_owned(), action.clone()))
            .collect()
    })
}

fn max_steps() -> usize {
    static MAX: OnceLock<usize> = OnceLock::new();
    *MAX.get_or_init(|| {
        let schema: Value =
            serde_json::from_str(include_str!("../../../../../contracts/automation_v1.schema.json")).expect("schema");
        schema["properties"]["steps"]["maxItems"].as_u64().expect("steps maxItems") as usize
    })
}

/// `json.loads(text)`; a decode failure is ValueError, a non-str TypeError.
fn loads(text: &Value) -> Py<Value> {
    let Value::String(text) = text else { return Err(PyExc::Type) };
    match pyjson::loads(text) {
        Ok(value) => Ok(value),
        Err(LoadError::Decode(message)) | Err(LoadError::NonFinite(message)) => Err(PyExc::Value(message)),
        Err(LoadError::Unrepresentable) => Err(PyExc::Type),
    }
}

fn identity(text: &str) -> bool {
    let mut characters = text.chars();
    characters.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && text.chars().count() <= 160
        && characters.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
}

fn key_set(value: &Value) -> Py<BTreeSet<String>> {
    match value {
        Value::Object(map) => Ok(map.keys().cloned().collect()),
        Value::Array(_) | Value::String(_) => Ok(iterate(value)?.iter().map(python_str).collect()),
        _ => Err(PyExc::Type),
    }
}

fn set_of(keys: &[&str]) -> BTreeSet<String> {
    keys.iter().map(|key| (*key).to_owned()).collect()
}

/// The inner `try` of `validate_publication_record`: `Ok(Err(()))` is any
/// KeyError/TypeError/ValueError/PermissionError it converts to
/// `AutomationError`; `Err` is an exception escaping it (AttributeError).
fn publication_binding(
    record: &Value,
    definition_json: &Value,
    actions_json: &Value,
    owner: i64,
    tenant: &str,
) -> Result<Option<String>, PyExc> {
    let inner = || -> Py<String> {
        let invalid = || Err(PyExc::Value("invalid".into()));
        let definition_text = match definition_json {
            Value::String(text) => text,
            _ => return invalid(),
        };
        if key_set(record)? != set_of(&["definition", "compiled", "grant"])
            || key_set(item(record, "compiled")?)? != set_of(&["digest", "stepOrder", "actions"])
            || key_set(item(record, "grant")?)? != set_of(&["principal", "approvedBy"])
            || definition_text.len() as i64 > limit("definitionBytes")
            || canonical(&loads(definition_json)?) != canonical(item(record, "definition")?)
        {
            return invalid();
        }
        let definition = item(record, "definition")?;
        let compiled = item(record, "compiled")?;
        let entity_id = item(definition, "id")?;
        let Value::String(entity_id) = entity_id else { return invalid() };
        if !identity(entity_id) || !item(definition, "enabled")?.is_boolean() {
            return invalid();
        }
        let Value::String(actions_text) = actions_json else { return Err(PyExc::Type) };
        let digest = sha256_hex(&format!("{definition_text}\n{actions_text}"));
        if !py_eq(item(compiled, "digest")?, &json!(digest))
            || canonical(&loads(actions_json)?) != canonical(item(compiled, "actions")?)
        {
            return invalid();
        }
        let trigger = item(definition, "trigger")?;
        let kind = item(trigger, "kind")?;
        if !in_set(kind, &["event", "schedule", "manual"])? {
            return invalid();
        }
        if py_eq(kind, &json!("event"))
            && (!in_set(item(trigger, "event")?, &["task.started", "skills.updated"])?
                || !in_set(item(trigger, "scope")?, &["all", "human", "automation"])?)
        {
            return invalid();
        }
        let Value::Array(steps) = item(definition, "steps")? else { return invalid() };
        if steps.is_empty() || steps.len() > max_steps() {
            return invalid();
        }
        let mut ids = Vec::new();
        for step in steps {
            ids.push(item(step, "id")?.clone());
        }
        let Value::Array(order) = item(compiled, "stepOrder")? else { return invalid() };
        if ids.iter().chain(order.iter()).any(|key| !key.is_string()) {
            return invalid();
        }
        let id_set: BTreeSet<&str> = ids.iter().filter_map(Value::as_str).collect();
        let order_set: BTreeSet<&str> = order.iter().filter_map(Value::as_str).collect();
        if id_set.len() != ids.len() || order.len() != ids.len() || order_set != id_set {
            return invalid();
        }
        let positions: HashMap<&str, usize> =
            order.iter().enumerate().map(|(index, key)| (key.as_str().unwrap_or(""), index)).collect();
        for step in steps {
            let Value::Array(after) = item(step, "after")? else { return invalid() };
            if after.iter().any(|parent| !parent.is_string()) {
                return invalid();
            }
        }
        for step in steps {
            let own = positions[item(step, "id")?.as_str().unwrap_or("")];
            for parent in item(step, "after")?.as_array().expect("checked list") {
                match positions.get(parent.as_str().unwrap_or("")) {
                    Some(position) if *position < own => {}
                    _ => return invalid(),
                }
            }
        }
        let actions = item(compiled, "actions")?;
        let mut referenced = BTreeSet::new();
        for step in steps {
            let action = item(step, "action")?;
            match action {
                Value::Array(_) | Value::Object(_) => return Err(PyExc::Type),
                other => {
                    referenced.insert(python_str(other) + if other.is_string() { "" } else { "\u{0}" });
                }
            }
        }
        if key_set(actions)? != referenced {
            return invalid();
        }
        let Value::Object(action_map) = actions else {
            return Err(PyExc::Storage(crate::error::Failure::Internal("AttributeError: items".into())));
        };
        for (key, value) in action_map {
            if !catalogue().get(key).is_some_and(|entry| py_eq(value, entry)) {
                return invalid();
            }
        }
        let grant = item(record, "grant")?;
        let raw_principal = item(grant, "principal")?;
        if !raw_principal.is_object() {
            return Err(PyExc::Storage(crate::error::Failure::Internal("AttributeError: get".into())));
        }
        let principal = Principal::from_payload(raw_principal)?;
        let mut required: BTreeSet<String> = set_of(&["automations:read", "automations:execute"]);
        for action in action_map.values() {
            for scope in iterate(item(action, "requiredScopes")?)? {
                required.insert(python_str(&scope));
            }
        }
        let approved_by = item(grant, "approvedBy")?;
        if principal.kind != "system"
            || principal.subject_id != format!("automation:{owner}:{entity_id}")
            || principal.require_owner("operation")? != owner
            || principal.tenant_id.as_deref().unwrap_or("") != tenant
            || principal.scopes != required
            || !py_eq(&principal.to_payload(), item(grant, "principal")?)
            || !approved_by.as_str().is_some_and(|by| (1..=256).contains(&by.chars().count()))
        {
            return invalid();
        }
        Ok(entity_id.clone())
    };
    check_json_bounds(record, limit("recordDepth") as usize, limit("recordNodes") as usize)?;
    match inner() {
        Ok(entity_id) => Ok(Some(entity_id)),
        Err(PyExc::Storage(failure)) => Err(PyExc::Storage(failure)),
        Err(_) => Ok(None),
    }
}

/// `publication.request_digest`.
fn request_digest(payload: &Payload) -> Py<String> {
    let record = &payload["record"];
    let mut basis = json!({
        "definition_key": payload["definition_key"], "expected_version": payload["expected_version"],
        "mode": payload["mode"], "actorSubjectId": payload["actor_subject_id"],
        "definitionJson": payload["definition_json"], "actionsJson": payload["actions_json"],
    });
    if !record.is_null() {
        basis["stepOrder"] = item(item(record, "compiled")?, "stepOrder")?.clone();
        basis["grant"] = item(record, "grant")?.clone();
    }
    Ok(sha256_hex(&canonical(&basis)))
}

/// `publication.subscription_key`.
fn subscription_key(prefix: &str, definition: &Value) -> Py<Value> {
    let trigger = item(definition, "trigger")?;
    if !truthy(item(definition, "enabled")?) || !py_eq(item(trigger, "kind")?, &json!("event")) {
        return Ok(Value::Null);
    }
    let project = get_or_none(definition, "projectId")?;
    let project_key = if truthy(project) {
        match project {
            Value::String(text) => sha256_hex(text),
            _ => return Err(PyExc::Type),
        }
    } else {
        "*".to_owned()
    };
    Ok(json!(format!(
        "{prefix}{}:{}:{project_key}:{}",
        python_str(item(trigger, "event")?),
        python_str(item(trigger, "scope")?),
        python_str(item(definition, "id")?)
    )))
}

/// `publication.clock_job_id`.
fn clock_job_id(key: &str, revision: i64) -> String {
    format!("automation-clock:{}", sha256_hex(&format!("{key}:{revision}")))
}

/// `python text[:count]`.
fn truncate(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// `_automation_definition_update`.
pub fn definition_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let document = Value::Object(payload.clone());
    if !storage_input_valid("definitionUpdate", &document) {
        return protocol("Automation publication does not match its contract");
    }
    let limits = &storage_schema()["x-limits"];
    if canonical(&document).len() as i64 > limits["publicationRequestBytes"].as_i64().unwrap_or(0) {
        return protocol("Automation publication exceeds its request bound");
    }
    let safe = limit("safeIntegerMax");
    let owner = integer(payload, "user_id", None, Some(1), Some(safe))?;
    let now = integer(payload, "now_ms", None, Some(1), Some(safe))?;
    let version = integer(payload, "expected_version", None, Some(0), Some(safe))?;
    let tenant = payload["tenant_id"].as_str().unwrap_or("").to_owned();
    let key = payload["definition_key"].as_str().unwrap_or("").to_owned();
    let prefix = owner_prefix(&json!(owner), &tenant);
    if !key.starts_with(&prefix) || clock_ahead(&json!(now), limits["clockSkewMs"].as_i64().unwrap_or(0)) {
        return protocol("Automation publication identity or clock is invalid");
    }
    let mut due = payload["first_due_ms"].clone();
    if !due.is_null() {
        due = json!(integer(payload, "first_due_ms", None, Some(1), Some(safe))?);
    }
    let record_value = payload["record"].clone();
    if !record_value.is_null() && !record_value.is_object() {
        return protocol("Invalid prepared publication binding");
    }
    let mut entity_id = String::new();
    if !record_value.is_null() {
        match publication_binding(&record_value, &payload["definition_json"], &payload["actions_json"], owner, &tenant)
        {
            Ok(Some(identity)) => entity_id = identity,
            Ok(None) => return protocol("Invalid prepared publication binding"),
            Err(PyExc::Value(_)) => return protocol("Invalid prepared publication binding"),
            Err(exception) => return Err(exception.raise()),
        }
        if !record_value["grant"].is_object() {
            return protocol("Invalid prepared publication binding");
        }
        if !py_eq(&record_value["grant"]["approvedBy"], &payload["actor_subject_id"]) {
            return protocol("Publication actor differs from its approval");
        }
        if key != format!("{prefix}{entity_id}")
            || canonical(&record_value).len() as i64
                > limit("recordBytes") - limits["publicationMetadataReserveBytes"].as_i64().unwrap_or(0)
        {
            return protocol("Automation publication escaped its identity or record bound");
        }
    }
    let digest = request_digest(payload).raise()?;
    session.lock_key("automation_definition", &key);
    let definition_namespace = namespace("definition");
    let current = record_get(session, definition_namespace, &key)?.map(Value::Object);
    if let Some(current) = &current {
        let old = &current["value"];
        let escaped = (|| -> Py<bool> {
            Ok(!matches!(get_or_none(old, "ownerId")?, Value::Number(n) if n.is_i64() || n.is_u64())
                || !py_eq(item(old, "ownerId")?, &json!(owner))
                || !py_eq(&or_empty(get_or_none(old, "tenantId")?), &json!(tenant))
                || key != concat(&prefix, item(old, "id")?)?)
        })()
        .raise()?;
        if escaped {
            return protocol("Automation publication escaped its owner");
        }
        let command = old["publicationCommand"].clone();
        let (same_request, same_digest) = if truthy(&command) {
            let same_request = py_eq(item(&command, "id").raise()?, &payload["request_id"]);
            if same_request && !py_eq(item(&command, "digest").raise()?, &json!(digest)) {
                return fail("database_conflict", "Automation publication request evidence changed");
            }
            (same_request, same_request)
        } else {
            (false, false)
        };
        if current["version"].as_i64() == Some(version + 1) && same_request && same_digest {
            return Ok(current.clone());
        }
    }
    let current_version = current.as_ref().map(|current| current["version"].as_i64().unwrap_or(0)).unwrap_or(0);
    if current_version != version {
        return fail("database_conflict", "Automation definition revision changed");
    }
    let mode = payload["mode"].as_str().unwrap_or("").to_owned();
    let inactive = mode == "pause" || mode == "retire";
    if inactive && current.is_none() {
        return fail("database_not_found", "Automation definition does not exist");
    }
    if mode == "publish" {
        if record_value.is_null() || !record_value["definition"].is_object() {
            return protocol("Invalid prepared publication binding");
        }
        require_project(session, owner, &tenant, &record_value["definition"]["projectId"])?;
    }
    let mut inactive_definition = Value::Null;
    if inactive {
        let current = current.as_ref().expect("inactive mode has a current definition");
        let result = (|| -> Py<Option<&'static str>> {
            inactive_definition = loads(&payload["definition_json"])?;
            let mut expected = item(&current["value"], "definition")?.clone();
            match &mut expected {
                Value::Object(map) => {
                    map.insert("enabled".into(), Value::Bool(false));
                }
                Value::Array(_) => return Err(PyExc::Type),
                _ => return Err(PyExc::Type),
            }
            if !same(&inactive_definition, &expected)
                || !same(&loads(&payload["actions_json"])?, item(item(&current["value"], "compiled")?, "actions")?)
            {
                return Ok(Some("State changes cannot modify the published definition or actions"));
            }
            Ok(None)
        })();
        match result {
            Ok(Some(message)) => return protocol(message),
            Ok(None) => {}
            Err(PyExc::Value(_) | PyExc::Type) => return protocol("Invalid inactive definition binding"),
            Err(exception) => return Err(exception.raise()),
        }
    }
    let mut last_clock_failure = match &current {
        Some(current) if !get_or_none(&current["value"], "grant").raise()?.is_null() => {
            current["value"]["lastClockFailure"].clone()
        }
        _ => Value::Null,
    };
    let empty = json!({});
    let old_state = match &current {
        Some(current) => get(&current["value"], "triggerState").raise()?.unwrap_or(&empty).clone(),
        None => empty.clone(),
    };
    let old_subscription = get_or_none(&old_state, "subscriptionKey").raise()?.clone();
    if truthy(&old_subscription) {
        let Some(current) = &current else {
            return fail("database_integrity", "Stored subscription identity is inconsistent");
        };
        let expected = subscription_key(&prefix, item(&current["value"], "definition").raise()?).raise()?;
        if !py_eq(&old_subscription, &expected) {
            return fail("database_integrity", "Stored subscription identity is inconsistent");
        }
        let subscription = old_subscription.as_str().unwrap_or("");
        if let Some(previous) = record_get(session, namespace("subscription"), subscription)? {
            let previous_version = previous["version"].as_i64().unwrap_or(0);
            record_delete_owned(session, namespace("subscription"), subscription, previous_version)?;
        }
    }
    let old_clock = get_or_none(&old_state, "clockJobId").raise()?.clone();
    if let (true, Some(current)) = (truthy(&old_clock), &current) {
        let current_version = current["version"].as_i64().unwrap_or(0);
        if !py_eq(&old_clock, &json!(clock_job_id(&key, current_version))) {
            return fail("database_integrity", "Stored clock identity is inconsistent");
        }
        let old_clock = old_clock.as_str().unwrap_or("").to_owned();
        session.lock_key("worker_job", &old_clock);
        let previous_job = worker_job_get(session, &json!(old_clock), &json!(owner))?;
        if previous_job.is_null() {
            return fail("database_integrity", "Published clock job is missing");
        }
        let source = &previous_job["payload"];
        let value = &current["value"];
        if !value.is_object() || !value["compiled"].is_object() {
            return fail("database_integrity", "Stored automation publication is inconsistent");
        }
        let escaped = (|| -> Py<bool> {
            Ok(!py_eq(&previous_job["tenantId"], &json!(tenant))
                || !py_eq(&previous_job["taskKind"], &json!("automation-clock"))
                || !py_eq(&previous_job["idempotencyKey"], &json!(old_clock))
                || !source.is_object()
                || !py_eq(&source["definitionKey"], &json!(key))
                || !matches!(&source["definitionRevision"], Value::Number(n) if n.is_i64() || n.is_u64())
                || !py_eq(&source["definitionRevision"], &json!(current_version))
                || !py_eq(&source["definitionId"], item(value, "id")?)
                || !py_eq(&source["definitionDigest"], item(item(value, "compiled")?, "digest")?)
                || !same(&source["principal"], item(item(value, "grant")?, "principal")?))
        })()
        .raise()?;
        if escaped {
            return protocol("Automation clock escaped its immutable binding");
        }
        if previous_job["status"] == "failed" {
            let kind = get_or_none(&previous_job["error"], "kind").raise()?;
            let characters = storage_schema()["x-clock"]["supersession"]["failureKindCharacters"].as_u64().unwrap_or(0);
            last_clock_failure = json!({
                "jobId": old_clock, "revision": current_version,
                "failedAtMs": previous_job["terminalAtMs"],
                "kind": match kind {
                    Value::String(kind) => truncate(kind, characters as usize),
                    _ => "automation_clock_failed".to_owned(),
                },
            });
        }
        let deleted = session.execute(
            "DELETE FROM storage_worker_jobs WHERE task_id=? AND user_id=? AND tenant_id=?",
            &crate::params![&old_clock, owner, &tenant],
        )?;
        if deleted != 1 {
            return fail("database_conflict", "Published clock changed during retirement");
        }
    }
    let definition_json = payload["definition_json"].as_str().unwrap_or("");
    let actions_json = payload["actions_json"].as_str().unwrap_or("");
    let mut value = if inactive {
        let Some(current) = &current else { return fail("database_not_found", "Automation definition does not exist") };
        let mut value = current["value"].clone();
        if mode == "retire" {
            value["deleted"] = Value::Bool(true);
        }
        value["definition"] = inactive_definition;
        let digest = sha256_hex(&format!("{definition_json}\n{actions_json}"));
        match value.get_mut("compiled") {
            Some(Value::Object(compiled)) => {
                compiled.insert("digest".into(), json!(digest));
            }
            _ => return Err(PyExc::Type.raise()),
        }
        value
    } else {
        if record_value.is_null() {
            return protocol("Invalid prepared publication binding");
        }
        let mut value = record_value.clone();
        let tenant_id = record_value["grant"]["principal"]["tenant_id"].clone();
        value["schemaVersion"] = json!(1);
        value["id"] = json!(entity_id);
        value["ownerId"] = json!(owner);
        value["tenantId"] = tenant_id;
        value["deleted"] = Value::Bool(false);
        value["grant"]["approvedAt"] = seconds(&json!(now));
        value
    };
    let map = value.as_object_mut().expect("definition value is an object");
    map.shift_remove("lastClockFailure");
    if !last_clock_failure.is_null() {
        map.insert("lastClockFailure".into(), last_clock_failure);
    }
    map.insert("updatedAt".into(), seconds(&json!(now)));
    map.insert(
        "publicationCommand".into(),
        json!({"id": payload["request_id"], "digest": digest, "expectedVersion": version,
            "requestedBy": payload["actor_subject_id"], "requestedAt": seconds(&json!(now))}),
    );
    map.insert("_commitDigest".into(), json!(digest));
    let mut subscription = Value::Null;
    let mut clock = Value::Null;
    let enabled = (|| -> Py<bool> {
        Ok(!truthy(item(&value, "deleted")?) && truthy(item(item(&value, "definition")?, "enabled")?))
    })()
    .raise()?;
    if enabled {
        subscription = subscription_key(&prefix, &value["definition"]).raise()?;
        if py_eq(item(item(&value["definition"], "trigger").raise()?, "kind").raise()?, &json!("schedule")) {
            if due.is_null() || due.as_i64().unwrap_or(0) <= now {
                return protocol("An enabled schedule requires a future first UTC slot");
            }
            clock = json!(clock_job_id(&key, version + 1));
        }
    }
    if !due.is_null() && clock.is_null() {
        return protocol("Only an enabled schedule may enqueue a clock slot");
    }
    value["triggerState"] = json!({"subscriptionKey": subscription, "clockJobId": clock});
    if truthy(&subscription) {
        let pointer = (|| -> Py<Value> {
            Ok(json!({
                "id": item(&value, "id")?, "ownerId": item(&value, "ownerId")?,
                "tenantId": item(&value, "tenantId")?,
                "definitionRevision": version + 1,
                "definitionDigest": item(item(&value, "compiled")?, "digest")?,
                "trigger": item(item(&value, "definition")?, "trigger")?,
                "projectId": get_or_none(item(&value, "definition")?, "projectId")?,
            }))
        })()
        .raise()?;
        record_put_owned(session, namespace("subscription"), subscription.as_str().unwrap_or(""), &pointer, 0)?;
    }
    if let Value::String(clock) = &clock {
        let contract = storage_schema()["$defs"]["clockJob"]["properties"]["contract"]["const"].clone();
        let enqueue = json!({
            "task_id": clock, "user_id": owner, "tenant_id": tenant,
            "task_kind": "automation-clock",
            "payload": {
                "contract": contract, "schemaVersion": 1, "definitionId": value["id"],
                "definitionRevision": version + 1, "definitionDigest": value["compiled"]["digest"],
                "definitionKey": key, "publishedAtMs": now, "firstDueMs": due,
                "principal": value["grant"]["principal"],
            },
            "idempotency_key": clock, "priority": 200, "now_ms": now, "available_at_ms": due,
        });
        worker_job_enqueue_owned(session, enqueue.as_object().expect("enqueue payload"))?;
    }
    let saved = record_put_owned(session, definition_namespace, &key, &value, version)?;
    Ok(with_value(saved, value))
}
