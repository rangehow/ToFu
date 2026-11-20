//! `operation_domains/identity.py`: tenant user accounts
//! (`operations_pkg/_tenant.py`) and bearer credentials
//! (`operations_pkg/_credentials.py`).

use serde_json::{json, Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::{float_value, truthy};
use crate::params;
use crate::payload::{char_len, get, integer, number, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "tenant.user.create", OperationSpec::command(true, tenant_user_create));
    add(registry, "tenant.user.get", OperationSpec::query(tenant_user_get));
    add(registry, "tenant.user.list", OperationSpec::query(tenant_user_list));
    add(registry, "tenant.user.set_status", OperationSpec::command(true, tenant_user_set_status));
    add(registry, "tenant.user.set_role", OperationSpec::command(true, tenant_user_set_role));
    add(registry, "tenant.user.authentication", OperationSpec::query(tenant_user_authentication));
    add(registry, "tenant.user.record_login", OperationSpec::command(true, tenant_user_record_login));
    add(registry, "credential.create", OperationSpec::command(true, credential_create));
    add(registry, "credential.create_if_owner_empty", OperationSpec::command(false, credential_create_if_owner_empty));
    add(registry, "credential.list", OperationSpec::query(credential_list));
    add(registry, "credential.get", OperationSpec::query(credential_get));
    add(registry, "credential.authenticate", OperationSpec::command(false, credential_authenticate));
    add(registry, "credential.validate", OperationSpec::query(credential_validate));
    add(registry, "credential.touch", OperationSpec::command(false, credential_touch));
    add(registry, "credential.identify", OperationSpec::query(credential_identify));
    add(registry, "credential.update", OperationSpec::command(true, credential_update));
    add(registry, "credential.revoke", OperationSpec::command(true, credential_revoke));
    add(registry, "credential.exists", OperationSpec::query(credential_exists));
}

const MAX_TIMESTAMP: f64 = 32_503_680_000.0;

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

/// `_json_text(value)`: `_dump(value).decode()` bound as TEXT.
fn json_text(value: &Value) -> OpResult<SqlValue> {
    Ok(SqlValue::Text(String::from_utf8(codec::dump(value)?).expect("orjson output is UTF-8")))
}

fn single(key: &str, value: &Value) -> Payload {
    let mut payload = Map::new();
    payload.insert(key.to_owned(), value.clone());
    payload
}

/// `row[column] or ''` as returned inside a result dict.
fn or_empty(row: &Row, column: &str) -> OpResult<Value> {
    let value = row.require(column)?;
    if value.truthy() {
        value.to_json()
    } else {
        Ok(Value::String(String::new()))
    }
}

fn row_float(row: &Row, column: &str) -> OpResult<f64> {
    match row.require(column)?.as_f64() {
        Some(value) => Ok(value),
        None => crate::error::internal(format!("float() of {column}")),
    }
}

fn optional_float(row: &Row, column: &str) -> OpResult<Value> {
    if row.require(column)?.is_null() {
        Ok(Value::Null)
    } else {
        Ok(float_value(row_float(row, column)?))
    }
}

/// `int(row[column] or 0)`.
fn int_or_zero(row: &Row, column: &str) -> OpResult<i64> {
    if row.require(column)?.truthy() {
        row.int(column)
    } else {
        Ok(0)
    }
}

// ---------------------------------------------------------------- tenant users

const TENANT_USER_COLUMNS: &str = "id, owner_user_id, email, display_name, role, status, created_at, \
last_login_at, email_verified, metadata";

fn tenant_user_document(row: &Row) -> OpResult<Value> {
    let metadata = codec::load(row.require("metadata")?)?;
    if !metadata.is_object() {
        return fail("database_integrity", "Tenant user metadata is invalid");
    }
    Ok(json!({
        "id": row.require("id")?.to_json()?,
        "owner_user_id": row.int("owner_user_id")?,
        "email": row.require("email")?.to_json()?,
        "display_name": or_empty(row, "display_name")?,
        "role": row.require("role")?.to_json()?,
        "status": row.require("status")?.to_json()?,
        "created_at": row.int("created_at")?,
        "last_login_at": int_or_zero(row, "last_login_at")?,
        "email_verified": row.require("email_verified")?.truthy(),
        "metadata": metadata,
    }))
}

