//! `operation_domains/model_routing.py`: owner-scoped model-routing v2
//! aggregates and secrets (`operations_pkg/_model_routing.py`).

use std::collections::HashSet;

use serde_json::{json, Value};

use super::{register as add, OperationSpec, Registry};
use crate::codec::{self, py_eq};
use crate::error::{fail, OpResult};
use crate::json::{float_value, truthy};
use crate::params;
use crate::payload::{char_len, get, integer, number, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "model_routing.get", OperationSpec::query(model_routing_get));
    add(registry, "model_routing.commit", OperationSpec::command(true, model_routing_commit));
    add(registry, "model_routing.migration_receipt", OperationSpec::query(model_routing_migration_receipt));
    add(
        registry,
        "model_routing.migration_receipt.put",
        OperationSpec::command(true, model_routing_migration_receipt_put),
    );
    add(registry, "model_routing.secret.put", OperationSpec::command(true, model_routing_secret_put));
    add(registry, "model_routing.secret.get", OperationSpec::query(model_routing_secret_get));
    add(registry, "model_routing.secret.list", OperationSpec::query(model_routing_secret_list));
    add(registry, "model_routing.secret.delete", OperationSpec::command(true, model_routing_secret_delete));
    add(registry, "model_routing.secret.prune", OperationSpec::command(false, model_routing_secret_prune));
}

const MAX_CIPHERTEXT_LENGTH: usize = 32768;
const MAX_TIMESTAMP: f64 = 32_503_680_000.0;
const CONTRACT_VERSION: &str = "tofu.model-routing/v2";
const PRUNE_PAGE: usize = 256;

/// Python `str.isspace()` for `str.strip()`.
fn py_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

fn py_strip(text: &str) -> String {
    text.trim_matches(py_space).to_owned()
}

fn protocol<T>(message: impl Into<String>) -> OpResult<T> {
    fail("database_protocol_error", message)
}

/// `_json_text(value)` (`_runs.py`): `_dump(value).decode()` bound as TEXT.
fn json_text(value: &Value) -> OpResult<String> {
    Ok(String::from_utf8(codec::dump(value)?).expect("orjson output is UTF-8"))
}

/// Python `str(value)` of a column.
fn py_str_column(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(value) => value.to_string(),
        SqlValue::Real(value) => crate::json::python_float_repr(*value),
        _ => value.text_or_empty(),
    }
}

fn row_float(row: &Row, column: &str) -> OpResult<f64> {
    match row.require(column)?.as_f64() {
        Some(value) => Ok(value),
        None => crate::error::internal(format!("float() of {column}")),
    }
}

fn optional_text(payload: &Payload, key: &str, maximum: usize) -> OpResult<String> {
    match payload.get(key) {
        None => Ok(String::new()),
        Some(Value::String(text)) if char_len(text) <= maximum => Ok(py_strip(text)),
        Some(_) => protocol(format!("Invalid {key} in model-routing request")),
    }
}

fn owner_boundary(payload: &Payload) -> OpResult<(i64, String)> {
    Ok((integer(payload, "owner_user_id", None, Some(1), None)?, optional_text(payload, "tenant_id", 256)?))
}

fn lock_owner(session: &Session<'_>, owner_user_id: i64, tenant_id: &str) {
    session.lock_key("model_routing.owner", &format!("{tenant_id}:{owner_user_id}"));
}

fn lock_secret(session: &Session<'_>, owner_user_id: i64, tenant_id: &str, secret_reference: &str) {
    session.lock_key("model_routing.secrets", &format!("{tenant_id}:{owner_user_id}:{secret_reference}"));
}

fn document_payload<'p>(payload: &'p Payload, key: &str) -> OpResult<(&'p Value, String)> {
    let value = get(payload, key);
    if !value.is_object() {
        return protocol(format!("Invalid {key} in model-routing request"));
    }
    Ok((value, json_text(value)?))
}

fn decoded_object(value: &SqlValue, field: &str) -> OpResult<Option<Value>> {
    if value.is_null() {
        return Ok(None);
    }
    let decoded = codec::load(value)?;
    if !decoded.is_object() {
        return fail("database_integrity", format!("Stored model-routing {field} is invalid"));
    }
    Ok(Some(decoded))
}

fn authority_document(row: &Row) -> OpResult<Value> {
    let Some(document) = decoded_object(row.require("document_json")?, "document")? else {
        return fail("database_integrity", "Stored model-routing document is empty");
    };
    Ok(json!({
        "owner_user_id": row.int("owner_user_id")?,
        "tenant_id": row.require("tenant_id")?.text_or_empty(),
        "revision": row.int("revision")?,
        "document": document,
        "updated_at": float_value(row_float(row, "updated_at")?),
    }))
}

