//! `operations_pkg/_records.py`: generic records and the task-result
//! projections stored in the `task_results` record namespace.

use std::collections::BTreeSet;

use serde_json::{json, Map, Value};

use super::codecs::{encoded_task_result_document, hydrated_task_result_value, project_usage_container};
use super::invocations::{
    guard_child_record, require_conversation_publication_claim, starts_new_tool_effect, worker_root_for_task,
};
use super::py::{
    attr_get, eq_str, get, int, int_or_zero, map_get, require_hashable, str_or_empty, strict_int, truncate,
};
use crate::codec::{dump, dump_param, load, py_eq};
use crate::error::{fail, internal, Failure, OpResult};
use crate::json::{self, truthy};
use crate::params;
use crate::payload::{expected_version, integer, required_text, required_text_512, Payload};
use crate::session::{Session, SqlValue};

/// `generated_checkpoint_storage_contract.SPEC['namespaces']`.
const NATIVE_CHECKPOINT_NAMESPACES: [&str; 3] = [
    "native_sql_checkpoint_headers_v1",
    "native_sql_checkpoint_replay_fences_v1",
    "native_sql_checkpoint_snapshots_v1",
];
/// `generated_project_dispatch_contract.SPEC['namespaces']`.
const DISPATCH_NAMESPACES: [&str; 2] = ["project.dispatch.documents.v2", "project.dispatch.heads.v2"];
/// `lib.automations.contract.STORAGE_NAMESPACES` kinds owned by atomic
/// automation operations (run, active, history, subscription, eventFailure,
/// presentation, target, skillSource).
const AUTOMATION_ATOMIC_NAMESPACES: [&str; 8] = [
    "automation.runs.v1",
    "automation.active.v1",
    "automation.history.v1",
    "automation.subscriptions.v1",
    "automation.event-failures.v1",
    "automation.presentations.v1",
    "automation.targets.v1",
    "automation.skillSources.v1",
];
const AUTOMATION_DEFINITION_NAMESPACE: &str = "automation.definitions.v1";

const TASK_RESULT_CHECKPOINT_GUARD_CONTRACT: &str = "tofu.task-results.checkpoint.guard/v1";
const TASK_RESULT_CACHE_SETTINGS_CONTRACT: &str = "tofu.task-results.checkpoint.cache-settings/v1";
const TASK_RESULT_CACHE_PREFIX_HWM_FIELD: &str = "cache_prefix_hwm";
const TASK_RESULT_LAST_TURN_CACHE_READ_FIELD: &str = "last_turn_cache_read";
const CONVERSATION_CACHE_PREFIX_HWM_SETTING: &str = "cachePrefixHWM";
const CONVERSATION_LAST_TURN_CACHE_READ_SETTING: &str = "lastTurnCacheRead";
const TASK_RESULT_CACHE_FACT_MAXIMUM: i64 = 2_147_483_647;
const TASK_RESULT_CACHE_BASELINE_FIELD: &str = "cache_baseline";
const CONVERSATION_CACHE_BASELINE_SETTING: &str = "cacheBaseline";
const TASK_RESULT_CACHE_BASELINE_MAXIMUM_BYTES: usize = 4096;

const COST_EXPERIMENT_SCAN_PAGE_ROWS: i64 = 8;
const COST_EXPERIMENT_SCAN_MAX_ROWS: i64 = 256;

const UPSERT_RECORD_SQL: &str =
    "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) \
VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET \
value_json = excluded.value_json, version = excluded.version, \
updated_at_ms = excluded.updated_at_ms";

fn protocol<T>(message: &str) -> OpResult<T> {
    fail("database_protocol_error", message)
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// Python `str(column)` for a TEXT key column.
pub fn column_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(integer) => integer.to_string(),
        SqlValue::Real(real) => json::python_float_repr(*real),
        SqlValue::Text(text) => text.clone(),
        SqlValue::Blob(_) => value.text_or_empty(),
    }
}

fn char_len(text: &str) -> usize {
    text.chars().count()
}

fn empty_object() -> Value {
    Value::Object(Map::new())
}

/// `_load` whose JSON-decode failure is the caller's to classify (`Err(None)`),
/// while a projection-codec StorageError keeps propagating (`Err(Some)`).
fn load_or_decode_error(value: &SqlValue) -> Result<Value, Option<Failure>> {
    match load(value) {
        Ok(value) => Ok(value),
        Err(Failure::Internal(_)) => Err(None),
        Err(other) => Err(Some(other)),
    }
}

/// `_load_record_value`.
pub fn load_record_value(namespace: &str, raw: &SqlValue) -> OpResult<Value> {
    let value = load(raw)?;
    if namespace == "task_results" {
        hydrated_task_result_value(value, None)
    } else {
        Ok(value)
    }
}

fn reserved_namespace(namespace: &str) -> bool {
    namespace == "tool_result_private" || NATIVE_CHECKPOINT_NAMESPACES.contains(&namespace)
}

/// `_guard_dispatch_namespace`.
fn guard_dispatch_namespace(payload: &Payload) -> OpResult<()> {
    if let Some(Value::String(namespace)) = payload.get("namespace") {
        if DISPATCH_NAMESPACES.contains(&namespace.as_str()) {
            return protocol("Use owned project dispatch operations");
        }
    }
    Ok(())
}

pub fn record_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    guard_dispatch_namespace(payload)?;
    record_get_owned(session, payload)
}

/// `_record_get(session, {'namespace': namespace, 'key': key})`.
pub fn record_get_value(session: &Session<'_>, namespace: &str, key: &str) -> OpResult<Value> {
    let mut payload = Map::new();
    payload.insert("namespace".into(), Value::from(namespace));
    payload.insert("key".into(), Value::from(key));
    record_get(session, &payload)
}

fn record_get_owned(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let namespace = required_text(payload, "namespace", 128)?;
    if reserved_namespace(&namespace) {
        return protocol("Reserved storage namespace");
    }
    let key = required_text_512(payload, "key")?;
    let row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![&namespace, &key],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    Ok(json!({
        "value": load_record_value(&namespace, row.get("value_json"))?,
        "version": row.int("version")?,
        "updated_at_ms": row.int("updated_at_ms")?,
    }))
}

pub fn record_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    guard_dispatch_namespace(payload)?;
    record_list_owned(session, payload)
}