fn allocate_owner_user_id(session: &Session<'_>) -> OpResult<i64> {
    let sequence_name = "owner_user_id";
    session.lock_key("identity.owner.allocate", sequence_name);
    let sequence = session.fetch_one(
        "SELECT next_value FROM storage_identity_sequences WHERE sequence_name = ?",
        &params![sequence_name],
    )?;
    let Some(sequence) = sequence else {
        let current = session.fetch_one("SELECT COALESCE(MAX(owner_user_id), 1) AS maximum FROM tenant_users", &[])?;
        let maximum = match current {
            Some(row) => int_or_zero(&row, "maximum")?,
            None => return crate::error::internal("TypeError: 'NoneType' object is not subscriptable"),
        };
        let maximum = if maximum == 0 { 1 } else { maximum };
        let owner_user_id = 2.max(maximum + 1);
        session.execute(
            "INSERT INTO storage_identity_sequences(sequence_name, next_value) VALUES (?, ?)",
            &params![sequence_name, owner_user_id + 1],
        )?;
        return Ok(owner_user_id);
    };
    let owner_user_id = sequence.int("next_value")?;
    if owner_user_id < 2 {
        return fail("database_integrity", "Owner identity sequence is invalid");
    }
    session.execute(
        "UPDATE storage_identity_sequences SET next_value = ? WHERE sequence_name = ?",
        &params![owner_user_id + 1, sequence_name],
    )?;
    Ok(owner_user_id)
}

fn tenant_user_role(payload: &Payload) -> OpResult<String> {
    let role = required_text(payload, "role", 32)?;
    if role != "user" && role != "admin" {
        return protocol("Invalid tenant user role");
    }
    Ok(role)
}

fn tenant_user_status(payload: &Payload, optional: bool) -> OpResult<String> {
    let status = payload.get("status").cloned().unwrap_or_else(|| Value::String(String::new()));
    if optional && status == Value::String(String::new()) {
        return Ok(String::new());
    }
    match status {
        Value::String(text) if matches!(text.as_str(), "active" | "suspended" | "deleted") => Ok(text),
        _ => protocol("Invalid tenant user status"),
    }
}

/// private port of `operations_pkg/_runs.py::_optional_text`; shared owner: core (runs).
fn runs_optional_text(payload: &Payload, field: &str, maximum: usize, scope: &str) -> OpResult<String> {
    match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(text)) if char_len(text) <= maximum => Ok(text.clone()),
        Some(_) => protocol(format!("Invalid {scope} {field}")),
    }
}

fn tenant_user_by_id(session: &Session<'_>, user_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(&format!("SELECT {TENANT_USER_COLUMNS} FROM tenant_users WHERE id = ?"), &params![user_id])
}

fn tenant_user_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = required_text(payload, "user_id", 256)?;
    let email = py_strip(&required_text(payload, "email", 320)?).to_lowercase();
    let role = tenant_user_role(payload)?;
    let metadata = payload.get("metadata").cloned().unwrap_or_else(|| Value::Object(Map::new()));
    if !metadata.is_object() {
        return protocol("Invalid tenant user metadata");
    }
    session.lock_key("tenant.user.email", &email);
    if session.fetch_one("SELECT id FROM tenant_users WHERE email = ?", &params![&email])?.is_some() {
        return fail("database_conflict", "Tenant user email already exists");
    }
    let owner_user_id = allocate_owner_user_id(session)?;
    let password_hash = runs_optional_text(payload, "password_hash", 512, "tenant user")?;
    let display_name = runs_optional_text(payload, "display_name", 256, "tenant user")?;
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    session.execute(
        "INSERT INTO tenant_users(\
id, owner_user_id, email, password_hash, display_name, role, status, \
created_at, last_login_at, email_verified, metadata) \
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            &user_id,
            owner_user_id,
            &email,
            password_hash,
            display_name,
            role,
            "active",
            created_at,
            0i64,
            0i64,
            json_text(&metadata)?,
        ],
    )?;
    match tenant_user_by_id(session, &user_id)? {
        Some(row) => tenant_user_document(&row),
        None => fail("database_integrity", "Tenant user insert was not visible"),
    }
}

fn tenant_user_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let empty = Value::String(String::new());
    let user_id = payload.get("user_id").unwrap_or(&empty);
    let email = payload.get("email").unwrap_or(&empty);
    if truthy(user_id) == truthy(email) {
        return protocol("Exactly one tenant user selector is required");
    }
    let (value, predicate) = if truthy(user_id) {
        (required_text(payload, "user_id", 256)?, "id = ?")
    } else {
        (py_strip(&required_text(payload, "email", 320)?).to_lowercase(), "email = ?")
    };
    let row = session
        .fetch_one(&format!("SELECT {TENANT_USER_COLUMNS} FROM tenant_users WHERE {predicate}"), &params![value])?;
    match row {
        Some(row) => tenant_user_document(&row),
        None => Ok(Value::Null),
    }
}

