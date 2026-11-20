//! `operations_pkg/_research_workspace.py`: versioned Research Foundry
//! workspaces on `storage_records` with optimistic revision CAS.

use serde_json::{json, Map, Value};

use super::json_text;
use crate::codec::py_eq;
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::ops::paper_artifacts::retain_research_build;
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Session, SqlValue};

const NAMESPACE: &str = "research.workspace.v1";

fn record_key(user_id: i64, paper_hash: &str, lang: &str) -> String {
    format!("{user_id}:{paper_hash}:{lang}")
}

/// `orjson.loads(value_json)`; `None` where orjson raises.
fn decode(value: &SqlValue) -> Option<Value> {
    match value {
        SqlValue::Text(text) => serde_json::from_str(text).ok(),
        SqlValue::Blob(bytes) => serde_json::from_slice(bytes).ok(),
        _ => None,
    }
}

/// `_research_workspace_get`.
pub(crate) fn research_workspace_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 8)?;
    let row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![NAMESPACE, record_key(user_id, &paper_hash, &lang)],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    let Some(workspace) = decode(row.get("value_json")) else {
        return fail("database_integrity", "Stored research workspace is invalid");
    };
    if !workspace.is_object() {
        return fail("database_integrity", "Stored research workspace is not a document");
    }
    Ok(json!({
        "workspace": workspace,
        "revision": row.get("version").as_int().unwrap_or(0),
        "updated_at_ms": row.get("updated_at_ms").as_int().unwrap_or(0),
    }))
}

/// `compilation.get('build') if isinstance(compilation, Mapping) else None`
/// with `compilation = document.get('compilation') or {}`.
fn compilation_of(document: &Map<String, Value>) -> Value {
    match document.get("compilation") {
        Some(value) if truthy(value) => value.clone(),
        _ => Value::Object(Map::new()),
    }
}

fn build_reference(compilation: &Value) -> Value {
    match compilation {
        Value::Object(map) => map.get("build").cloned().unwrap_or(Value::Null),
        _ => Value::Null,
    }
}

/// `_research_workspace_put`.
pub(super) fn research_workspace_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 8)?;
    let expected_revision = integer(payload, "expected_revision", None, Some(0), None)?;
    let updated_at = integer(payload, "updated_at", None, Some(0), None)?;
    let Value::Object(workspace) = get(payload, "workspace") else {
        return fail("database_protocol_error", "Invalid research workspace");
    };

    let key = record_key(user_id, &paper_hash, &lang);
    session.lock_key(NAMESPACE, &key);
    let current = session.fetch_one(
        "SELECT version, value_json FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![NAMESPACE, &key],
    )?;
    let current_revision = current.as_ref().map(|row| row.get("version").as_int().unwrap_or(0)).unwrap_or(0);
    if current_revision != expected_revision {
        return fail("database_conflict", format!("Research workspace revision advanced to {current_revision}"));
    }
    let compilation = compilation_of(workspace);
    let reference = build_reference(&compilation);
    let previous = match &current {
        Some(row) => match decode(row.get("value_json")) {
            Some(value) => value,
            None => return internal("orjson.JSONDecodeError"),
        },
        None => Value::Object(Map::new()),
    };
    let Value::Object(previous) = previous else { return internal("AttributeError: get") };
    let previous_reference = build_reference(&compilation_of(&previous));
    let proof = get(payload, "artifact_proof");
    if !proof.is_null() {
        let Value::Object(compilation) = &compilation else { return internal("AttributeError: get") };
        let null = Value::Null;
        if compilation.get("mode").unwrap_or(&null) != "paper_build"
            || compilation.get("status").unwrap_or(&null) != "passing"
        {
            return fail("database_protocol_error", "Retained compilation must be verified and passing");
        }
        retain_research_build(session, user_id, &reference, proof)?;
    } else if !py_eq(&reference, &previous_reference) {
        return fail("database_protocol_error", "Research PDF reference requires retained artifact evidence");
    }
    let next_revision = current_revision + 1;
    let encoded = json_text(&Value::Object(workspace.clone()))?;
    let updated_at_ms = updated_at * 1000;
    if current.is_none() {
        session.execute(
            "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
            &params![NAMESPACE, &key, encoded, next_revision, updated_at_ms],
        )?;
    } else {
        let changed = session.execute(
            "UPDATE storage_records SET value_json = ?, version = ?, updated_at_ms = ? WHERE namespace = ? AND record_key = ? AND version = ?",
            &params![encoded, next_revision, updated_at_ms, NAMESPACE, &key, current_revision],
        )?;
        if changed != 1 {
            return fail("database_conflict", "Research workspace changed concurrently");
        }
    }
    Ok(json!({"workspace": workspace, "revision": next_revision, "updated_at_ms": updated_at_ms}))
}