fn record_list_owned(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let namespace = required_text(payload, "namespace", 128)?;
    if reserved_namespace(&namespace) {
        return protocol("Reserved storage namespace");
    }
    let prefix = match payload.get("prefix") {
        None => String::new(),
        Some(Value::String(prefix)) if char_len(prefix) <= 512 => prefix.clone(),
        Some(_) => return protocol("Invalid prefix in storage request"),
    };
    let limit = integer(payload, "limit", Some(100), Some(1), Some(1000))?;
    let include_value = match payload.get("include_value") {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("include_value must be boolean"),
    };
    let cursor = |key: &str| -> Option<String> {
        match payload.get(key) {
            None => Some(String::new()),
            Some(Value::String(text)) => Some(text.clone()),
            Some(_) => None,
        }
    };
    let (after, before) = match (cursor("after_key"), cursor("before_key")) {
        (Some(after), Some(before)) => (after, before),
        _ => return protocol("Invalid record list cursor"),
    };
    if char_len(&after) > 512
        || char_len(&before) > 512
        || (!after.is_empty() && !before.is_empty())
        || (!after.is_empty() && !after.starts_with(&prefix))
        || (!before.is_empty() && !before.starts_with(&prefix))
    {
        return protocol("Invalid record list cursor");
    }
    let escaped = prefix.replace('\\', "\\\\").replace('_', "\\_").replace('%', "\\%");
    let ordered_key = "record_key";
    let boundary = if !after.is_empty() {
        format!(" AND {ordered_key} > ?")
    } else if !before.is_empty() {
        format!(" AND {ordered_key} < ?")
    } else {
        String::new()
    };
    let direction = if !before.is_empty() { "DESC" } else { "ASC" };
    let mut parameters = vec![SqlValue::from(&namespace), SqlValue::from(escaped + "%")];
    if !boundary.is_empty() {
        parameters.push(SqlValue::from(if after.is_empty() { &before } else { &after }));
    }
    parameters.push(SqlValue::from(limit));
    let scope = "namespace = ? AND record_key LIKE ? ESCAPE '\\' ";
    let value_column = if include_value { "value_json, " } else { "" };
    let rows = session.fetch_all(
        &format!(
            "SELECT record_key, {value_column}version, updated_at_ms FROM storage_records WHERE {scope}{boundary} \
             ORDER BY {ordered_key} {direction} LIMIT ?"
        ),
        &parameters,
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let mut item = Map::new();
        item.insert("key".into(), row.require("record_key")?.to_json()?);
        if include_value {
            item.insert("value".into(), load_record_value(&namespace, row.get("value_json"))?);
        }
        item.insert("version".into(), Value::from(row.int("version")?));
        item.insert("updated_at_ms".into(), Value::from(row.int("updated_at_ms")?));
        result.push(Value::Object(item));
    }
    Ok(Value::Array(result))
}

/// `_guard_automation_draft`.
fn guard_automation_draft(session: &Session<'_>, payload: &Payload, writing: bool) -> OpResult<()> {
    let namespace = payload.get("namespace").unwrap_or(&Value::Null);
    require_hashable(namespace)?;
    if let Value::String(text) = namespace {
        if AUTOMATION_ATOMIC_NAMESPACES.contains(&text.as_str()) {
            return protocol("Automation records require their atomic authority");
        }
    }
    if !eq_str(namespace, AUTOMATION_DEFINITION_NAMESPACE) {
        return Ok(());
    }
    let key = required_text_512(payload, "key")?;
    session.lock_key("automation_definition", &key);
    let current = record_get_value(session, AUTOMATION_DEFINITION_NAMESPACE, &key)?;
    let reserved = ["grant", "triggerState", "publicationCommand"];
    if truthy(&current) {
        let value = &current["value"];
        for field in reserved {
            if !attr_get(value, field)?.is_null() {
                return protocol("Published automation definitions require atomic publication");
            }
        }
    }
    if writing {
        if let Some(Value::Object(value)) = payload.get("value") {
            if reserved.iter().any(|field| !map_get(value, field).is_null()) {
                return protocol("Published automation definitions require atomic publication");
            }
        }
    }
    Ok(())
}

/// `_project_task_result_experiment`.
pub fn project_task_result_experiment(value: &Value) -> Value {
    let Value::Object(source) = value else { return value.clone() };
    let mut projected = source.clone();
    let raw_metadata = projected.get("metadata").cloned().unwrap_or(Value::Null);
    let metadata_was_text = raw_metadata.is_string();
    let mut metadata = if let Value::String(text) = &raw_metadata {
        json::loads(text.as_bytes()).unwrap_or(Value::Null)
    } else {
        raw_metadata
    };
    if metadata.is_object() {
        if let Some(clean) = project_usage_container(&metadata) {
            let stored = if metadata_was_text {
                Value::String(String::from_utf8(json::dumps(&clean)).expect("orjson emits UTF-8"))
            } else {
                clean.clone()
            };
            projected.insert("metadata".into(), stored);
            metadata = clean;
        }
    }
    let outcome = if metadata.is_object() { get(&metadata, "costExperiment") } else { &Value::Null };
    let experiment_id = if outcome.is_object() {
        let primary = get(outcome, "experimentId");
        let chosen = if truthy(primary) { primary } else { get(outcome, "experiment_id") };
        str_or_empty(chosen)
    } else {
        String::new()
    };
    if !experiment_id.is_empty() {
        projected.insert("cost_experiment_id".into(), Value::String(truncate(&experiment_id, 128)));
    } else {
        projected.shift_remove("cost_experiment_id");
    }
    Value::Object(projected)
}

pub fn record_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    guard_dispatch_namespace(payload)?;
    guard_automation_draft(session, payload, true)?;
    record_put_owned(session, payload)
}

fn record_put_owned(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let namespace = required_text(payload, "namespace", 128)?;
    if reserved_namespace(&namespace) {
        return protocol("Reserved storage namespace");
    }
    let key = required_text_512(payload, "key")?;
    let mut value = payload.get("value").cloned().unwrap_or(Value::Null);
    if namespace == "task_results" {
        value = project_task_result_experiment(&value);
        let user_id = if value.is_object() { Some(get(&value, "user_id")) } else { None };
        if worker_root_for_task(session, &key, user_id)?.is_some() {
            return protocol("Durable invocation writes require task_results.checkpoint");
        }
    }
    let encoded = if namespace == "task_results" { encoded_task_result_document(&value)? } else { dump(&value)? };
    let now = now_ms();
    session.lock_key("record", &format!("{namespace}:{key}"));
    if namespace == "task_results" {
        guard_child_record(session, &key, Some(&value))?;
    }
    let current = session.fetch_one(
        "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![&namespace, &key],
    )?;
    let expected = expected_version(payload)?;
    let actual = match current {
        Some(row) => row.int("version")?,
        None => 0,
    };
    if expected.is_some_and(|expected| expected != actual) {
        return fail("database_conflict", "Storage record version conflict");
    }
    let version = actual + 1;
    session.execute(UPSERT_RECORD_SQL, &params![&namespace, &key, encoded, version, now])?;
    Ok(json!({"key": key, "version": version, "updated_at_ms": now}))
}