fn tenant_user_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(100), Some(1), Some(1000))?;
    let offset = integer(payload, "offset", Some(0), Some(0), Some(10_000_000))?;
    let status = tenant_user_status(payload, true)?;
    let rows = if !status.is_empty() {
        session.fetch_all(
            &format!(
                "SELECT {TENANT_USER_COLUMNS} FROM tenant_users \
WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            ),
            &params![status, limit, offset],
        )?
    } else {
        session.fetch_all(
            &format!(
                "SELECT {TENANT_USER_COLUMNS} FROM tenant_users ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            ),
            &params![limit, offset],
        )?
    };
    Ok(Value::Array(rows.iter().map(tenant_user_document).collect::<OpResult<_>>()?))
}

fn tenant_user_set_status(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = required_text(payload, "user_id", 256)?;
    let status = tenant_user_status(payload, false)?;
    let count = session.execute("UPDATE tenant_users SET status = ? WHERE id = ?", &params![status, &user_id])?;
    if count == 0 {
        return Ok(Value::Null);
    }
    match tenant_user_by_id(session, &user_id)? {
        Some(row) => tenant_user_document(&row),
        None => Ok(Value::Null),
    }
}

fn tenant_user_set_role(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = required_text(payload, "user_id", 256)?;
    let role = tenant_user_role(payload)?;
    let count = session.execute("UPDATE tenant_users SET role = ? WHERE id = ?", &params![role, &user_id])?;
    if count == 0 {
        return Ok(Value::Null);
    }
    match tenant_user_by_id(session, &user_id)? {
        Some(row) => tenant_user_document(&row),
        None => Ok(Value::Null),
    }
}

fn tenant_user_authentication(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let email = py_strip(&required_text(payload, "email", 320)?).to_lowercase();
    let row = session.fetch_one(
        &format!("SELECT {TENANT_USER_COLUMNS}, password_hash FROM tenant_users WHERE email = ?"),
        &params![email],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    Ok(json!({
        "user": tenant_user_document(&row)?,
        "password_hash": or_empty(&row, "password_hash")?,
    }))
}

fn tenant_user_record_login(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let last_login_at = integer(payload, "last_login_at", None, Some(0), None)?;
    let user_id = required_text(payload, "user_id", 256)?;
    let count =
        session.execute("UPDATE tenant_users SET last_login_at = ? WHERE id = ?", &params![last_login_at, user_id])?;
    Ok(json!({"updated": count > 0}))
}

// ------------------------------------------------------------------ credentials

const CREDENTIAL_COLUMNS: &str = "id, owner_user_id, account_user_id, tenant_id, name, prefix, scopes, \
rate_limit_rpm, rate_limit_tpd, created_at, last_used_at, expires_at, \
disabled, revoked_at, metadata";

/// `_credentials.py::_optional_text`.
fn optional_text(payload: &Payload, key: &str, maximum: usize) -> OpResult<String> {
    match payload.get(key) {
        None => Ok(String::new()),
        Some(Value::String(text)) if char_len(text) <= maximum => Ok(py_strip(text)),
        Some(_) => protocol(format!("Invalid {key} in credential request")),
    }
}

fn owner_boundary(payload: &Payload) -> OpResult<(i64, String)> {
    Ok((integer(payload, "owner_user_id", None, Some(1), None)?, optional_text(payload, "tenant_id", 256)?))
}

fn scopes(value: &Value) -> OpResult<Vec<String>> {
    let valid = match value {
        Value::Array(items) => {
            items.len() <= 128
                && items.iter().all(|scope| matches!(scope, Value::String(s) if !s.is_empty() && char_len(s) <= 128))
        }
        _ => false,
    };
    if !valid {
        return protocol("Invalid scopes in credential request");
    }
    let mut result: Vec<String> =
        value.as_array().expect("array").iter().map(|scope| scope.as_str().expect("str").to_owned()).collect();
    result.sort();
    result.dedup();
    Ok(result)
}

fn is_secret_hash(text: &str) -> bool {
    text.len() == 64 && text.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn secret_hash(payload: &Payload) -> OpResult<String> {
    let secret_hash = required_text(payload, "secret_hash", 64)?;
    if !is_secret_hash(&secret_hash) {
        return protocol("Invalid secret_hash in credential request");
    }
    Ok(secret_hash)
}

fn credential_document(row: &Row) -> OpResult<Value> {
    let scopes = codec::load(row.require("scopes")?)?;
    let metadata = codec::load(row.require("metadata")?)?;
    if !matches!(&scopes, Value::Array(items) if items.iter().all(Value::is_string)) {
        return fail("database_integrity", "Credential scopes are invalid");
    }
    if !metadata.is_object() {
        return fail("database_integrity", "Credential metadata is invalid");
    }
    Ok(json!({
        "id": row.require("id")?.to_json()?,
        "owner_user_id": row.int("owner_user_id")?,
        "account_user_id": or_empty(row, "account_user_id")?,
        "tenant_id": or_empty(row, "tenant_id")?,
        "name": row.require("name")?.to_json()?,
        "prefix": row.require("prefix")?.to_json()?,
        "scopes": scopes,
        "rate_limit_rpm": int_or_zero(row, "rate_limit_rpm")?,
        "rate_limit_tpd": int_or_zero(row, "rate_limit_tpd")?,
        "created_at": float_value(row_float(row, "created_at")?),
        "last_used_at": optional_float(row, "last_used_at")?,
        "expires_at": optional_float(row, "expires_at")?,
        "disabled": row.require("disabled")?.truthy(),
        "revoked_at": optional_float(row, "revoked_at")?,
        "metadata": metadata,
    }))
}

/// The account row is missing, inactive or owned by another principal.
fn account_rejected(session: &Session<'_>, account_user_id: &str, owner_user_id: i64) -> OpResult<bool> {
    let account =
        session.fetch_one("SELECT owner_user_id, status FROM tenant_users WHERE id = ?", &params![account_user_id])?;
    Ok(match account {
        None => true,
        Some(account) => {
            account.require("status")?.as_str() != Some("active") || account.int("owner_user_id")? != owner_user_id
        }
    })
}

fn credential_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let credential_id = required_text(payload, "credential_id", 128)?;
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let account_user_id = optional_text(payload, "account_user_id", 256)?;
    let name = py_strip(&required_text(payload, "name", 80)?);
    let prefix = required_text(payload, "prefix", 32)?;
    let secret_hash = secret_hash(payload)?;
    let scopes = scopes(get(payload, "scopes"))?;
    if scopes.is_empty() {
        return protocol("Credential requires at least one scope");
    }
    let metadata = payload.get("metadata").cloned().unwrap_or_else(|| Value::Object(Map::new()));
    if !metadata.is_object() {
        return protocol("Invalid metadata in credential request");
    }
    let expires_at = if get(payload, "expires_at").is_null() {
        None
    } else {
        Some(number(payload, "expires_at", 0.0, MAX_TIMESTAMP)?)
    };
    if !account_user_id.is_empty() && account_rejected(session, &account_user_id, owner_user_id)? {
        return fail("database_conflict", "Credential account is missing, inactive, or owned by another principal");
    }
    session.lock_key("identity.credential", &credential_id);
    session.lock_key("identity.credential.secret", &secret_hash);
    if session
        .fetch_one(
            "SELECT id FROM auth_credentials WHERE id = ? OR secret_hash = ?",
            &params![&credential_id, &secret_hash],
        )?
        .is_some()
    {
        return fail("database_conflict", "Credential already exists");
    }
    let scopes_text = json_text(&Value::Array(scopes.into_iter().map(Value::String).collect()))?;
    let rate_limit_rpm = integer(payload, "rate_limit_rpm", None, Some(0), None)?;
    let rate_limit_tpd = integer(payload, "rate_limit_tpd", None, Some(0), None)?;
    let created_at = number(payload, "created_at", 0.0, MAX_TIMESTAMP)?;
    let metadata_text = json_text(&metadata)?;
    session.execute(
        "INSERT INTO auth_credentials(\
id, owner_user_id, account_user_id, tenant_id, name, prefix, \
secret_hash, scopes, rate_limit_rpm, rate_limit_tpd, created_at, \
last_used_at, expires_at, disabled, metadata) \
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?)",
        &params![
            &credential_id,
            owner_user_id,
            account_user_id,
            tenant_id,
            name,
            prefix,
            secret_hash,
            scopes_text,
            rate_limit_rpm,
            rate_limit_tpd,
            created_at,
            expires_at,
            metadata_text,
        ],
    )?;
    let row = session.fetch_one(
        &format!("SELECT {CREDENTIAL_COLUMNS} FROM auth_credentials WHERE id = ?"),
        &params![&credential_id],
    )?;
    match row {
        Some(row) => credential_document(&row),
        None => fail("database_integrity", "Credential insert was not visible"),
    }
}

fn credential_create_if_owner_empty(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    session.lock_key("identity.credential.bootstrap", &format!("{tenant_id}:{owner_user_id}"));
    let existing = session.fetch_one(
        "SELECT 1 AS present FROM auth_credentials \
WHERE owner_user_id = ? AND tenant_id = ? \
AND revoked_at IS NULL LIMIT 1",
        &params![owner_user_id, tenant_id],
    )?;
    if existing.is_some() {
        return Ok(Value::Null);
    }
    credential_create(session, payload)
}

fn credential_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let rows = session.fetch_all(
        &format!(
            "SELECT {CREDENTIAL_COLUMNS} FROM auth_credentials \
WHERE owner_user_id = ? AND tenant_id = ? \
AND revoked_at IS NULL \
ORDER BY created_at DESC, id DESC"
        ),
        &params![owner_user_id, tenant_id],
    )?;
    Ok(Value::Array(rows.iter().map(credential_document).collect::<OpResult<_>>()?))
}

fn credential_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let credential_id = required_text(payload, "credential_id", 128)?;
    let row = session.fetch_one(
        &format!(
            "SELECT {CREDENTIAL_COLUMNS} FROM auth_credentials \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ? \
AND revoked_at IS NULL"
        ),
        &params![credential_id, owner_user_id, tenant_id],
    )?;
    match row {
        Some(row) => credential_document(&row),
        None => Ok(Value::Null),
    }
}

