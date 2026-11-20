//! `operation_domains/providers.py`: owner-scoped BYO providers
//! (`operations_pkg/_providers.py`).

use serde_json::{json, Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::float_value;
use crate::params;
use crate::payload::{char_len, get, integer, number, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "provider.list", OperationSpec::query(provider_list));
    add(registry, "provider.get", OperationSpec::query(provider_get));
    add(registry, "provider.create", OperationSpec::command(true, provider_create));
    add(registry, "provider.update", OperationSpec::command(true, provider_update));
    add(registry, "provider.delete", OperationSpec::command(true, provider_delete));
    add(registry, "provider.touch", OperationSpec::command(false, provider_touch));
}

const MAX_TIMESTAMP: f64 = 32_503_680_000.0;

const PROVIDER_COLUMNS: &str = "id, owner_user_id, tenant_id, name, base_url, api_key_ciphertext, \
key_hint, models_json, extra_headers_json, thinking_format, disabled, \
created_at, updated_at, last_used_at";

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
fn json_text(value: &Value) -> OpResult<SqlValue> {
    Ok(SqlValue::Text(String::from_utf8(codec::dump(value)?).expect("orjson output is UTF-8")))
}

fn single(key: &str, value: &Value) -> Payload {
    let mut payload = Map::new();
    payload.insert(key.to_owned(), value.clone());
    payload
}

fn optional_text(payload: &Payload, key: &str, maximum: usize) -> OpResult<String> {
    match payload.get(key) {
        None => Ok(String::new()),
        Some(Value::String(text)) if char_len(text) <= maximum => Ok(py_strip(text)),
        Some(_) => protocol(format!("Invalid {key} in provider request")),
    }
}

fn owner_boundary(payload: &Payload) -> OpResult<(i64, String)> {
    Ok((integer(payload, "owner_user_id", None, Some(1), None)?, optional_text(payload, "tenant_id", 256)?))
}

fn json_array(payload: &Payload, key: &str, maximum: usize) -> OpResult<Value> {
    match get(payload, key) {
        value @ Value::Array(items) if items.len() <= maximum => Ok(value.clone()),
        _ => protocol(format!("Invalid {key} in provider request")),
    }
}

fn json_object(payload: &Payload, key: &str, maximum: usize) -> OpResult<Value> {
    match get(payload, key) {
        value @ Value::Object(map) if map.len() <= maximum => Ok(value.clone()),
        _ => protocol(format!("Invalid {key} in provider request")),
    }
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

fn provider_document(row: &Row, include_ciphertext: bool) -> OpResult<Value> {
    let models = codec::load(row.require("models_json")?)?;
    let headers = codec::load(row.require("extra_headers_json")?)?;
    if !models.is_array() || !headers.is_object() {
        return fail("database_integrity", "BYO provider documents are invalid");
    }
    let mut document = json!({
        "id": py_str_column(row.require("id")?),
        "owner_user_id": row.int("owner_user_id")?,
        "tenant_id": row.require("tenant_id")?.text_or_empty(),
        "name": py_str_column(row.require("name")?),
        "base_url": py_str_column(row.require("base_url")?),
        "key_hint": row.require("key_hint")?.text_or_empty(),
        "models": models,
        "extra_headers": headers,
        "thinking_format": row.require("thinking_format")?.text_or_empty(),
        "disabled": row.require("disabled")?.truthy(),
        "created_at": float_value(row_float(row, "created_at")?),
        "updated_at": float_value(row_float(row, "updated_at")?),
        "last_used_at": if row.require("last_used_at")?.is_null() {
            Value::Null
        } else {
            float_value(row_float(row, "last_used_at")?)
        },
    });
    if include_ciphertext {
        document["api_key_ciphertext"] = Value::String(row.require("api_key_ciphertext")?.text_or_empty());
    }
    Ok(document)
}

fn provider_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let rows = session.fetch_all(
        &format!(
            "SELECT {PROVIDER_COLUMNS} FROM byo_providers \
WHERE owner_user_id = ? AND tenant_id = ? \
ORDER BY created_at DESC, id DESC"
        ),
        &params![owner_user_id, tenant_id],
    )?;
    Ok(Value::Array(rows.iter().map(|row| provider_document(row, false)).collect::<OpResult<_>>()?))
}

fn provider_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let provider_id = required_text(payload, "provider_id", 128)?;
    let row = session.fetch_one(
        &format!(
            "SELECT {PROVIDER_COLUMNS} FROM byo_providers \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ?"
        ),
        &params![provider_id, owner_user_id, tenant_id],
    )?;
    match row {
        Some(row) => provider_document(&row, true),
        None => Ok(Value::Null),
    }
}