pub fn record_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    guard_dispatch_namespace(payload)?;
    guard_automation_draft(session, payload, false)?;
    record_delete_owned(session, payload)
}

fn record_delete_owned(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let namespace = required_text(payload, "namespace", 128)?;
    if namespace == "task_results" {
        let key = required_text_512(payload, "key")?;
        if worker_root_for_task(session, &key, None)?.is_some() {
            return protocol("Durable invocation deletion requires its domain owner");
        }
        guard_child_record(session, &key, None)?;
    }
    if reserved_namespace(&namespace) {
        return protocol("Reserved storage namespace");
    }
    let key = required_text_512(payload, "key")?;
    let expected = expected_version(payload)?;
    let count = match expected {
        None => session.execute(
            "DELETE FROM storage_records WHERE namespace = ? AND record_key = ?",
            &params![&namespace, &key],
        )?,
        Some(expected) => {
            let count = session.execute(
                "DELETE FROM storage_records WHERE namespace = ? AND record_key = ? AND version = ?",
                &params![&namespace, &key, expected],
            )?;
            if count == 0 {
                let current = session.fetch_one(
                    "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
                    &params![&namespace, &key],
                )?;
                if current.is_some() {
                    return fail("database_conflict", "Storage record version conflict");
                }
            }
            count
        }
    };
    Ok(json!({"deleted": count > 0}))
}

/// Optional boolean flag with a `False` default.
fn bool_flag(payload: &Payload, key: &str, message: &str) -> OpResult<bool> {
    match payload.get(key) {
        None => Ok(false),
        Some(Value::Bool(flag)) => Ok(*flag),
        Some(_) => protocol(message),
    }
}

/// `metadata` decoded from text when needed; malformed text maps to `fallback`.
fn metadata_mapping(metadata: &Value) -> Option<Value> {
    match metadata {
        Value::String(text) => json::loads(text.as_bytes()).ok(),
        other => Some(other.clone()),
    }
}

pub fn task_results_replay_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let key = required_text_512(payload, "key")?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let include_terminal_payload =
        bool_flag(payload, "include_terminal_payload", "include_terminal_payload must be boolean")?;
    let include_metadata = bool_flag(payload, "include_metadata", "include_metadata must be boolean")?;
    let row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records WHERE namespace=? AND record_key=?",
        &params!["task_results", &key],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    let value = match load_or_decode_error(row.get("value_json")) {
        Ok(value) => value,
        Err(Some(failure)) => return Err(failure),
        Err(None) => return fail("database_integrity", "Stored task result is invalid"),
    };
    let fields: &[&str] =
        if include_terminal_payload { &["metadata", "error", "content", "thinking"] } else { &["metadata", "error"] };
    let value = hydrated_task_result_value(value, Some(fields))?;
    let Value::Object(value) = value else {
        return fail("database_integrity", "Stored task result is not an object");
    };
    let raw_user_id = map_get(&value, "user_id");
    let row_user_id = if raw_user_id.is_boolean() {
        0
    } else {
        match int_or_zero(raw_user_id) {
            Ok(user) => user,
            Err(()) => return Ok(Value::Null),
        }
    };
    if row_user_id != user_id {
        return Ok(Value::Null);
    }
    let task_id_value = map_get(&value, "task_id");
    let stored_task_id = if truthy(task_id_value) { json::python_str(task_id_value) } else { key.clone() };
    if stored_task_id != key {
        return fail("database_integrity", "Stored task-result identity is invalid");
    }
    let metadata = metadata_mapping(map_get(&value, "metadata")).unwrap_or_else(empty_object);
    let metadata = match metadata {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    let replay_metadata = if include_metadata {
        metadata
    } else {
        let mut selected = Map::new();
        for field in ["finishReason", "model", "requestId"] {
            if let Some(item) = metadata.get(field) {
                selected.insert(field.into(), item.clone());
            }
        }
        selected
    };
    let clocks = (int_or_zero(map_get(&value, "created_at")), int_or_zero(map_get(&value, "completed_at")));
    let (Ok(created_at), Ok(completed_at)) = clocks else {
        return fail("database_integrity", "Stored task-result clocks are invalid");
    };
    if map_get(&value, "created_at").is_boolean() || map_get(&value, "completed_at").is_boolean() {
        return fail("database_integrity", "Stored task-result clocks are invalid");
    }
    let status = map_get(&value, "status");
    let mut result = json!({
        "task_id": key,
        "conv_id": str_or_empty(map_get(&value, "conv_id")),
        "user_id": row_user_id,
        "status": if truthy(status) { json::python_str(status) } else { "running".to_owned() },
        "error": map_get(&value, "error").clone(),
        "metadata": Value::Object(replay_metadata),
        "created_at": created_at,
        "completed_at": completed_at,
        "version": row.int("version")?,
        "updated_at_ms": row.int("updated_at_ms")?,
    });
    if include_terminal_payload {
        let or_empty = |field: &str| {
            let item = map_get(&value, field);
            if truthy(item) {
                item.clone()
            } else {
                Value::from("")
            }
        };
        result["content"] = or_empty("content");
        result["thinking"] = or_empty("thinking");
    }
    Ok(result)
}

/// Optional bounded text filter: `None` when absent/null.
fn optional_filter(payload: &Payload, key: &str, maximum: usize, message: &str) -> OpResult<Option<String>> {
    match payload.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) if !text.is_empty() && char_len(text) <= maximum => Ok(Some(text.clone())),
        Some(_) => protocol(message),
    }
}

fn optional_integer(payload: &Payload, key: &str) -> OpResult<Option<i64>> {
    match payload.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => Ok(Some(integer(payload, key, None, Some(if key == "user_id" { 1 } else { 0 }), None)?)),
    }
}

