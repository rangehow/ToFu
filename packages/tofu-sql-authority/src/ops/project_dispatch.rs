//! `operation_domains/project_dispatch.py`: owner-scoped segmented dispatch
//! persistence (`operations_pkg/_project_dispatch.py`), contract
//! `contracts/project_dispatch_storage_v2.json`.
//!
//! The versioned record CAS/codec helpers are private ports of
//! `operations_pkg/_records.py` restricted to the two private dispatch
//! namespaces (no task-result, automation or reserved-namespace branches can
//! apply to them).

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::{register as add, OperationSpec, Registry};
use crate::codec;
use crate::error::{fail, OpResult, StorageError};
use crate::json as pyjson;
use crate::params;
use crate::payload::{char_len, get, integer, required_text, Payload};
use crate::session::Session;

// `lib/storage/generated_project_dispatch_contract.py::SPEC`.
const FORMAT: &str = "tofu.project-dispatch-storage/v2";
const HEADS: &str = "project.dispatch.heads.v2";
const DOCUMENTS: &str = "project.dispatch.documents.v2";
const COLLECTIONS: &[&str] = &[
    "assignments",
    "batches",
    "settlements",
    "scope_changes",
    "source_cohorts",
    "requests",
    "accounts",
    "model_attempts",
    "indexes",
];
const PROJECT_PATH_CHARACTERS: usize = 4096;
const IDENTITY_CHARACTERS: usize = 256;
const PAGE_ROWS: i64 = 64;
const COMMIT_CHANGES: usize = 512;
const HEAD_BYTES: usize = 16384;
const DOCUMENT_BYTES: usize = 4194304;
const INDEX_BYTES: usize = 2048;
const COMMIT_BYTES: usize = 8388608;
const MAXIMUM_REVISION: i64 = 9007199254740990;

pub fn register(registry: &mut Registry) {
    add(registry, "project.dispatch.get", OperationSpec::query(get_document));
    add(registry, "project.dispatch.list", OperationSpec::query(list_documents));
    add(registry, "project.dispatch.projects", OperationSpec::query(projects));
    add(registry, "project.dispatch.commit", OperationSpec::command(true, commit));
}

fn invalid_message(message: &str) -> StorageError {
    StorageError::protocol(message)
}

fn invalid() -> StorageError {
    invalid_message("Invalid project dispatch storage request")
}

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// `_ID.fullmatch(value)` for `[A-Za-z0-9._:-]*\Z`.
fn id_matches(value: &str) -> bool {
    value.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '-'))
}

fn owner(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), Some(i64::MAX))
}

/// `_scope`: `(owner, project, owner:sha256(project))`.
fn scope(payload: &Payload) -> OpResult<(i64, String, String)> {
    let owner = owner(payload)?;
    let project = required_text(payload, "project_path", PROJECT_PATH_CHARACTERS)?;
    if python_strip(&project).is_empty() || project.contains('\x00') {
        return Err(invalid().into());
    }
    let digest = hex::encode(Sha256::digest(project.as_bytes()));
    Ok((owner, project, format!("{owner}:{digest}")))
}

/// Python `str.strip()` whitespace (`str.isspace`).
fn python_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c))
}

fn identity(value: &Value, empty: bool) -> OpResult<String> {
    let minimum = if empty { 0 } else { 1 };
    match value {
        Value::String(text) if (minimum..=IDENTITY_CHARACTERS).contains(&char_len(text)) && id_matches(text) => {
            Ok(text.clone())
        }
        _ => Err(invalid().into()),
    }
}

fn collection(payload: &Payload) -> OpResult<String> {
    match payload.get("collection") {
        Some(Value::String(text)) if COLLECTIONS.contains(&text.as_str()) => Ok(text.clone()),
        _ => Err(invalid().into()),
    }
}

fn revision(payload: &Payload, field: &str) -> OpResult<i64> {
    integer(payload, field, None, Some(0), Some(MAXIMUM_REVISION))
}

fn document_size(value: &Value, maximum: usize) -> OpResult<usize> {
    if !value.is_object() {
        return Err(invalid_message("Dispatch document must be an object").into());
    }
    let size = pyjson::dumps_sorted(value).len();
    if size > maximum {
        return Err(invalid_message("Dispatch document exceeds transport bound").into());
    }
    Ok(size)
}

// private port of lib/storage_authority_sql/operations_pkg/_records.py::_record_get_owned; shared owner: core
fn record_get(session: &Session<'_>, namespace: &str, key: &str) -> OpResult<Value> {
    check_record_key(key)?;
    let row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![namespace, key],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    Ok(json!({
        "value": codec::load(row.get("value_json"))?,
        "version": row.int("version")?,
        "updated_at_ms": row.int("updated_at_ms")?,
    }))
}

