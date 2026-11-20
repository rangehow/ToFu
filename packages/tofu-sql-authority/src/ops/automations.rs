//! `operation_domains/automations.py`.
//!
//! `automation.project.update` (`_automation_projects.py`,
//! `lib/automations/projects.py`), `automation.skills.source.update`
//! (`_automation_skill_sources.py`, `lib/skills/automation/source_records.py`),
//! `automation.run.admit|advance|cancel` (`runs`, `_automations.py`),
//! `automation.run.approve` (`approval`, `_automation_approval.py`),
//! `automation.definition.update` (`definitions`,
//! `_automation_definitions.py`) and `automation.event.settle` (`events`,
//! `_automation_events.py`). Storage inputs are checked by the Draft 2020-12
//! subset in `schema` (`contract.py::storage_input_validator`).

mod approval;
mod definitions;
mod events;
mod py;
mod pyjson;
mod runs;
mod schema;

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::{register as add, OperationSpec, Registry};
use crate::codec::{self, py_eq};
use crate::error::{fail, internal, OpResult, StorageError};
use crate::params;
use crate::payload::{char_len, expected_version, required_text, Payload};
use crate::session::Session;

pub fn register(registry: &mut Registry) {
    add(registry, "automation.skills.source.update", OperationSpec::command(false, skill_source_update));
    add(registry, "automation.project.update", OperationSpec::command(false, project_update));
    add(registry, "automation.run.approve", OperationSpec::command(false, approval::run_approve));
    add(registry, "automation.event.settle", OperationSpec::command(false, events::event_settle));
    add(registry, "automation.definition.update", OperationSpec::command(false, definitions::definition_update));
    add(registry, "automation.run.admit", OperationSpec::command(false, runs::run_admit));
    add(registry, "automation.run.advance", OperationSpec::command(false, runs::run_advance));
    add(registry, "automation.run.cancel", OperationSpec::command(false, runs::run_cancel));
}

const STORAGE_SCHEMA: &str = include_str!("../../../../contracts/automation_storage_v1.schema.json");
const DEFINITION_SCHEMA: &str = include_str!("../../../../contracts/automation_v1.schema.json");

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

fn clock_skew_ms() -> i64 {
    storage_schema()["x-limits"]["clockSkewMs"].as_i64().expect("clockSkewMs")
}

fn namespace(name: &str) -> &'static str {
    storage_schema()["x-namespaces"][name].as_str().expect("automation namespace")
}

fn wall_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or(0)
}

fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

/// `re.fullmatch(pattern, value)` for the schema's ECMA-style patterns.
fn full_match(pattern: &str, value: &str) -> bool {
    let translated = pattern.replace(r"(?![\s\S])", "$");
    Regex::new(&format!("^(?:{translated})$")).is_ok_and(|re| re.is_match(value))
}

/// `owner:sha256(tenant)[:32]:` (shared identity prefix).
fn owner_prefix(owner: i64, tenant: &str) -> String {
    format!("{owner}:{}:", &sha256_hex(tenant)[..32])
}

// private port of lib/storage_authority_sql/operations_pkg/_records.py::_record_get; shared owner: core
fn record_get(session: &Session<'_>, record_namespace: &str, key: &str) -> OpResult<Option<Map<String, Value>>> {
    let row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![record_namespace, key],
    )?;
    let Some(row) = row else { return Ok(None) };
    let mut record = Map::new();
    record.insert("value".into(), codec::load(row.require("value_json")?)?);
    record.insert("version".into(), Value::from(row.int("version")?));
    record.insert("updated_at_ms".into(), Value::from(row.int("updated_at_ms")?));
    Ok(Some(record))
}

// private port of lib/storage_authority_sql/operations_pkg/_records.py::_record_put_owned; shared owner: core
fn record_put_owned(
    session: &Session<'_>,
    record_namespace: &str,
    key: &str,
    value: &Value,
    expected: i64,
) -> OpResult<Value> {
    let mut payload = Payload::new();
    payload.insert("namespace".into(), Value::String(record_namespace.to_owned()));
    payload.insert("key".into(), Value::String(key.to_owned()));
    payload.insert("expected_version".into(), Value::from(expected));
    let record_namespace = required_text(&payload, "namespace", 128)?;
    let key = required_text(&payload, "key", 512)?;
    let encoded = codec::dump_param(value)?;
    let now = wall_ms();
    session.lock_key("record", &format!("{record_namespace}:{key}"));
    let current = session.fetch_one(
        "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![&record_namespace, &key],
    )?;
    let expected = expected_version(&payload)?;
    let actual = match &current {
        Some(row) => row.int("version")?,
        None => 0,
    };
    if expected.is_some_and(|expected| expected != actual) {
        return fail("database_conflict", "Storage record version conflict");
    }
    let version = actual + 1;
    session.execute(
        "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) \
         VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET \
         value_json = excluded.value_json, version = excluded.version, \
         updated_at_ms = excluded.updated_at_ms",
        &params![&record_namespace, &key, encoded, version, now],
    )?;
    Ok(json!({"key": key, "version": version, "updated_at_ms": now}))
}