pub fn task_results_summary_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let status = optional_filter(payload, "status", 64, "Invalid status in storage request")?;
    let conv_id = optional_filter(payload, "conv_id", 512, "Invalid conv_id in storage request")?;
    let user_id = optional_integer(payload, "user_id")?;
    let completed_before_ms = optional_integer(payload, "completed_before_ms")?;
    let limit = integer(payload, "limit", Some(100), Some(1), Some(1000))?;
    let scan_limit = integer(payload, "scan_limit", Some(1000), Some(1), Some(10_000))?;
    let order_value = payload.get("order_by").unwrap_or(&Value::Null);
    let order_by = if truthy(order_value) { json::python_str(order_value) } else { "created_at_desc".to_owned() };
    if !["created_at_desc", "completed_at_asc", "updated_at_asc"].contains(&order_by.as_str()) {
        return protocol("Invalid task-result summary order");
    }
    let page_size = 8i64;
    let mut after_key = match payload.get("after_key") {
        None => String::new(),
        Some(Value::String(text)) if char_len(text) <= 1024 => text.clone(),
        Some(_) => return protocol("Invalid task recovery cursor"),
    };
    let mut scanned = 0i64;
    let mut invalid = 0i64;
    let mut summaries: Vec<(i64, i64, i64, String, Value)> = Vec::new();
    let mut exhausted = false;
    let prefilter_sql = if conv_id.is_some() { " AND instr(value_json, ?) > 0" } else { "" };
    let sql = format!(
        "SELECT record_key, value_json, version, updated_at_ms FROM storage_records WHERE namespace=? AND record_key>? \
         {prefilter_sql} ORDER BY record_key LIMIT ?"
    );
    while scanned < scan_limit {
        let mut parameters = vec![SqlValue::from("task_results"), SqlValue::from(&after_key)];
        if let Some(conv_id) = &conv_id {
            parameters.push(SqlValue::from(conv_id));
        }
        parameters.push(SqlValue::from(page_size.min(scan_limit - scanned)));
        let rows = session.fetch_all(&sql, &parameters)?;
        if rows.is_empty() {
            exhausted = true;
            break;
        }
        for row in rows {
            after_key = column_str(row.get("record_key"));
            scanned += 1;
            let value = match load_or_decode_error(row.get("value_json")) {
                Ok(value) => value,
                Err(Some(failure)) => return Err(failure),
                Err(None) => {
                    invalid += 1;
                    continue;
                }
            };
            let Value::Object(value) = value else {
                invalid += 1;
                continue;
            };
            let row_status = str_or_empty(map_get(&value, "status"));
            let row_conv_id = str_or_empty(map_get(&value, "conv_id"));
            let Ok(row_user_id) = int_or_zero(map_get(&value, "user_id")) else {
                invalid += 1;
                continue;
            };
            let (Ok(created_at), Ok(completed_at)) =
                (int_or_zero(map_get(&value, "created_at")), int_or_zero(map_get(&value, "completed_at")))
            else {
                invalid += 1;
                continue;
            };
            if status.as_ref().is_some_and(|status| &row_status != status)
                || conv_id.as_ref().is_some_and(|conv| &row_conv_id != conv)
                || user_id.is_some_and(|user| row_user_id != user)
                || completed_before_ms.is_some_and(|before| completed_at == 0 || completed_at >= before)
            {
                continue;
            }
            let task_value = map_get(&value, "task_id");
            let task_id = if truthy(task_value) { json::python_str(task_value) } else { after_key.clone() };
            let updated_at_ms = row.int("updated_at_ms")?;
            summaries.push((
                created_at,
                completed_at,
                updated_at_ms,
                after_key.clone(),
                json!({
                    "key": after_key,
                    "task_id": task_id,
                    "conv_id": row_conv_id,
                    "user_id": row_user_id,
                    "status": row_status,
                    "created_at": created_at,
                    "completed_at": completed_at,
                    "version": row.int("version")?,
                    "updated_at_ms": updated_at_ms,
                }),
            ));
        }
    }
    match order_by.as_str() {
        "created_at_desc" => summaries.sort_by(|a, b| (b.0, &b.3).cmp(&(a.0, &a.3))),
        "completed_at_asc" => summaries.sort_by(|a, b| (a.1, &a.3).cmp(&(b.1, &b.3))),
        _ => summaries.sort_by(|a, b| (a.2, &a.3).cmp(&(b.2, &b.3))),
    }
    let records: Vec<Value> = summaries.into_iter().take(limit as usize).map(|item| item.4).collect();
    Ok(json!({"records": records, "scanned": scanned, "invalid": invalid, "capped": !exhausted}))
}

/// `_project_cost_experiment_candidate` -> (candidate, invalid-row bit).
/// `(completed_at, task_id, conv_id, outcome)` of one cost-experiment match.
type CostCandidate = (i64, String, String, Value);

fn project_cost_experiment_candidate(
    row: &crate::session::Row,
    cutoff: i64,
    experiment_id: &str,
    experiment_id_ascii: bool,
) -> OpResult<(Option<CostCandidate>, bool)> {
    let updated = match row.get("updated_at_ms") {
        SqlValue::Null => Some(0),
        value if !value.truthy() => Some(0),
        value => value.as_int(),
    };
    let Some(updated) = updated else { return Ok((None, true)) };
    if updated < cutoff {
        return Ok((None, false));
    }
    let raw = row.get("value_json");
    match raw {
        SqlValue::Text(text) if !text.contains(experiment_id) => return Ok((None, false)),
        SqlValue::Blob(bytes) if experiment_id_ascii && !contains_bytes(bytes, experiment_id.as_bytes()) => {
            return Ok((None, false));
        }
        _ => {}
    }
    let value = match load(raw).and_then(|value| hydrated_task_result_value(value, Some(&["metadata"]))) {
        Ok(value) => value,
        Err(Failure::Sqlite(error)) => return Err(Failure::Sqlite(error)),
        Err(_) => return Ok((None, true)),
    };
    let Value::Object(value) = value else { return Ok((None, true)) };
    let completed_at = match strict_int(map_get(&value, "completed_at")) {
        Some(completed_at) if completed_at >= cutoff => completed_at,
        _ => return Ok((None, false)),
    };
    let metadata = match map_get(&value, "metadata") {
        Value::String(text) => match json::loads(text.as_bytes()) {
            Ok(parsed) => parsed,
            Err(_) => return Ok((None, true)),
        },
        other => other.clone(),
    };
    let outcome = if metadata.is_object() { get(&metadata, "costExperiment").clone() } else { Value::Null };
    let Value::Object(outcome_map) = &outcome else { return Ok((None, false)) };
    if outcome_map.is_empty() {
        return Ok((None, false));
    }
    let primary = map_get(outcome_map, "experimentId");
    let chosen = if truthy(primary) { primary } else { map_get(outcome_map, "experiment_id") };
    if str_or_empty(chosen) != experiment_id {
        return Ok((None, false));
    }
    let task_id = column_str(row.get("record_key"));
    Ok((Some((completed_at, task_id, str_or_empty(map_get(&value, "conv_id")), outcome)), false))
}

fn contains_bytes(haystack: &[u8], needle: &[u8]) -> bool {
    needle.is_empty() || haystack.windows(needle.len()).any(|window| window == needle)
}