/// `_required_text(payload, "key")` inside the private record helpers.
fn check_record_key(key: &str) -> OpResult<()> {
    if key.is_empty() || char_len(key) > 512 {
        return Err(StorageError::protocol("Invalid key in storage request").into());
    }
    Ok(())
}

// private port of lib/storage_authority_sql/operations_pkg/_records.py::_record_put_owned; shared owner: core
fn record_put(session: &Session<'_>, namespace: &str, key: &str, expected: i64, value: &Value) -> OpResult<Value> {
    check_record_key(key)?;
    let encoded = codec::dump_param(value)?;
    let now = now_ms();
    session.lock_key("record", &format!("{namespace}:{key}"));
    let current = session.fetch_one(
        "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![namespace, key],
    )?;
    let actual = match current {
        Some(row) => row.int("version")?,
        None => 0,
    };
    if expected != actual {
        return fail("database_conflict", "Storage record version conflict");
    }
    let version = actual + 1;
    session.execute(
        "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) \
         VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET \
         value_json = excluded.value_json, version = excluded.version, \
         updated_at_ms = excluded.updated_at_ms",
        &params![namespace, key, encoded, version, now],
    )?;
    Ok(json!({"key": key, "version": version, "updated_at_ms": now}))
}

// private port of lib/storage_authority_sql/operations_pkg/_records.py::_record_delete_owned; shared owner: core
fn record_delete(session: &Session<'_>, namespace: &str, key: &str, expected: i64) -> OpResult<()> {
    check_record_key(key)?;
    let count = session.execute(
        "DELETE FROM storage_records WHERE namespace = ? AND record_key = ? AND version = ?",
        &params![namespace, key, expected],
    )?;
    if count == 0 {
        let current = session.fetch_one(
            "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
            &params![namespace, key],
        )?;
        if current.is_some() {
            return fail("database_conflict", "Storage record version conflict");
        }
    }
    Ok(())
}

// private port of lib/storage_authority_sql/operations_pkg/_records.py::_record_list_owned
// (binary_order=True over a dispatch namespace); shared owner: core
fn record_list(
    session: &Session<'_>,
    namespace: &str,
    prefix: &str,
    cursor: &str,
    limit: i64,
    include_value: bool,
    reverse: bool,
) -> OpResult<Value> {
    if char_len(prefix) > 512 {
        return Err(StorageError::protocol("Invalid prefix in storage request").into());
    }
    if char_len(cursor) > 512 || (!cursor.is_empty() && !cursor.starts_with(prefix)) {
        return Err(StorageError::protocol("Invalid record list cursor").into());
    }
    let ordered_key = "record_key COLLATE BINARY";
    let boundary = if cursor.is_empty() {
        String::new()
    } else if reverse {
        format!(" AND {ordered_key} < ?")
    } else {
        format!(" AND {ordered_key} > ?")
    };
    let direction = if reverse { "DESC" } else { "ASC" };
    if prefix.is_empty() || !prefix.is_ascii() {
        return Err(StorageError::protocol("Invalid owned record range").into());
    }
    let private_names = format!("'{DOCUMENTS}','{HEADS}'");
    let scope =
        format!("namespace IN ({private_names}) AND namespace = ? AND {ordered_key} >= ? AND {ordered_key} < ? ");
    let last = prefix.as_bytes()[prefix.len() - 1];
    let upper = format!("{}{}", &prefix[..prefix.len() - 1], (last + 1) as char);
    let mut parameters = params![namespace, prefix, upper].to_vec();
    if !cursor.is_empty() {
        parameters.push(cursor.into());
    }
    parameters.push(limit.into());
    let value_column = if include_value { "value_json, " } else { "" };
    let sql = format!(
        "SELECT record_key, {value_column}version, updated_at_ms FROM storage_records WHERE {scope}{boundary} \
         ORDER BY {ordered_key} {direction} LIMIT ?"
    );
    let rows = session.fetch_all(&sql, &parameters)?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let mut entry = Map::new();
        entry.insert("key".into(), row.require("record_key")?.to_json()?);
        if include_value {
            entry.insert("value".into(), codec::load(row.get("value_json"))?);
        }
        entry.insert("version".into(), Value::from(row.int("version")?));
        entry.insert("updated_at_ms".into(), Value::from(row.int("updated_at_ms")?));
        result.push(Value::Object(entry));
    }
    Ok(Value::Array(result))
}

/// `get`.
fn get_document(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (_, _, project_key) = scope(payload)?;
    let identity = identity(get(payload, "id"), false)?;
    let (namespace, key) = if payload.get("collection") == Some(&Value::String("head".into())) {
        if identity != "root" {
            return Err(invalid().into());
        }
        (HEADS, project_key)
    } else {
        let collection = collection(payload)?;
        (DOCUMENTS, format!("{project_key}:{collection}:{identity}"))
    };
    record_get(session, namespace, &key)
}