/// `lib/automations/projects.py::project_payload`; `None` is its
/// `ValueError`/`TypeError`/`KeyError`.
fn project_payload(payload: &Payload) -> Option<String> {
    let properties = storage_schema()["$defs"]["projectUpdate"]["properties"].as_object()?;
    if payload.len() != properties.len() || !payload.keys().all(|key| properties.contains_key(key)) {
        return None;
    }
    for (name, schema) in properties {
        let value = &payload[name];
        let valid = match schema["type"].as_str() {
            Some("integer") => value.as_i64().is_some_and(|value| {
                schema["minimum"].as_i64().is_some_and(|minimum| minimum <= value)
                    && schema["maximum"].as_i64().is_some_and(|maximum| value <= maximum)
            }),
            Some("boolean") => value.is_boolean(),
            _ => value.as_str().is_some_and(|text| {
                let length = char_len(text) as i64;
                let valid = schema["minLength"].as_i64().unwrap_or(0) <= length
                    && length <= schema["maxLength"].as_i64().unwrap_or(128);
                valid && schema["pattern"].as_str().is_none_or(|pattern| full_match(pattern, text))
            }),
        };
        if !valid {
            return None;
        }
    }
    let root = payload["root"].as_str()?;
    if root.contains('\0') {
        return None;
    }
    let prefix = owner_prefix(payload["user_id"].as_i64()?, payload["tenant_id"].as_str()?);
    let identity = format!("project_{}", sha256_hex(root));
    if payload["project_key"].as_str()? != format!("{prefix}{identity}") {
        return None;
    }
    Some(identity)
}

/// `projects.project_digest`: `canonical` without `now_ms`.
fn project_digest(payload: &Payload) -> String {
    let mut value = payload.clone();
    value.shift_remove("now_ms");
    sha256_hex(&pyjson::dumps(&Value::Object(value), true))
}

fn tenant_or_none(payload: &Payload) -> Value {
    match payload.get("tenant_id") {
        Some(Value::String(tenant)) if !tenant.is_empty() => Value::String(tenant.clone()),
        _ => Value::Null,
    }
}

/// `projects.project_record`; `Err(None)` is a caught identity error.
fn project_record(payload: &Payload, current: Option<&Map<String, Value>>) -> Result<Value, Option<String>> {
    let identity = project_payload(payload).ok_or(None)?;
    let contract = storage_schema()["x-project"]["contract"].clone();
    let created = match current {
        Some(current) => {
            let Value::Object(old) = &current["value"] else {
                return Err(Some("AttributeError: get".into()));
            };
            let get = |key: &str| old.get(key).cloned().unwrap_or(Value::Null);
            let owner_ok = matches!(old.get("ownerId"), Some(Value::Number(n)) if n.is_i64() || n.is_u64());
            if !py_eq(&get("contract"), &contract)
                || !py_eq(&get("id"), &Value::String(identity.clone()))
                || !owner_ok
                || !py_eq(&get("ownerId"), &payload["user_id"])
                || !py_eq(&get("tenantId"), &tenant_or_none(payload))
                || !py_eq(&get("root"), &payload["root"])
                || !matches!(old.get("createdAt"), Some(Value::Number(n)) if n.is_i64() || n.is_u64())
            {
                return Err(None);
            }
            get("createdAt")
        }
        None => payload["now_ms"].clone(),
    };
    Ok(json!({
        "contract": contract,
        "id": identity,
        "ownerId": payload["user_id"],
        "tenantId": tenant_or_none(payload),
        "root": payload["root"],
        "label": payload["label"],
        "active": payload["active"],
        "createdAt": created,
        "updatedAt": payload["now_ms"],
        "command": {
            "id": payload["request_id"],
            "digest": project_digest(payload),
            "actorSubjectId": payload["actor_subject_id"],
        },
    }))
}