pub fn task_results_cost_experiment_scan(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let cutoff = integer(payload, "completed_at_gte", None, Some(0), None)?;
    let limit = integer(payload, "limit", Some(5000), Some(1), Some(10000))?;
    let scan_limit = integer(payload, "scan_limit", Some(256), Some(1), Some(COST_EXPERIMENT_SCAN_MAX_ROWS))?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let experiment_id = required_text(payload, "experiment_id", 128)?;
    let mut after_key = match payload.get("after_key") {
        None => String::new(),
        Some(Value::String(text)) if char_len(text) <= 1024 => text.clone(),
        Some(_) => return protocol("Invalid cost-experiment scan cursor"),
    };
    let mut parsed: Vec<(i64, String, String, Value)> = Vec::new();
    let mut invalid = 0i64;
    let mut conv_ids: BTreeSet<String> = BTreeSet::new();
    let ascii = experiment_id.is_ascii();
    let mut scanned = 0i64;
    let mut exhausted = false;
    while scanned < scan_limit {
        let page_limit = COST_EXPERIMENT_SCAN_PAGE_ROWS.min(scan_limit - scanned);
        let page = session.fetch_all(
            "SELECT record_key, value_json, updated_at_ms FROM storage_records \
             WHERE namespace = ? AND record_key > ? ORDER BY record_key LIMIT ?",
            &params!["task_results", &after_key, page_limit],
        )?;
        if page.is_empty() {
            exhausted = true;
            break;
        }
        let page_len = page.len() as i64;
        for row in &page {
            after_key = column_str(row.get("record_key"));
            scanned += 1;
            let (candidate, row_invalid) = project_cost_experiment_candidate(row, cutoff, &experiment_id, ascii)?;
            invalid += row_invalid as i64;
            let Some(candidate) = candidate else { continue };
            if !candidate.2.is_empty() {
                conv_ids.insert(candidate.2.clone());
            }
            parsed.push(candidate);
        }
        if page_len < page_limit {
            exhausted = true;
            break;
        }
    }
    if !exhausted {
        exhausted = session
            .fetch_all(
                "SELECT record_key FROM storage_records WHERE namespace = ? AND record_key > ? \
                 ORDER BY record_key LIMIT 1",
                &params!["task_results", &after_key],
            )?
            .is_empty();
    }
    let mut owned: BTreeSet<String> = BTreeSet::new();
    let sorted: Vec<String> = conv_ids.into_iter().collect();
    for chunk in sorted.chunks(500) {
        let marks = vec!["?"; chunk.len()].join(",");
        let mut parameters = vec![SqlValue::from(user_id)];
        parameters.extend(chunk.iter().map(SqlValue::from));
        let owners = session.fetch_all(
            &format!("SELECT id FROM storage_conversations WHERE user_id = ? AND id IN ({marks})"),
            &parameters,
        )?;
        owned.extend(owners.iter().map(|item| item.get("id").text_or_empty()));
    }
    let mut records: Vec<(i64, String, String, Value)> =
        parsed.into_iter().filter(|item| !item.2.is_empty() && owned.contains(&item.2)).collect();
    records.sort_by(|a, b| (b.0, &b.1).cmp(&(a.0, &a.1)));
    let capped = records.len() as i64 > limit;
    let records: Vec<Value> = records
        .into_iter()
        .take(limit as usize)
        .map(|(completed_at, task_id, conv_id, outcome)| {
            json!({"task_id": task_id, "conv_id": conv_id, "completed_at": completed_at, "outcome": outcome})
        })
        .collect();
    Ok(json!({
        "records": records,
        "invalid": invalid,
        "scanned": scanned,
        "capped": capped,
        "exhausted": exhausted,
        "next_cursor": if exhausted { String::new() } else { after_key },
    }))
}

/// `_task_result_writer_attempt`.
fn task_result_writer_attempt(value: &Value) -> OpResult<String> {
    let hydrated = hydrated_task_result_value(value.clone(), Some(&["metadata"]))?;
    let metadata = attr_get(&hydrated, "metadata")?.clone();
    let metadata = match metadata {
        Value::String(text) => match json::loads(text.as_bytes()) {
            Ok(parsed) => parsed,
            Err(_) => return Ok(String::new()),
        },
        other => other,
    };
    Ok(match get(&metadata, "attemptId") {
        Value::String(text) => text.clone(),
        _ => String::new(),
    })
}

/// `_current_attempt_bound_to_task`.
fn current_attempt_bound_to_task(
    session: &Session<'_>,
    attempt_id: &str,
    task_id: &str,
    user_id: i64,
) -> OpResult<bool> {
    let row = session.fetch_one(
        "SELECT a.task_id, t.current_attempt_id FROM storage_generation_attempts AS a \
         JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id \
         WHERE a.attempt_id=? AND t.user_id=?",
        &params![attempt_id, user_id],
    )?;
    Ok(row.is_some_and(|row| {
        matches!(row.get("task_id"), SqlValue::Text(text) if text == task_id)
            && matches!(row.get("current_attempt_id"), SqlValue::Text(text) if text == attempt_id)
    }))
}

struct CacheRequest {
    requested: bool,
    prefix_hwm: Option<i64>,
    last_read: Option<i64>,
    baseline: Option<Map<String, Value>>,
}

#[allow(clippy::too_many_arguments)]
fn guarded_result(
    key: &str,
    tool_effect_intent: Option<&str>,
    cache: &CacheRequest,
    owned: bool,
    version: i64,
    updated_at_ms: i64,
    committed: bool,
    values: Map<String, Value>,
) -> Value {
    let mut result = Map::new();
    result.insert("key".into(), Value::from(key));
    result.insert("version".into(), Value::from(version));
    result.insert("updated_at_ms".into(), Value::from(updated_at_ms));
    result.insert("owned".into(), Value::Bool(owned));
    result.insert("guard_contract".into(), Value::from(TASK_RESULT_CHECKPOINT_GUARD_CONTRACT));
    if owned {
        if let Some(intent) = tool_effect_intent {
            result.insert("tool_effect_intent".into(), Value::from(intent));
        }
    }
    if cache.requested {
        result.insert("cache_settings_contract".into(), Value::from(TASK_RESULT_CACHE_SETTINGS_CONTRACT));
        result.insert("cache_settings_committed".into(), Value::Bool(committed));
        if committed && !values.is_empty() {
            result.extend(values);
        }
    }
    Value::Object(result)
}

fn valid_cache_int(value: &Value, minimum: i64) -> Option<i64> {
    strict_int(value).filter(|value| minimum <= *value && *value <= TASK_RESULT_CACHE_FACT_MAXIMUM)
}