fn current_secret_references(session: &Session<'_>, owner_user_id: i64, tenant_id: &str) -> OpResult<HashSet<String>> {
    let row = session.fetch_one(
        "SELECT document_json FROM storage_model_routing_authorities \
WHERE owner_user_id = ? AND tenant_id = ?",
        &params![owner_user_id, tenant_id],
    )?;
    let Some(row) = row else { return Ok(HashSet::new()) };
    let Some(document) = decoded_object(row.require("document_json")?, "document")? else {
        return fail("database_integrity", "Stored model-routing document is empty");
    };
    let empty = Value::Array(Vec::new());
    let credentials = document.get("credentials").unwrap_or(&empty);
    let Value::Array(credentials) = credentials else {
        return fail("database_integrity", "Stored model-routing credentials are invalid");
    };
    let mut references = HashSet::new();
    for credential in credentials {
        let Value::Object(credential) = credential else {
            return fail("database_integrity", "Stored model-routing credential is invalid");
        };
        match credential.get("secret_reference") {
            None | Some(Value::Null) => continue,
            Some(Value::String(reference)) => {
                let reference = py_strip(reference);
                if !reference.is_empty() {
                    references.insert(reference);
                }
            }
            Some(_) => return fail("database_integrity", "Stored model-routing secret reference is invalid"),
        }
    }
    Ok(references)
}

fn model_routing_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let row = session.fetch_one(
        "SELECT owner_user_id, tenant_id, revision, document_json, updated_at \
FROM storage_model_routing_authorities \
WHERE owner_user_id = ? AND tenant_id = ?",
        &params![owner_user_id, tenant_id],
    )?;
    match row {
        Some(row) => authority_document(&row),
        None => Ok(Value::Null),
    }
}

/// `document.get(key) != expected` with Python equality.
fn differs(document: &Value, key: &str, expected: &Value) -> bool {
    !py_eq(document.get(key).unwrap_or(&Value::Null), expected)
}

fn model_routing_commit(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let expected_revision = integer(payload, "expected_revision", None, Some(0), None)?;
    let (document, encoded) = document_payload(payload, "document")?;
    let next_revision_value = match expected_revision.checked_add(1) {
        Some(next) => Value::from(next),
        None => Value::from(expected_revision as u64 + 1),
    };
    if differs(document, "contract_version", &Value::from(CONTRACT_VERSION)) {
        return protocol("Model-routing contract_version is invalid");
    }
    if differs(document, "revision", &next_revision_value) {
        return protocol("Model-routing document revision must equal expected_revision + 1");
    }
    let updated_at = number(payload, "updated_at", 0.0, MAX_TIMESTAMP)?;
    let migration_receipt = get(payload, "migration_receipt");
    if !migration_receipt.is_null() && !migration_receipt.is_object() {
        return protocol("migration_receipt must be an object");
    }

    lock_owner(session, owner_user_id, &tenant_id);
    let current = session.fetch_one(
        "SELECT revision, document_json FROM storage_model_routing_authorities \
WHERE owner_user_id = ? AND tenant_id = ?",
        &params![owner_user_id, &tenant_id],
    )?;
    let current_revision = match &current {
        Some(row) => row.int("revision")?,
        None => 0,
    };
    if current_revision != expected_revision {
        return fail(
            "database_conflict",
            format!("Model-routing revision changed from {expected_revision} to {current_revision}"),
        );
    }
    let Some(next_revision) = next_revision_value.as_i64() else {
        return crate::error::internal("OverflowError: Python int too large to convert to SQLite INTEGER");
    };

    match &current {
        None => {
            let receipt = if migration_receipt.is_null() { None } else { Some(json_text(migration_receipt)?) };
            session.execute(
                "INSERT INTO storage_model_routing_authorities(\
owner_user_id, tenant_id, revision, document_json, backup_json, \
migration_receipt_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                &params![owner_user_id, &tenant_id, next_revision, encoded, SqlValue::Null, receipt, updated_at],
            )?;
        }
        Some(_) if migration_receipt.is_null() => {
            session.execute(
                "UPDATE storage_model_routing_authorities SET revision = ?, \
document_json = ?, updated_at = ? \
WHERE owner_user_id = ? AND tenant_id = ?",
                &params![next_revision, encoded, updated_at, owner_user_id, &tenant_id],
            )?;
        }
        Some(current) => {
            session.execute(
                "UPDATE storage_model_routing_authorities SET revision = ?, \
document_json = ?, backup_json = ?, migration_receipt_json = ?, \
updated_at = ? WHERE owner_user_id = ? AND tenant_id = ?",
                &params![
                    next_revision,
                    encoded,
                    current.require("document_json")?,
                    json_text(migration_receipt)?,
                    updated_at,
                    owner_user_id,
                    &tenant_id,
                ],
            )?;
        }
    }
    Ok(json!({
        "owner_user_id": owner_user_id,
        "tenant_id": tenant_id,
        "revision": next_revision,
        "updated_at": float_value(updated_at),
    }))
}