fn provider_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let provider_id = required_text(payload, "provider_id", 128)?;
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let name = py_strip(&required_text(payload, "name", 80)?);
    let base_url = py_strip(&required_text(payload, "base_url", 500)?);
    let ciphertext = optional_text(payload, "api_key_ciphertext", 32768)?;
    let key_hint = optional_text(payload, "key_hint", 64)?;
    let models = json_array(payload, "models", 64)?;
    let headers = json_object(payload, "extra_headers", 16)?;
    let thinking_format = optional_text(payload, "thinking_format", 64)?;
    let created_at = number(payload, "created_at", 0.0, MAX_TIMESTAMP)?;

    if session.fetch_one("SELECT 1 AS present FROM byo_providers WHERE id = ?", &params![&provider_id])?.is_some() {
        return fail("database_conflict", "Provider already exists");
    }
    session.execute(
        "INSERT INTO byo_providers(\
id, owner_user_id, tenant_id, name, base_url, api_key_ciphertext, \
key_hint, models_json, extra_headers_json, thinking_format, disabled, \
created_at, updated_at, last_used_at) \
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)",
        &params![
            provider_id,
            owner_user_id,
            tenant_id,
            name,
            base_url,
            ciphertext,
            key_hint,
            json_text(&models)?,
            json_text(&headers)?,
            thinking_format,
            created_at,
            created_at,
        ],
    )?;
    provider_get(session, payload)
}

const PROVIDER_UPDATE_FIELDS: &[&str] =
    &["name", "base_url", "api_key_ciphertext", "key_hint", "models", "extra_headers", "thinking_format", "disabled"];

fn provider_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let provider_id = required_text(payload, "provider_id", 128)?;
    let updates = match payload.get("updates") {
        Some(Value::Object(updates)) if !updates.is_empty() => updates,
        _ => return protocol("Provider updates must be an object"),
    };
    if updates.keys().any(|key| !PROVIDER_UPDATE_FIELDS.contains(&key.as_str())) {
        return protocol("Provider update contains unknown fields");
    }
    let mut assignments = Vec::new();
    let mut parameters = Vec::new();
    for (key, value) in updates {
        let field = single(key, value);
        let (column, normalized) = match key.as_str() {
            "name" => ("name", SqlValue::Text(py_strip(&required_text(&field, key, 80)?))),
            "base_url" => ("base_url", SqlValue::Text(py_strip(&required_text(&field, key, 500)?))),
            "api_key_ciphertext" => ("api_key_ciphertext", SqlValue::Text(optional_text(&field, key, 32768)?)),
            "key_hint" => ("key_hint", SqlValue::Text(optional_text(&field, key, 64)?)),
            "models" => ("models_json", json_text(&json_array(&field, key, 64)?)?),
            "extra_headers" => ("extra_headers_json", json_text(&json_object(&field, key, 16)?)?),
            "thinking_format" => ("thinking_format", SqlValue::Text(optional_text(&field, key, 64)?)),
            _ => match value {
                Value::Bool(flag) => ("disabled", SqlValue::Integer(*flag as i64)),
                _ => return protocol("Provider disabled update must be boolean"),
            },
        };
        assignments.push(format!("{column} = ?"));
        parameters.push(normalized);
    }
    assignments.push("updated_at = ?".to_owned());
    parameters.push(SqlValue::Real(number(payload, "updated_at", 0.0, MAX_TIMESTAMP)?));
    parameters.extend(params![provider_id, owner_user_id, tenant_id]);
    session.execute(
        &format!(
            "UPDATE byo_providers SET {} WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
            assignments.join(", ")
        ),
        &parameters,
    )?;
    provider_get(session, payload)
}

fn provider_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let provider_id = required_text(payload, "provider_id", 128)?;
    let deleted = session.execute(
        "DELETE FROM byo_providers \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        &params![provider_id, owner_user_id, tenant_id],
    )?;
    Ok(json!({"deleted": deleted > 0}))
}

fn provider_touch(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let provider_id = required_text(payload, "provider_id", 128)?;
    let used_at = number(payload, "used_at", 0.0, MAX_TIMESTAMP)?;
    let touched = session.execute(
        "UPDATE byo_providers SET last_used_at = ? \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        &params![used_at, provider_id, owner_user_id, tenant_id],
    )?;
    Ok(json!({"touched": touched > 0}))
}