/// `commit_cache_settings` inside `_task_results_checkpoint`.
fn commit_cache_settings(
    session: &Session<'_>,
    cache: &CacheRequest,
    parent_settings: &mut Option<Map<String, Value>>,
    conversation_id: &str,
    user_id: i64,
    ambiguous_replay: bool,
) -> OpResult<Map<String, Value>> {
    let mut authoritative = Map::new();
    let Some(settings) = parent_settings.as_mut() else { return Ok(authoritative) };
    if !cache.requested {
        return Ok(authoritative);
    }
    let mut changed = false;
    if let Some(requested) = cache.prefix_hwm {
        let current = valid_cache_int(map_get(settings, CONVERSATION_CACHE_PREFIX_HWM_SETTING), 1).unwrap_or(0);
        let merged = current.max(requested);
        if merged != current {
            settings.insert(CONVERSATION_CACHE_PREFIX_HWM_SETTING.into(), Value::from(merged));
            changed = true;
        }
        authoritative.insert(TASK_RESULT_CACHE_PREFIX_HWM_FIELD.into(), Value::from(merged));
    }
    if let Some(requested) = cache.last_read {
        let mut current = valid_cache_int(map_get(settings, CONVERSATION_LAST_TURN_CACHE_READ_SETTING), 0);
        if !ambiguous_replay || current.is_none() {
            if current != Some(requested) {
                settings.insert(CONVERSATION_LAST_TURN_CACHE_READ_SETTING.into(), Value::from(requested));
                changed = true;
            }
            current = Some(requested);
            let current_baseline = map_get(settings, CONVERSATION_CACHE_BASELINE_SETTING).clone();
            let requested_baseline = cache.baseline.clone().map(Value::Object).unwrap_or(Value::Null);
            if !py_eq(&current_baseline, &requested_baseline) {
                if requested_baseline.is_null() {
                    settings.shift_remove(CONVERSATION_CACHE_BASELINE_SETTING);
                } else {
                    settings.insert(CONVERSATION_CACHE_BASELINE_SETTING.into(), requested_baseline);
                }
                changed = true;
            }
        }
        if let Value::Object(stored) = map_get(settings, CONVERSATION_CACHE_BASELINE_SETTING) {
            authoritative.insert(TASK_RESULT_CACHE_BASELINE_FIELD.into(), Value::Object(stored.clone()));
        }
        authoritative.insert(TASK_RESULT_LAST_TURN_CACHE_READ_FIELD.into(), Value::from(current.expect("last read")));
    }
    if changed {
        session.execute(
            "UPDATE storage_conversations SET settings_json=? WHERE id=? AND user_id=?",
            &params![dump_param(&Value::Object(settings.clone()))?, conversation_id, user_id],
        )?;
    }
    Ok(authoritative)
}