/// `_page`.
fn page(
    session: &Session<'_>,
    payload: &Payload,
    namespace: &str,
    prefix: &str,
    include_value: bool,
) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(PAGE_ROWS), Some(1), Some(PAGE_ROWS))?;
    let reverse = match payload.get("reverse") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return Err(invalid_message("Dispatch ordering must be boolean").into()),
    };
    let after = match payload.get("after_key") {
        None => String::new(),
        Some(Value::String(text))
            if char_len(text) <= 512 && id_matches(text) && (text.is_empty() || text.starts_with(prefix)) =>
        {
            text.clone()
        }
        Some(_) => return Err(invalid_message("Dispatch cursor differs from owned prefix").into()),
    };
    record_list(session, namespace, prefix, &after, limit, include_value, reverse)
}

/// `list_documents`.
fn list_documents(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (_, _, key) = scope(payload)?;
    let collection = collection(payload)?;
    let empty = Value::String(String::new());
    let prefix = identity(payload.get("prefix").unwrap_or(&empty), true)?;
    page(session, payload, DOCUMENTS, &format!("{key}:{collection}:{prefix}"), collection == "indexes")
}

/// `projects`.
fn projects(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    page(session, payload, HEADS, &format!("{owner}:"), true)
}

struct Prepared {
    deleting: bool,
    key: String,
    expected_version: i64,
    value: Value,
}

/// `_require_turn_witnesses`: validate the whole set before observing rows,
/// lock conversations in deterministic order, then fence each source Turn.
fn require_turn_witnesses(session: &Session<'_>, owner: i64, witnesses: &Value) -> OpResult<()> {
    let Some(witnesses) = witnesses.as_array().filter(|rows| rows.len() <= COMMIT_CHANGES) else {
        return Err(invalid_message("Invalid dispatch Turn witnesses").into());
    };
    let mut prepared = std::collections::BTreeMap::new();
    for value in witnesses {
        let Some(row) = value.as_object().filter(|row| {
            row.len() == 5
                && ["conversation_id", "turn_id", "attempt_id", "projection_revision", "status"]
                    .iter()
                    .all(|key| row.contains_key(*key))
        }) else {
            return Err(invalid_message("Invalid dispatch Turn witness").into());
        };
        let conv = identity(get(row, "conversation_id"), false)?;
        let turn = identity(get(row, "turn_id"), false)?;
        let attempt = identity(get(row, "attempt_id"), true)?;
        let status = identity(get(row, "status"), false)?;
        if prepared.contains_key(&(conv.clone(), turn.clone())) {
            return Err(invalid_message("Duplicate dispatch Turn witness").into());
        }
        prepared.insert((conv, turn), (revision(row, "projection_revision")?, attempt, status));
    }
    let conversations: std::collections::BTreeSet<&str> = prepared.keys().map(|(conv, _)| conv.as_str()).collect();
    for conv in conversations {
        session.lock_key("turn_conversation", &format!("{owner}:{conv}"));
    }
    for ((conv, turn), expected) in prepared {
        let current = session.fetch_one_for_update(
            "SELECT projection_revision, current_attempt_id, status, settlement_json FROM storage_conversation_turns \
             WHERE conversation_id=? AND turn_id=? AND user_id=?",
            &params![conv, turn, owner],
        )?;
        let matches = match current {
            Some(row) => {
                (
                    row.int("projection_revision")?,
                    row.text("current_attempt_id"),
                    crate::ops::turns::turn_status_and_settlement(&row)?.0,
                ) == expected
            }
            None => false,
        };
        if !matches {
            return fail("database_conflict", "Dispatch Turn witness changed");
        }
    }
    Ok(())
}