/// `_automation_project_update`.
fn project_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    if project_payload(payload).is_none() {
        return fail("database_protocol_error", "Invalid project registration");
    }
    let now = payload["now_ms"].as_i64().unwrap_or(0);
    if now - wall_ms() > clock_skew_ms() {
        return fail("database_protocol_error", "Project clock is too far ahead");
    }
    let key = payload["project_key"].as_str().unwrap_or("").to_owned();
    let target = namespace("target");
    session.lock_key("automation_project", &key);
    let current = record_get(session, target, &key)?;
    let record = match project_record(payload, current.as_ref()) {
        Ok(record) => record,
        Err(None) => return fail("database_integrity", "Project identity differs"),
        Err(Some(reason)) => return internal(reason),
    };
    let version = payload["expected_version"].as_i64().unwrap_or(0);
    let digest = project_digest(payload);
    if let Some(current) = &current {
        let command = match current["value"].get("command") {
            None => Map::new(),
            Some(Value::Object(command)) => command.clone(),
            Some(_) => return internal("AttributeError: get"),
        };
        let command_id = command.get("id").cloned().unwrap_or(Value::Null);
        let command_digest = command.get("digest").cloned().unwrap_or(Value::Null);
        let same_request = py_eq(&command_id, &payload["request_id"]);
        let same_digest = py_eq(&command_digest, &Value::String(digest.clone()));
        if same_request && !same_digest {
            return fail("database_conflict", "Project registration request changed");
        }
        if py_eq(&current["version"], &Value::from(version + 1)) && same_request && same_digest {
            return Ok(Value::Object(current.clone()));
        }
    }
    record_put_owned(session, target, &key, &record, version)?;
    Ok(record_get(session, target, &key)?.map(Value::Object).unwrap_or(Value::Null))
}

fn skill_source() -> &'static Value {
    &storage_schema()["x-skillSource"]
}

fn source_properties() -> &'static Map<String, Value> {
    storage_schema()["$defs"]["skillSourceUpdate"]["properties"].as_object().expect("skill source properties")
}

/// `source_records._field`.
fn field(schema: &Value, value: &Value) -> bool {
    if let Some(Value::Array(parts)) = schema.get("anyOf") {
        return parts.iter().any(|part| field(part, value));
    }
    if let Some(Value::Array(options)) = schema.get("enum") {
        return options.iter().any(|option| py_eq(option, value));
    }
    match schema["type"].as_str() {
        Some("null") => value.is_null(),
        Some("boolean") => value.is_boolean(),
        Some("integer") => value.as_i64().is_some_and(|value| {
            schema.get("minimum").and_then(Value::as_i64).unwrap_or(0) <= value
                && value <= schema.get("maximum").and_then(Value::as_i64).unwrap_or(safe_integer_max())
        }),
        Some("string") => value.as_str().is_some_and(|text| {
            let length = char_len(text) as i64;
            schema.get("minLength").and_then(Value::as_i64).unwrap_or(0) <= length
                && length <= schema.get("maxLength").and_then(Value::as_i64).unwrap_or(safe_integer_max())
                && schema["pattern"].as_str().is_none_or(|pattern| full_match(pattern, text))
        }),
        _ => false,
    }
}

fn tenant_text(value: &Value) -> String {
    value.as_str().unwrap_or("").to_owned()
}

/// `source_records.credential_scope`.
fn credential_scope(owner: &Value, tenant: &Value) -> String {
    let domain = skill_source()["credentialScopeDomain"].as_str().expect("scope domain");
    let owner = crate::json::python_str(owner);
    sha256_hex(&format!("{domain}\n{owner}\n{}", tenant_text(tenant)))[..24].to_owned()
}

/// `source_records.source_payload`; `false` is its `ValueError`.
fn source_payload(payload: &Payload) -> bool {
    let properties = source_properties();
    if payload.len() != properties.len() || !payload.keys().all(|key| properties.contains_key(key)) {
        return false;
    }
    if properties.iter().any(|(name, schema)| !field(schema, &payload[name])) {
        return false;
    }
    let source_key = format!(
        "{}:{}:{}",
        payload["user_id"],
        &sha256_hex(&tenant_text(&payload["tenant_id"]))[..32],
        skill_source()["id"].as_str().expect("source id")
    );
    if payload["source_key"].as_str() != Some(source_key.as_str()) {
        return false;
    }
    let (reference, seal) = (&payload["credential_ref"], &payload["credential_seal"]);
    if reference.is_null() != seal.is_null() || (payload["active"] == Value::Bool(true) && reference.is_null()) {
        return false;
    }
    if let Some(reference) = reference.as_str() {
        if !reference.starts_with(&format!("{}_", credential_scope(&payload["user_id"], &payload["tenant_id"]))) {
            return false;
        }
    }
    true
}

/// `source_records.source_digest`.
fn source_digest(payload: &Payload) -> String {
    let mut value = payload.clone();
    value.shift_remove("credential_ref");
    value.shift_remove("now_ms");
    sha256_hex(&pyjson::dumps(&Value::Object(value), true))
}