fn credential_authenticate(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (row, now) = validated_credential_row(session, payload)?;
    let Some(row) = row else { return Ok(Value::Null) };
    session.execute("UPDATE auth_credentials SET last_used_at = ? WHERE id = ?", &params![now, row.require("id")?])?;
    let mut document = credential_document(&row)?;
    document["last_used_at"] = float_value(now);
    Ok(document)
}

fn validated_credential_row(session: &Session<'_>, payload: &Payload) -> OpResult<(Option<Row>, f64)> {
    let secret_hash = secret_hash(payload)?;
    let now = number(payload, "now", 0.0, MAX_TIMESTAMP)?;
    let row = session.fetch_one(
        &format!(
            "SELECT {CREDENTIAL_COLUMNS} FROM auth_credentials \
WHERE secret_hash = ? AND disabled = 0 \
AND revoked_at IS NULL \
AND (expires_at IS NULL OR expires_at > ?)"
        ),
        &params![secret_hash, now],
    )?;
    let Some(row) = row else { return Ok((None, now)) };
    let account_user_id = row.text("account_user_id");
    if !account_user_id.is_empty() && account_rejected(session, &account_user_id, row.int("owner_user_id")?)? {
        return Ok((None, now));
    }
    Ok((Some(row), now))
}

fn credential_validate(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    match validated_credential_row(session, payload)?.0 {
        Some(row) => credential_document(&row),
        None => Ok(Value::Null),
    }
}