pub fn task_results_checkpoint(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let key = required_text_512(payload, "key")?;
    let Some(value @ Value::Object(_)) = payload.get("value") else {
        return protocol("Invalid task result checkpoint");
    };
    let guard_contract = payload.get("guard_contract").unwrap_or(&Value::Null);
    if !guard_contract.is_null() && !eq_str(guard_contract, TASK_RESULT_CHECKPOINT_GUARD_CONTRACT) {
        return protocol("Invalid task result checkpoint guard contract");
    }
    let guarded = eq_str(guard_contract, TASK_RESULT_CHECKPOINT_GUARD_CONTRACT);
    let require_parent = payload.get("require_parent").cloned().unwrap_or(Value::Bool(false));
    let require_parent_true = require_parent == Value::Bool(true);
    let tool_effect_intent = payload.get("tool_effect_intent").unwrap_or(&Value::Null);
    if payload.contains_key("tool_effect_intent")
        && (!guarded || !require_parent_true || !matches!(tool_effect_intent, Value::String(text) if !text.is_empty()))
    {
        return protocol("Invalid child tool effect admission");
    }
    let tool_effect_intent = tool_effect_intent.as_str();
    if guarded && !require_parent.is_boolean() {
        return protocol("Invalid task result parent requirement");
    }
    if !guarded && payload.contains_key("require_parent") {
        return protocol("Task result parent requirement needs a guard contract");
    }
    let Some(expected) = expected_version(payload)? else {
        return protocol("task_results.checkpoint requires expected_version");
    };
    let Value::Object(mut checkpoint_value) = project_task_result_experiment(value) else {
        return protocol("Invalid projected task result");
    };

    let cache_settings_contract = payload.get("cache_settings_contract").unwrap_or(&Value::Null);
    let hwm_present = checkpoint_value.contains_key(TASK_RESULT_CACHE_PREFIX_HWM_FIELD);
    let last_read_present = checkpoint_value.contains_key(TASK_RESULT_LAST_TURN_CACHE_READ_FIELD);
    let requested = hwm_present || last_read_present;
    if !cache_settings_contract.is_null() && !eq_str(cache_settings_contract, TASK_RESULT_CACHE_SETTINGS_CONTRACT) {
        return protocol("Invalid task result cache settings contract");
    }
    if requested {
        if !eq_str(cache_settings_contract, TASK_RESULT_CACHE_SETTINGS_CONTRACT) || !guarded || !require_parent_true {
            return protocol("Task result cache settings need a guarded parent");
        }
    } else if !cache_settings_contract.is_null() {
        return protocol("Task result cache settings contract has no facts");
    }
    let cache_fact = |field: &str, present: bool| -> OpResult<Option<i64>> {
        if !present {
            return Ok(None);
        }
        let minimum = if field == TASK_RESULT_LAST_TURN_CACHE_READ_FIELD { 0 } else { 1 };
        match valid_cache_int(map_get(&checkpoint_value, field), minimum) {
            Some(value) => Ok(Some(value)),
            None => protocol(&format!("Invalid task result cache fact: {field}")),
        }
    };
    let prefix_hwm = cache_fact(TASK_RESULT_CACHE_PREFIX_HWM_FIELD, hwm_present)?;
    let last_read = cache_fact(TASK_RESULT_LAST_TURN_CACHE_READ_FIELD, last_read_present)?;
    let mut baseline = None;
    if let Some(candidate) = checkpoint_value.get(TASK_RESULT_CACHE_BASELINE_FIELD) {
        match candidate {
            Value::Object(map)
                if last_read.is_some()
                    && json::dumps_sorted(candidate).len() <= TASK_RESULT_CACHE_BASELINE_MAXIMUM_BYTES =>
            {
                baseline = Some(map.clone());
            }
            _ => return protocol("Invalid task result cache baseline"),
        }
    }
    let cache = CacheRequest { requested, prefix_hwm, last_read, baseline };
    let result = |owned: bool, version: i64, updated: i64, committed: bool, values: Map<String, Value>| {
        guarded_result(&key, tool_effect_intent, &cache, owned, version, updated, committed, values)
    };

    let mut incoming_user_id = 0i64;
    let mut conversation_id = String::new();
    let mut parent_settings: Option<Map<String, Value>> = None;
    if guarded {
        incoming_user_id = match strict_int(map_get(&checkpoint_value, "user_id")) {
            Some(user) if user >= 1 => user,
            _ => return protocol("Guarded task result checkpoint requires a valid owner"),
        };
        if !eq_str(map_get(&checkpoint_value, "task_id"), &key) {
            return protocol("Guarded task result checkpoint identity is invalid");
        }
        if require_parent_true {
            conversation_id = match map_get(&checkpoint_value, "conv_id") {
                Value::String(text) if !text.is_empty() && char_len(text) <= 512 => text.clone(),
                _ => return protocol("Guarded task result checkpoint requires a parent"),
            };
            session.lock_key("conversation", &format!("{incoming_user_id}:{conversation_id}"));
            let columns = if requested { "settings_json" } else { "1 AS present" };
            let parent = session.fetch_one(
                &format!("SELECT {columns} FROM storage_conversations WHERE id=? AND user_id=?"),
                &params![&conversation_id, incoming_user_id],
            )?;
            let Some(parent) = parent else { return Ok(result(false, 0, 0, false, Map::new())) };
            if requested {
                let loaded = load(parent.get("settings_json"))?;
                let loaded = if truthy(&loaded) { loaded } else { empty_object() };
                let Value::Object(loaded) = loaded else {
                    return fail("database_integrity", "Conversation settings are malformed");
                };
                parent_settings = Some(loaded);
            }
        }
    }
    session.lock_key("task_result", &key);
    let worker_root = worker_root_for_task(session, &key, Some(map_get(&checkpoint_value, "user_id")))?;
    if tool_effect_intent.is_some() && worker_root.as_deref().is_none_or(|root| root == key) {
        return protocol("Tool admission requires a durable worker descendant");
    }
    let worker_claim = payload.get("worker_claim").unwrap_or(&Value::Null);
    if worker_root.is_some() || !worker_claim.is_null() {
        let new_effect = match &worker_root {
            Some(root) if *root != key => starts_new_tool_effect(session, &key, &checkpoint_value, tool_effect_intent)?,
            _ => false,
        };
        let root = worker_root.clone().unwrap_or_else(|| key.clone());
        let attempt_id = root.strip_prefix("conversation-attempt:").unwrap_or(&root).to_owned();
        let owner = integer(&checkpoint_value, "user_id", None, Some(1), None)?;
        let status = map_get(&checkpoint_value, "status");
        let terminal = !new_effect
            && ["done", "error", "aborted", "interrupted"].iter().any(|candidate| eq_str(status, candidate));
        require_conversation_publication_claim(
            session,
            &attempt_id,
            &root,
            owner,
            worker_claim,
            terminal,
            !new_effect,
        )?;
    }
    let current = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params!["task_results", &key],
    )?;
    guard_child_record(session, &key, Some(&Value::Object(checkpoint_value.clone())))?;
    let actual = match &current {
        Some(row) => row.int("version")?,
        None => 0,
    };
    if guarded {
        if let Some(row) = &current {
            let decoded_current = load(row.get("value_json"))?;
            let Value::Object(current_value) = &decoded_current else {
                return fail("database_integrity", "Task result checkpoint is malformed");
            };
            let Some(current_user_id) = strict_int(map_get(current_value, "user_id")) else {
                return fail("database_integrity", "Task result checkpoint owner is malformed");
            };
            if current_user_id != incoming_user_id {
                return Ok(result(false, 0, 0, false, Map::new()));
            }
            let current_status = map_get(current_value, "status").clone();
            let incoming_status = map_get(&checkpoint_value, "status").clone();
            let writer = task_result_writer_attempt(&Value::Object(checkpoint_value.clone()))?;
            let holder = if writer.is_empty() { String::new() } else { task_result_writer_attempt(&decoded_current)? };
            let takeover = !writer.is_empty()
                && writer != holder
                && (!holder.is_empty() || eq_str(&current_status, "interrupted"));
            if takeover && !current_attempt_bound_to_task(session, &writer, &key, incoming_user_id)? {
                return Ok(result(false, 0, 0, false, Map::new()));
            }
            let recovered = eq_str(&current_status, "interrupted");
            let incoming_live = eq_str(&incoming_status, "pending") || eq_str(&incoming_status, "running");
            let current_live =
                current_status.is_null() || eq_str(&current_status, "pending") || eq_str(&current_status, "running");
            if (recovered && !takeover) || (incoming_live && !current_live && !(recovered && takeover)) {
                return Ok(result(false, 0, 0, false, Map::new()));
            }
            for tombstone in ["abort_requested_at", "abort_source"] {
                if let Some(item) = current_value.get(tombstone) {
                    checkpoint_value.insert(tombstone.into(), item.clone());
                }
            }
        }
    }

    let checkpoint = Value::Object(checkpoint_value);
    let encoded = encoded_task_result_document(&checkpoint)?;
    if let Some(row) = &current {
        let current_encoded = match row.get("value_json") {
            SqlValue::Blob(bytes) => Some(bytes.as_slice()),
            _ => None,
        };
        let same_bytes = current_encoded == Some(encoded.as_slice());
        let mut semantic_replay = false;
        if !same_bytes && expected != actual {
            let decoded = hydrated_task_result_value(load(row.get("value_json"))?, None)?;
            semantic_replay = dump(&decoded)? == dump(&checkpoint)?;
        }
        if same_bytes || semantic_replay {
            let updated = row.int("updated_at_ms")?;
            if guarded {
                let values = commit_cache_settings(
                    session,
                    &cache,
                    &mut parent_settings,
                    &conversation_id,
                    incoming_user_id,
                    true,
                )?;
                return Ok(result(true, actual, updated, cache.requested, values));
            }
            return Ok(json!({"key": key, "version": actual, "updated_at_ms": updated}));
        }
    }
    if expected != actual {
        return fail("database_conflict", "Storage record version conflict");
    }
    let now = now_ms();
    let version = actual + 1;
    session.execute(UPSERT_RECORD_SQL, &params!["task_results", &key, encoded, version, now])?;
    if guarded {
        let values =
            commit_cache_settings(session, &cache, &mut parent_settings, &conversation_id, incoming_user_id, false)?;
        return Ok(result(true, version, now, cache.requested, values));
    }
    Ok(json!({"key": key, "version": version, "updated_at_ms": now}))
}