/// `source_records.validate_source_record`.
fn validate_source_record(value: &Value) -> bool {
    const KEYS: [&str; 11] = [
        "contract",
        "id",
        "ownerId",
        "tenantId",
        "environment",
        "active",
        "credentialRef",
        "credentialSeal",
        "createdAt",
        "updatedAt",
        "command",
    ];
    let Value::Object(map) = value else { return false };
    if map.len() != KEYS.len() || !KEYS.iter().all(|key| map.contains_key(*key)) {
        return false;
    }
    let bounded = |item: &Value| item.as_i64().is_some_and(|v| 1 <= v && v <= safe_integer_max());
    let environments = source_properties()["environment"]["enum"].as_array().expect("environments");
    let tenant_ok = match &map["tenantId"] {
        Value::Null => true,
        Value::String(tenant) => !tenant.is_empty() && char_len(tenant) <= 256,
        _ => false,
    };
    if !py_eq(&map["contract"], &skill_source()["contract"])
        || !py_eq(&map["id"], &skill_source()["id"])
        || !bounded(&map["ownerId"])
        || !tenant_ok
        || !map["environment"].is_string()
        || !environments.iter().any(|environment| py_eq(environment, &map["environment"]))
        || !map["active"].is_boolean()
        || !bounded(&map["createdAt"])
        || !bounded(&map["updatedAt"])
    {
        return false;
    }
    let properties = source_properties();
    let (reference, seal) = (&map["credentialRef"], &map["credentialSeal"]);
    if !field(&properties["credential_ref"], reference)
        || !field(&properties["credential_seal"], seal)
        || reference.is_null() != seal.is_null()
        || (map["active"] == Value::Bool(true) && reference.is_null())
        || reference.as_str().is_some_and(|reference| {
            !reference.starts_with(&format!("{}_", credential_scope(&map["ownerId"], &map["tenantId"])))
        })
    {
        return false;
    }
    let Value::Object(command) = &map["command"] else { return false };
    command.len() == 3
        && ["id", "digest", "actorSubjectId"].iter().all(|key| command.contains_key(*key))
        && field(&properties["request_id"], &command["id"])
        && field(&properties["actor_subject_id"], &command["actorSubjectId"])
        && command["digest"].as_str().is_some_and(|digest| full_match("[0-9a-f]{64}", digest))
}

/// `source_records.source_record`; `None` is a caught identity error.
fn source_record(payload: &Payload, current: Option<&Map<String, Value>>) -> Option<Value> {
    let created = match current {
        Some(current) => {
            let value = &current["value"];
            if !validate_source_record(value) {
                return None;
            }
            if !py_eq(&value["ownerId"], &payload["user_id"]) || !py_eq(&value["tenantId"], &tenant_or_none(payload)) {
                return None;
            }
            value["createdAt"].clone()
        }
        None => payload["now_ms"].clone(),
    };
    Some(json!({
        "contract": skill_source()["contract"],
        "id": skill_source()["id"],
        "ownerId": payload["user_id"],
        "tenantId": tenant_or_none(payload),
        "environment": payload["environment"],
        "active": payload["active"],
        "credentialRef": payload["credential_ref"],
        "credentialSeal": payload["credential_seal"],
        "createdAt": created,
        "updatedAt": payload["now_ms"],
        "command": {
            "id": payload["request_id"],
            "digest": source_digest(payload),
            "actorSubjectId": payload["actor_subject_id"],
        },
    }))
}

/// `_automation_skill_source_update`.
fn skill_source_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    if !source_payload(payload) {
        return Err(StorageError::protocol("Invalid owned skill source setting").into());
    }
    let now = payload["now_ms"].as_i64().unwrap_or(0);
    if now - wall_ms() > clock_skew_ms() {
        return fail("database_protocol_error", "Source clock is too far ahead");
    }
    let key = payload["source_key"].as_str().unwrap_or("").to_owned();
    let source_namespace = namespace("skillSource");
    session.lock_key("automation_skill_source", &key);
    let current = record_get(session, source_namespace, &key)?;
    let Some(record) = source_record(payload, current.as_ref()) else {
        return fail("database_integrity", "Stored skill source identity differs");
    };
    let version = payload["expected_version"].as_i64().unwrap_or(0);
    let digest = source_digest(payload);
    if let Some(current) = &current {
        let command = &current["value"]["command"];
        let same_request = py_eq(&command["id"], &payload["request_id"]);
        let same_digest = py_eq(&command["digest"], &Value::String(digest.clone()));
        if same_request && !same_digest {
            return fail("database_conflict", "Skill source registration request changed");
        }
        if py_eq(&current["version"], &Value::from(version + 1)) && same_request && same_digest {
            return Ok(Value::Object(current.clone()));
        }
    }
    record_put_owned(session, source_namespace, &key, &record, version)?;
    Ok(record_get(session, source_namespace, &key)?.map(Value::Object).unwrap_or(Value::Null))
}