fn model_routing_migration_receipt(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let row = session.fetch_one(
        "SELECT revision, backup_json, migration_receipt_json, updated_at \
FROM storage_model_routing_authorities \
WHERE owner_user_id = ? AND tenant_id = ?",
        &params![owner_user_id, tenant_id],
    )?;
    let row = match row {
        Some(row) if !row.require("migration_receipt_json")?.is_null() => row,
        _ => return Ok(Value::Null),
    };
    Ok(json!({
        "revision": row.int("revision")?,
        "backup": decoded_object(row.require("backup_json")?, "migration backup")?,
        "receipt": decoded_object(row.require("migration_receipt_json")?, "migration receipt")?,
        "updated_at": float_value(row_float(&row, "updated_at")?),
    }))
}

fn model_routing_migration_receipt_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let receipt = get(payload, "migration_receipt");
    if !receipt.is_object() {
        return protocol("migration_receipt must be an object");
    }
    let encoded_receipt = json_text(receipt)?;
    let updated_at = number(payload, "updated_at", 0.0, MAX_TIMESTAMP)?;
    lock_owner(session, owner_user_id, &tenant_id);
    let current = session.fetch_one(
        "SELECT revision FROM storage_model_routing_authorities \
WHERE owner_user_id = ? AND tenant_id = ?",
        &params![owner_user_id, &tenant_id],
    )?;
    let revision = match &current {
        None => {
            let (document, encoded_document) = document_payload(payload, "document")?;
            if differs(document, "contract_version", &Value::from(CONTRACT_VERSION))
                || differs(document, "revision", &Value::from(0))
            {
                return protocol("initial model-routing receipt document must be empty revision 0");
            }
            session.execute(
                "INSERT INTO storage_model_routing_authorities(\
owner_user_id, tenant_id, revision, document_json, backup_json, \
migration_receipt_json, updated_at) VALUES (?, ?, 0, ?, NULL, ?, ?)",
                &params![owner_user_id, &tenant_id, encoded_document, encoded_receipt, updated_at],
            )?;
            0
        }
        Some(current) => {
            session.execute(
                "UPDATE storage_model_routing_authorities SET \
migration_receipt_json = ?, updated_at = ? \
WHERE owner_user_id = ? AND tenant_id = ?",
                &params![encoded_receipt, updated_at, owner_user_id, &tenant_id],
            )?;
            current.int("revision")?
        }
    };
    let status = receipt.get("status").unwrap_or(&Value::Null);
    let status = if truthy(status) { crate::json::python_str(status) } else { String::new() };
    Ok(json!({
        "owner_user_id": owner_user_id,
        "tenant_id": tenant_id,
        "revision": revision,
        "status": status,
        "updated_at": float_value(updated_at),
    }))
}

fn model_routing_secret_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let secret_reference = required_text(payload, "secret_reference", 256)?;
    let ciphertext = required_text(payload, "ciphertext", MAX_CIPHERTEXT_LENGTH)?;
    let key_hint = optional_text(payload, "key_hint", 64)?;
    let updated_at = number(payload, "updated_at", 0.0, MAX_TIMESTAMP)?;
    lock_secret(session, owner_user_id, &tenant_id, &secret_reference);
    let current = session.fetch_one(
        "SELECT created_at FROM storage_model_routing_secrets WHERE \
owner_user_id = ? AND tenant_id = ? AND secret_reference = ?",
        &params![owner_user_id, &tenant_id, &secret_reference],
    )?;
    if current.is_none() {
        session.execute(
            "INSERT INTO storage_model_routing_secrets(\
owner_user_id, tenant_id, secret_reference, ciphertext, key_hint, \
created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            &params![owner_user_id, &tenant_id, &secret_reference, ciphertext, &key_hint, updated_at, updated_at],
        )?;
    } else {
        session.execute(
            "UPDATE storage_model_routing_secrets SET ciphertext = ?, \
key_hint = ?, updated_at = ? WHERE owner_user_id = ? \
AND tenant_id = ? AND secret_reference = ?",
            &params![ciphertext, &key_hint, updated_at, owner_user_id, &tenant_id, &secret_reference],
        )?;
    }
    Ok(json!({
        "secret_reference": secret_reference,
        "key_hint": key_hint,
        "updated_at": float_value(updated_at),
    }))
}

