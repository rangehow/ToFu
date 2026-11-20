//! Owner-scoped orchestration definitions with atomic CAS
//! (`operations_pkg/_orchestration_definitions.py`).

use serde_json::{json, Map, Value};

use super::{int_of, json_text, protocol, run_owner, sql_str};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Row, Session};

/// `lib/orchestration/definition_contract_registry.py::MAX_DEFINITION_VERSION`.
const MAX_DEFINITION_VERSION: i64 = 9_007_199_254_740_991;

/// `_definition_document(row)`.
fn definition_document(row: &Row) -> OpResult<Value> {
    let definition = codec::load(row.get("definition_json"))?;
    if !definition.is_object() {
        return fail("database_integrity", "Orchestration definition is not an object");
    }
    let name = row.get("name");
    Ok(json!({
        "id": sql_str(row.require("id")?),
        "name": if name.truthy() { sql_str(name) } else { String::new() },
        "definition": definition,
        "createdAt": int_of(row.get("created_at_ms"))?,
        "updatedAt": int_of(row.get("updated_at_ms"))?,
    }))
}

/// `str(definition.get("name") or "")`.
fn definition_name(definition: &Map<String, Value>) -> String {
    match definition.get("name") {
        Some(value) if truthy(value) => python_str(value),
        _ => String::new(),
    }
}

fn definition_payload(payload: &Payload) -> OpResult<&Map<String, Value>> {
    match payload.get("definition") {
        Some(Value::Object(definition)) => Ok(definition),
        _ => protocol("Invalid orchestration definition"),
    }
}

/// The `{entry, conflict, current_updated_at, deleted}` CAS answer.
fn cas_result(entry: Value, conflict: bool, current_updated_at: Option<i64>, deleted: bool) -> Value {
    json!({
        "entry": entry,
        "conflict": conflict,
        "current_updated_at": current_updated_at,
        "deleted": deleted,
    })
}

pub(super) fn list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (user_id, tenant_id) = run_owner(payload)?;
    let rows = session.fetch_all(
        concat!(
            "SELECT id, user_id, tenant_id, name, definition_json, created_at_ms, updated_at_ms ",
            "FROM orchestration_definitions ",
            "WHERE user_id = ? AND tenant_id = ? ",
            "ORDER BY updated_at_ms DESC, id"
        ),
        &params![user_id, tenant_id],
    )?;
    Ok(Value::Array(rows.iter().map(definition_document).collect::<OpResult<_>>()?))
}

pub(super) fn get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (user_id, tenant_id) = run_owner(payload)?;
    let row = session.fetch_one(
        concat!(
            "SELECT id, user_id, tenant_id, name, definition_json, created_at_ms, updated_at_ms ",
            "FROM orchestration_definitions ",
            "WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![required_text(payload, "orchestration_id", 200)?, user_id, tenant_id],
    )?;
    match row {
        Some(row) => definition_document(&row),
        None => Ok(Value::Null),
    }
}

pub(super) fn create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let orchestration_id = required_text(payload, "orchestration_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let definition = definition_payload(payload)?;
    let now_ms = integer(payload, "now_ms", None, Some(0), None)?;
    session.lock_key("orchestration.definition", &orchestration_id);
    if session
        .fetch_one("SELECT 1 AS present FROM orchestration_definitions WHERE id = ?", &params![&orchestration_id])?
        .is_some()
    {
        return fail("database_conflict", "Orchestration definition id already exists");
    }
    session.execute(
        concat!(
            "INSERT INTO orchestration_definitions(",
            "id, user_id, tenant_id, name, definition_json, ",
            "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)"
        ),
        &params![
            &orchestration_id,
            user_id,
            &tenant_id,
            definition_name(definition),
            json_text(&Value::Object(definition.clone()))?,
            now_ms,
            now_ms,
        ],
    )?;
    get(session, payload)
}

/// The shared CAS read; `Err(answer)` is the early `entry: None` result.
fn current_version(
    session: &Session<'_>,
    orchestration_id: &str,
    user_id: i64,
    tenant_id: &str,
    expected: i64,
) -> OpResult<Result<i64, Value>> {
    let current = session.fetch_one(
        concat!(
            "SELECT updated_at_ms FROM orchestration_definitions ",
            "WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![orchestration_id, user_id, tenant_id],
    )?;
    let Some(current) = current else { return Ok(Err(cas_result(Value::Null, false, None, false))) };
    let current_updated_at = int_of(current.get("updated_at_ms"))?;
    if expected != current_updated_at {
        return Ok(Err(cas_result(Value::Null, true, Some(current_updated_at), false)));
    }
    Ok(Ok(current_updated_at))
}

pub(super) fn update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let orchestration_id = required_text(payload, "orchestration_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let definition = definition_payload(payload)?;
    let expected = integer(payload, "expected_updated_at", None, Some(0), Some(MAX_DEFINITION_VERSION))?;
    session.lock_key("orchestration.definition", &orchestration_id);
    let current_updated_at = match current_version(session, &orchestration_id, user_id, &tenant_id, expected)? {
        Ok(version) => version,
        Err(answer) => return Ok(answer),
    };
    let updated_at = integer(payload, "now_ms", None, Some(0), None)?.max(current_updated_at + 1);
    session.execute(
        concat!(
            "UPDATE orchestration_definitions SET name = ?, definition_json = ?, ",
            "updated_at_ms = ? WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![
            definition_name(definition),
            json_text(&Value::Object(definition.clone()))?,
            updated_at,
            &orchestration_id,
            user_id,
            &tenant_id,
        ],
    )?;
    let entry = get(session, payload)?;
    Ok(cas_result(entry, false, Some(updated_at), false))
}

pub(super) fn delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let orchestration_id = required_text(payload, "orchestration_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let expected = integer(payload, "expected_updated_at", None, Some(0), Some(MAX_DEFINITION_VERSION))?;
    session.lock_key("orchestration.definition", &orchestration_id);
    let current_updated_at = match current_version(session, &orchestration_id, user_id, &tenant_id, expected)? {
        Ok(version) => version,
        Err(answer) => return Ok(answer),
    };
    session.execute(
        concat!("DELETE FROM orchestration_definitions ", "WHERE id = ? AND user_id = ? AND tenant_id = ?"),
        &params![&orchestration_id, user_id, &tenant_id],
    )?;
    Ok(cas_result(Value::Null, false, Some(current_updated_at), true))
}