/// `int(value.get("user_id") or 0)` after `_load`, classifying failures as
/// the ownership-metadata integrity error the abort handlers raise.
fn task_owner(raw: &SqlValue) -> OpResult<(Value, i64)> {
    let invalid = || fail("database_integrity", "Task result ownership metadata is invalid");
    let value = match load_or_decode_error(raw) {
        Ok(value) => value,
        Err(Some(failure)) => return Err(failure),
        Err(None) => return invalid(),
    };
    if !value.is_object() {
        return invalid();
    }
    match int_or_zero(get(&value, "user_id")) {
        Ok(owner) => Ok((value, owner)),
        Err(()) => invalid(),
    }
}

pub fn task_results_abort(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let key = required_text_512(payload, "task_id")?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let source = required_text(payload, "source", 128)?;
    session.lock_key("task_result", &key);
    let row = session.fetch_one(
        "SELECT value_json, version FROM storage_records WHERE namespace=? AND record_key=?",
        &params!["task_results", &key],
    )?;
    let Some(row) = row else { return Ok(json!({"signaled": false, "changed": false})) };
    let (value, record_user_id) = task_owner(row.get("value_json"))?;
    let status = get(&value, "status");
    if record_user_id != user_id || !(eq_str(status, "pending") || eq_str(status, "running")) {
        return Ok(json!({"signaled": false, "changed": false}));
    }
    if truthy(get(&value, "abort_requested_at")) {
        return Ok(json!({"signaled": true, "changed": false}));
    }
    let Value::Object(mut updated) = value else { unreachable!("task_owner proved a mapping") };
    let requested_at = now_ms();
    updated.insert("abort_requested_at".into(), Value::from(requested_at));
    updated.insert("abort_source".into(), Value::from(source));
    let version = row.int("version")? + 1;
    session.execute(
        "UPDATE storage_records SET value_json=?, version=?, updated_at_ms=? WHERE namespace=? AND record_key=?",
        &params![dump_param(&Value::Object(updated))?, version, requested_at, "task_results", &key],
    )?;
    Ok(json!({"signaled": true, "changed": true, "version": version, "requested_at_ms": requested_at}))
}

pub fn task_results_abort_requested(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let key = required_text_512(payload, "task_id")?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let row = session.fetch_one(
        "SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?",
        &params!["task_results", &key],
    )?;
    let Some(row) = row else { return Ok(json!({"requested": false})) };
    let (value, record_user_id) = task_owner(row.get("value_json"))?;
    Ok(json!({"requested": record_user_id == user_id && truthy(get(&value, "abort_requested_at"))}))
}

pub fn task_results_recovery_pending(session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    let attempt = session.fetch_one(
        "SELECT 1 AS x FROM storage_generation_attempts \
         WHERE status IN ('pending','running') \
         AND COALESCE(task_id,'') NOT LIKE 'conversation-attempt:%' LIMIT 1",
        &[],
    )?;
    if attempt.is_some() {
        return Ok(json!({"pending": true}));
    }
    let row = session.fetch_one(
        "SELECT 1 AS x FROM storage_records WHERE namespace=? \
         AND record_key NOT LIKE 'conversation-attempt:%' AND \
         json_extract(CAST(value_json AS TEXT), '$.status') IN ('pending','running') LIMIT 1",
        &params!["task_results"],
    )?;
    Ok(json!({"pending": row.is_some()}))
}

pub fn task_results_recover_running(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let reason_value = payload.get("interrupted_reason").unwrap_or(&Value::Null);
    let reason = if truthy(reason_value) { json::python_str(reason_value) } else { "server_restart".to_owned() };
    if !["server_restart", "process_killed", "manual_restart"].contains(&reason.as_str()) {
        return protocol("Invalid task recovery reason");
    }
    let max_rows = integer(payload, "max_rows", Some(32), Some(1), Some(500))?;
    let scan_limit = integer(payload, "scan_limit", Some(10_000), Some(1), Some(100_000))?;
    let now = now_ms();
    let mut after_key = String::new();
    let mut scanned = 0i64;
    let mut recovered: Vec<Value> = Vec::new();
    let mut exhausted = false;
    'scan: while scanned < scan_limit && (recovered.len() as i64) < max_rows {
        let rows = session.fetch_all(
            "SELECT record_key, value_json, version FROM storage_records \
             WHERE namespace=? AND record_key>? \
             AND record_key NOT LIKE 'conversation-attempt:%' AND \
             json_extract(CAST(value_json AS TEXT), '$.status') IN ('pending','running') ORDER BY record_key LIMIT ?",
            &params!["task_results", &after_key, 8i64.min(scan_limit - scanned)],
        )?;
        if rows.is_empty() {
            exhausted = true;
            break;
        }
        for row in rows {
            after_key = column_str(row.get("record_key"));
            scanned += 1;
            let value = match load_or_decode_error(row.get("value_json")) {
                Ok(value) => value,
                Err(Some(failure)) => return Err(failure),
                Err(None) => continue,
            };
            let Value::Object(mut document) = value else { continue };
            let status = map_get(&document, "status");
            require_hashable(status)?;
            if !(eq_str(status, "pending") || eq_str(status, "running")) {
                continue;
            }
            document.insert("status".into(), Value::from("interrupted"));
            document.insert("interruptedReason".into(), Value::from(reason.as_str()));
            let completed = map_get(&document, "completed_at");
            let completed_at = if truthy(completed) {
                match int(completed) {
                    Ok(value) => value,
                    Err(()) => return internal("ValueError: invalid completed_at"),
                }
            } else {
                now
            };
            document.insert("completed_at".into(), Value::from(completed_at));
            let previous = row.int("version")?;
            let version = previous + 1;
            let task_value = map_get(&document, "task_id");
            let task_id = if truthy(task_value) { json::python_str(task_value) } else { after_key.clone() };
            let conversation_id = str_or_empty(map_get(&document, "conv_id"));
            session.execute(
                "UPDATE storage_records SET value_json=?, version=?, \
                 updated_at_ms=? WHERE namespace=? AND record_key=? AND version=?",
                &params![dump_param(&Value::Object(document))?, version, now, "task_results", &after_key, previous],
            )?;
            recovered.push(json!({"taskId": task_id, "conversationId": conversation_id}));
            if recovered.len() as i64 >= max_rows {
                break 'scan;
            }
        }
    }
    Ok(json!({"recovered": recovered, "scanned": scanned, "nextKey": after_key, "remaining": !exhausted}))
}