fn model_routing_secret_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let secret_reference = required_text(payload, "secret_reference", 256)?;
    let row = session.fetch_one(
        "SELECT secret_reference, ciphertext, key_hint, created_at, updated_at \
FROM storage_model_routing_secrets WHERE owner_user_id = ? \
AND tenant_id = ? AND secret_reference = ?",
        &params![owner_user_id, tenant_id, secret_reference],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    Ok(json!({
        "secret_reference": py_str_column(row.require("secret_reference")?),
        "ciphertext": py_str_column(row.require("ciphertext")?),
        "key_hint": row.require("key_hint")?.text_or_empty(),
        "created_at": float_value(row_float(&row, "created_at")?),
        "updated_at": float_value(row_float(&row, "updated_at")?),
    }))
}

fn model_routing_secret_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let rows = session.fetch_all(
        "SELECT secret_reference, key_hint, created_at, updated_at \
FROM storage_model_routing_secrets WHERE owner_user_id = ? \
AND tenant_id = ? ORDER BY secret_reference",
        &params![owner_user_id, tenant_id],
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in &rows {
        result.push(json!({
            "secret_reference": py_str_column(row.require("secret_reference")?),
            "key_hint": row.require("key_hint")?.text_or_empty(),
            "created_at": float_value(row_float(row, "created_at")?),
            "updated_at": float_value(row_float(row, "updated_at")?),
        }));
    }
    Ok(Value::Array(result))
}

fn model_routing_secret_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let secret_reference = required_text(payload, "secret_reference", 256)?;
    lock_owner(session, owner_user_id, &tenant_id);
    if current_secret_references(session, owner_user_id, &tenant_id)?.contains(&secret_reference) {
        return Ok(json!({"deleted": false, "secret_reference": secret_reference}));
    }
    lock_secret(session, owner_user_id, &tenant_id, &secret_reference);
    let deleted = session.execute(
        "DELETE FROM storage_model_routing_secrets WHERE \
owner_user_id = ? AND tenant_id = ? AND secret_reference = ?",
        &params![owner_user_id, &tenant_id, &secret_reference],
    )?;
    Ok(json!({"deleted": deleted > 0, "secret_reference": secret_reference}))
}

fn model_routing_secret_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let Value::Array(active_raw) = get(payload, "active_secret_references") else {
        return protocol("active_secret_references must be an array");
    };
    let mut active: HashSet<String> =
        active_raw.iter().filter_map(Value::as_str).map(py_strip).filter(|value| !value.is_empty()).collect();
    let before = number(payload, "updated_before", 0.0, MAX_TIMESTAMP)?;
    lock_owner(session, owner_user_id, &tenant_id);
    active.extend(current_secret_references(session, owner_user_id, &tenant_id)?);
    let mut removed: Vec<String> = Vec::new();
    let mut cursor: Option<(f64, String)> = None;
    while removed.len() < PRUNE_PAGE {
        let mut cursor_sql = "";
        let mut parameters = params![owner_user_id, &tenant_id, before].to_vec();
        if let Some((updated_at, reference)) = &cursor {
            cursor_sql = " AND (updated_at > ? OR (updated_at = ? AND secret_reference > ?))";
            parameters.extend(params![*updated_at, *updated_at, reference]);
        }
        let rows = session.fetch_all(
            &format!(
                "SELECT secret_reference, updated_at FROM storage_model_routing_secrets WHERE \
owner_user_id = ? AND tenant_id = ? AND updated_at < ?{cursor_sql} ORDER BY updated_at, secret_reference LIMIT 256"
            ),
            &parameters,
        )?;
        if rows.is_empty() {
            break;
        }
        for row in &rows {
            let secret_reference = py_str_column(row.require("secret_reference")?);
            cursor = Some((row_float(row, "updated_at")?, secret_reference.clone()));
            if active.contains(&secret_reference) {
                continue;
            }
            lock_secret(session, owner_user_id, &tenant_id, &secret_reference);
            let deleted = session.execute(
                "DELETE FROM storage_model_routing_secrets WHERE owner_user_id = ? \
AND tenant_id = ? AND secret_reference = ? AND updated_at < ?",
                &params![owner_user_id, &tenant_id, &secret_reference, before],
            )?;
            if deleted > 0 {
                removed.push(secret_reference);
            }
            if removed.len() == PRUNE_PAGE {
                break;
            }
        }
        if rows.len() < PRUNE_PAGE {
            break;
        }
    }
    Ok(json!({"removed": removed, "count": removed.len(), "limit": PRUNE_PAGE}))
}