fn credential_touch(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let credential_id = required_text(payload, "credential_id", 128)?;
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let used_at = number(payload, "used_at", 0.0, MAX_TIMESTAMP)?;
    let touch_if_before = number(payload, "touch_if_before", 0.0, MAX_TIMESTAMP)?;
    if touch_if_before > used_at {
        return protocol("Credential touch boundary cannot exceed used_at");
    }
    let row = session.fetch_one(
        &format!(
            "SELECT {CREDENTIAL_COLUMNS} FROM auth_credentials \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ? \
AND disabled = 0 AND revoked_at IS NULL \
AND (expires_at IS NULL OR expires_at > ?)"
        ),
        &params![&credential_id, owner_user_id, &tenant_id, used_at],
    )?;
    let Some(row) = row else { return Ok(json!({"touched": false})) };
    let account_user_id = row.text("account_user_id");
    if !account_user_id.is_empty() && account_rejected(session, &account_user_id, owner_user_id)? {
        return Ok(json!({"touched": false}));
    }
    let changed = session.execute(
        "UPDATE auth_credentials SET last_used_at = ? \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ? \
AND disabled = 0 AND revoked_at IS NULL \
AND (expires_at IS NULL OR expires_at > ?) \
AND (last_used_at IS NULL OR last_used_at < ?)",
        &params![used_at, credential_id, owner_user_id, tenant_id, used_at, touch_if_before],
    )?;
    Ok(json!({"touched": changed > 0}))
}