/// `commit`.
fn commit(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner, project, key) = scope(payload)?;
    let changes = match payload.get("changes") {
        Some(Value::Array(changes)) if changes.len() <= COMMIT_CHANGES => changes,
        _ => return Err(invalid().into()),
    };
    let headless = !payload.contains_key("expected_revision") && !payload.contains_key("head");
    if (!headless && (!payload.contains_key("expected_revision") || !payload.contains_key("head")))
        || (headless && changes.is_empty())
    {
        return Err(invalid_message("Dispatch commit needs both expected_revision and head, or neither").into());
    }
    let mut size = 0usize;
    let mut expected = 0i64;
    let mut head = &Value::Null;
    if !headless {
        expected = revision(payload, "expected_revision")?;
        head = get(payload, "head");
        size = document_size(head, HEAD_BYTES)?;
    }
    let mut prepared: Vec<Prepared> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for change in changes {
        let Value::Object(change) = change else { return Err(invalid().into()) };
        let collection = collection(change)?;
        let identity = identity(get(change, "id"), false)?;
        let child_key = format!("{key}:{collection}:{identity}");
        if seen.contains(&child_key) {
            return Err(invalid_message("Duplicate dispatch document identity").into());
        }
        seen.insert(child_key.clone());
        let deleting = change.get("delete") == Some(&Value::Bool(true));
        if headless && (collection == "indexes" || revision(change, "expected_version")? == 0) {
            return Err(invalid_message("A document commit updates existing evidence only").into());
        }
        if deleting {
            if headless || collection != "indexes" || change.contains_key("value") {
                return Err(invalid_message("Only reconstructible dispatch indexes can be deleted").into());
            }
        } else if change.contains_key("delete") {
            return Err(invalid().into());
        } else {
            let maximum = if collection == "indexes" { INDEX_BYTES } else { DOCUMENT_BYTES };
            size += document_size(get(change, "value"), maximum)?;
        }
        if size > COMMIT_BYTES {
            return Err(invalid_message("Dispatch commit exceeds transport bound").into());
        }
        prepared.push(Prepared {
            deleting,
            key: child_key,
            expected_version: revision(change, "expected_version")?,
            value: get(change, "value").clone(),
        });
    }

    let empty_witnesses = Value::Array(Vec::new());
    require_turn_witnesses(session, owner, payload.get("turn_witnesses").unwrap_or(&empty_witnesses))?;

    let result = if headless {
        Value::Null
    } else {
        let envelope = json!({"format": FORMAT, "owner": owner, "projectPath": project, "state": head});
        record_put(session, HEADS, &key, expected, &envelope)?
    };
    let first_key = prepared[0..].first().map(|row| row.key.clone());
    let mut order: Vec<&Prepared> = prepared.iter().collect();
    order.sort_by(|left, right| left.key.cmp(&right.key));
    let mut receipts: std::collections::HashMap<String, Value> = std::collections::HashMap::new();
    for row in order {
        if row.deleting {
            let current = record_get(session, DOCUMENTS, &row.key)?;
            let current_version = match &current {
                Value::Object(map) => map.get("version").and_then(Value::as_i64).unwrap_or(0),
                _ => 0,
            };
            if current_version != row.expected_version {
                return fail("database_conflict", "Dispatch index version conflict");
            }
            record_delete(session, DOCUMENTS, &row.key, row.expected_version)?;
        } else {
            let receipt = record_put(session, DOCUMENTS, &row.key, row.expected_version, &row.value)?;
            receipts.insert(row.key.clone(), receipt);
        }
    }
    if headless {
        let first_key = first_key.expect("a document commit has changes");
        return Ok(receipts.remove(&first_key).expect("a document commit writes its first change"));
    }
    Ok(result)
}

#[cfg(test)]
mod witness_tests {
    use super::*;

    #[test]
    fn source_witness_is_owner_scoped_and_revision_fenced_before_dispatch_write() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE storage_conversation_turns(conversation_id TEXT, turn_id TEXT,
            user_id INTEGER, projection_revision INTEGER, current_attempt_id TEXT, status TEXT, settlement_json TEXT);
            INSERT INTO storage_conversation_turns VALUES ('conv','turn',7,3,'attempt','interrupted','{}');",
            )
            .unwrap();
        let session = Session::new(&connection, "test", None, true);
        let witness = json!([{"conversation_id":"conv","turn_id":"turn","attempt_id":"attempt",
            "projection_revision":3,"status":"interrupted"}]);
        require_turn_witnesses(&session, 7, &witness).unwrap();
        let foreign = require_turn_witnesses(&session, 8, &witness).unwrap_err();
        assert!(matches!(foreign, crate::error::Failure::Storage(error) if error.code == "database_conflict"));
        connection.execute("UPDATE storage_conversation_turns SET projection_revision=4", []).unwrap();
        let stale = require_turn_witnesses(&session, 7, &witness).unwrap_err();
        assert!(
            matches!(stale, crate::error::Failure::Storage(error) if error.message == "Dispatch Turn witness changed")
        );
    }

    #[test]
    fn malformed_and_duplicate_witness_sets_refuse_before_reading_storage() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        let session = Session::new(&connection, "test", None, true);
        for value in [Value::Null, json!({}), json!([{}])] {
            assert!(matches!(require_turn_witnesses(&session, 7, &value),
                Err(crate::error::Failure::Storage(error)) if error.code == "database_protocol_error"));
        }
        let witness = json!({"conversation_id":"conv","turn_id":"turn","attempt_id":"",
            "projection_revision":0,"status":"failed"});
        let repeated = json!([witness, witness]);
        assert!(matches!(require_turn_witnesses(&session, 7, &repeated),
            Err(crate::error::Failure::Storage(error)) if error.message == "Duplicate dispatch Turn witness"));
    }
}