fn credential_identify(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let secret_hash = secret_hash(payload)?;
    let row = session.fetch_one(
        &format!("SELECT {CREDENTIAL_COLUMNS} FROM auth_credentials WHERE secret_hash = ?"),
        &params![secret_hash],
    )?;
    match row {
        Some(row) => credential_document(&row),
        None => Ok(Value::Null),
    }
}

const CREDENTIAL_UPDATE_FIELDS: &[&str] =
    &["name", "scopes", "rate_limit_rpm", "rate_limit_tpd", "expires_at", "disabled", "metadata"];

fn credential_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let credential_id = required_text(payload, "credential_id", 128)?;
    let updates = match payload.get("updates") {
        Some(Value::Object(updates)) if !updates.is_empty() => updates,
        _ => return protocol("Credential updates must be an object"),
    };
    if updates.keys().any(|key| !CREDENTIAL_UPDATE_FIELDS.contains(&key.as_str())) {
        return protocol("Credential update contains unknown fields");
    }
    let mut assignments = Vec::new();
    let mut parameters = Vec::new();
    for (key, value) in updates {
        let normalized = match key.as_str() {
            "name" => SqlValue::Text(py_strip(&required_text(&single("name", value), "name", 80)?)),
            "scopes" => json_text(&Value::Array(scopes(value)?.into_iter().map(Value::String).collect()))?,
            "rate_limit_rpm" | "rate_limit_tpd" => {
                SqlValue::Integer(integer(&single(key, value), key, None, Some(0), None)?)
            }
            "expires_at" => {
                if value.is_null() {
                    SqlValue::Null
                } else {
                    SqlValue::Real(number(&single("expires_at", value), "expires_at", 0.0, MAX_TIMESTAMP)?)
                }
            }
            "disabled" => match value {
                Value::Bool(flag) => SqlValue::Integer(*flag as i64),
                _ => return protocol("Credential disabled update must be boolean"),
            },
            _ => {
                if !value.is_object() {
                    return protocol("Credential metadata update must be an object");
                }
                json_text(value)?
            }
        };
        assignments.push(format!("{key} = ?"));
        parameters.push(normalized);
    }
    parameters.extend(params![credential_id, owner_user_id, tenant_id]);
    session.execute(
        &format!(
            "UPDATE auth_credentials SET {} WHERE id = ? AND owner_user_id = ? AND tenant_id = ? AND revoked_at IS NULL",
            assignments.join(", ")
        ),
        &parameters,
    )?;
    credential_get(session, payload)
}

fn credential_revoke(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let credential_id = required_text(payload, "credential_id", 128)?;
    let row = session.fetch_one(
        "SELECT revoked_at, metadata FROM auth_credentials \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        &params![&credential_id, owner_user_id, &tenant_id],
    )?;
    let row = match row {
        Some(row) if row.require("revoked_at")?.is_null() => row,
        _ => return Ok(json!({"revoked": false, "metadata": {}})),
    };
    let metadata = codec::load(row.require("metadata")?)?;
    if !metadata.is_object() {
        return fail("database_integrity", "Credential metadata is invalid");
    }
    let revoked_at = number(payload, "revoked_at", 0.0, MAX_TIMESTAMP)?;
    session.execute(
        "UPDATE auth_credentials SET disabled = 1, revoked_at = ? \
WHERE id = ? AND owner_user_id = ? AND tenant_id = ?",
        &params![revoked_at, credential_id, owner_user_id, tenant_id],
    )?;
    Ok(json!({"revoked": true, "metadata": metadata}))
}

fn credential_exists(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, tenant_id) = owner_boundary(payload)?;
    let row = session.fetch_one(
        "SELECT 1 AS present FROM auth_credentials \
WHERE owner_user_id = ? AND tenant_id = ? \
AND revoked_at IS NULL LIMIT 1",
        &params![owner_user_id, tenant_id],
    )?;
    Ok(json!({"exists": row.is_some()}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scopes_sort_and_dedupe_like_sorted_set() {
        assert_eq!(scopes(&json!(["b", "a", "b"])).unwrap(), vec!["a".to_owned(), "b".to_owned()]);
        assert!(scopes(&json!(["a", ""])).is_err());
        assert!(scopes(&json!("a")).is_err());
    }

    #[test]
    fn secret_hash_and_strip_follow_python() {
        assert!(is_secret_hash(&"a".repeat(64)));
        assert!(!is_secret_hash(&"A".repeat(64)));
        assert_eq!(py_strip("\u{1f} x \u{a0}"), "x");
    }
}
