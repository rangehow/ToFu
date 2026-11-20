//! `operation_domains/plugins.py`: plugin manifest registration and the
//! manifest-declared `plugin.<namespace>.<operation>` row operations
//! (`operations_pkg/_plugins.py`, `lib/storage/manifest.py`).

use std::collections::BTreeSet;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::{register as add, Kind, OperationSpec, Registry};
use crate::codec::{self, py_eq};
use crate::error::{fail, internal, OpResult, StorageError};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{expected_version, integer, required_text, required_text_512, Payload};
use crate::session::{Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "plugin.register", OperationSpec::command(true, plugin_register));
    add(registry, "plugin.manifest.get", OperationSpec::query(plugin_manifest_get));
}

fn wall_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or(0)
}

fn name_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[a-z][a-z0-9_]{0,62}$").expect("name regex"))
}

fn namespace_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[a-z][a-z0-9_.\-]{2,127}$").expect("namespace regex"))
}

const TYPES: &[&str] = &["string", "integer", "number", "boolean", "json", "bytes", "timestamp"];
const ACTIONS: &[&str] = &["get", "list", "put", "delete", "batch", "legacy_scan"];

/// Python `str(value or '')`.
fn str_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(value) if truthy(value) => python_str(value),
        _ => String::new(),
    }
}

/// `manifest._name`; `None` is `ManifestError`.
fn manifest_name(value: Option<&Value>) -> Option<String> {
    let value = str_or_empty(value);
    name_re().is_match(&value).then_some(value)
}

/// Python `isinstance(value, int)` as an integer (bool included).
fn python_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(flag) => Some(*flag as i64),
        Value::Number(number) => number.as_i64(),
        _ => None,
    }
}

/// `value or []` iterated as a list; `None` when iteration cannot yield
/// objects (every such case raises `ManifestError`/`TypeError`).
fn list_or_empty(value: Option<&Value>) -> Option<Vec<Value>> {
    match value {
        None => Some(Vec::new()),
        Some(value) if !truthy(value) => Some(Vec::new()),
        Some(Value::Array(items)) => Some(items.clone()),
        Some(_) => None,
    }
}

/// `lib/storage/manifest.py::validate_manifest`; `None` is a
/// `ManifestError`/`TypeError` (one wire failure for every reason).
fn validate_manifest(value: Option<&Value>) -> Option<Value> {
    let Some(Value::Object(value)) = value else { return None };
    let namespace = str_or_empty(value.get("namespace"));
    if !namespace_re().is_match(&namespace) {
        return None;
    }
    let version = value.get("version")?;
    let version = version.as_i64().filter(|v| *v >= 1 && version.is_number())?;
    let Some(Value::Array(tables_in)) = value.get("tables") else { return None };
    let Some(Value::Array(operations_in)) = value.get("operations") else { return None };
    if tables_in.is_empty() || operations_in.is_empty() {
        return None;
    }
    let mut tables = Vec::new();
    let mut table_names = BTreeSet::new();
    for raw_table in tables_in {
        let Value::Object(raw_table) = raw_table else { return None };
        let table_name = manifest_name(raw_table.get("name"))?;
        if table_names.contains(&table_name) {
            return None;
        }
        let Some(Value::Array(columns_in)) = raw_table.get("columns") else { return None };
        if columns_in.is_empty() {
            return None;
        }
        let mut columns = Vec::new();
        let mut column_names = BTreeSet::new();
        for raw_column in columns_in {
            let Value::Object(raw_column) = raw_column else { return None };
            let column_name = manifest_name(raw_column.get("name"))?;
            let column_type = str_or_empty(raw_column.get("type"));
            if column_names.contains(&column_name) || !TYPES.contains(&column_type.as_str()) {
                return None;
            }
            column_names.insert(column_name.clone());
            columns.push(json!({
                "name": column_name,
                "type": column_type,
                "required": raw_column.get("required").is_some_and(truthy),
            }));
        }
        let Some(Value::Array(primary_key)) = raw_table.get("primary_key") else { return None };
        if primary_key.len() != 1 {
            return None;
        }
        let primary = manifest_name(primary_key.first())?;
        if !column_names.contains(&primary) {
            return None;
        }
        let mut indexes = Vec::new();
        for raw_index in list_or_empty(raw_table.get("indexes"))? {
            let Value::Object(raw_index) = raw_index else { return None };
            let index_name = manifest_name(raw_index.get("name"))?;
            let Some(Value::Array(index_columns)) = raw_index.get("columns") else { return None };
            if index_columns.is_empty() || index_columns.iter().any(|c| !column_names.contains(&python_str(c))) {
                return None;
            }
            indexes.push(json!({
                "name": index_name,
                "columns": index_columns.iter().map(python_str).collect::<Vec<_>>(),
                "unique": raw_index.get("unique").is_some_and(truthy),
            }));
        }
        table_names.insert(table_name.clone());
        tables.push(json!({
            "name": table_name,
            "columns": columns,
            "primary_key": [primary],
            "indexes": indexes,
        }));
    }
    let mut operations = Vec::new();
    let mut operation_names = BTreeSet::new();
    for raw_operation in operations_in {
        let Value::Object(raw_operation) = raw_operation else { return None };
        let name = manifest_name(raw_operation.get("name"))?;
        let action = str_or_empty(raw_operation.get("action"));
        let table_name = manifest_name(raw_operation.get("table"))?;
        if operation_names.contains(&name) || !ACTIONS.contains(&action.as_str()) || !table_names.contains(&table_name)
        {
            return None;
        }
        operation_names.insert(name.clone());
        let expected_kind = if matches!(action.as_str(), "get" | "list" | "legacy_scan") { "query" } else { "command" };
        let kind = match raw_operation.get("kind") {
            Some(kind) if truthy(kind) => python_str(kind),
            _ => expected_kind.to_owned(),
        };
        if kind != expected_kind {
            return None;
        }
        let limit_max = raw_operation.get("limit_max").cloned().unwrap_or(Value::from(100));
        if !python_int(&limit_max).is_some_and(|limit| (1..=1000).contains(&limit)) {
            return None;
        }
        let mut operation = Map::new();
        operation.insert("name".into(), Value::String(name.clone()));
        operation.insert("kind".into(), Value::String(kind));
        operation.insert("action".into(), Value::String(action.clone()));
        operation.insert("table".into(), Value::String(table_name));
        operation.insert("limit_max".into(), limit_max);
        if action == "legacy_scan" {
            let legacy_table = manifest_name(raw_operation.get("legacy_table"))?;
            let Some(Value::Array(legacy_columns_in)) = raw_operation.get("legacy_columns") else { return None };
            if legacy_columns_in.is_empty() {
                return None;
            }
            let legacy_columns =
                legacy_columns_in.iter().map(|column| manifest_name(Some(column))).collect::<Option<Vec<_>>>()?;
            if legacy_columns.iter().collect::<BTreeSet<_>>().len() != legacy_columns.len() {
                return None;
            }
            let legacy_order_in = match raw_operation.get("legacy_order_by") {
                Some(value) if truthy(value) => match value {
                    Value::Array(items) => items.clone(),
                    _ => return None,
                },
                _ => Vec::new(),
            };
            if legacy_order_in
                .iter()
                .any(|column| !column.as_str().is_some_and(|c| legacy_columns.iter().any(|l| l == c)))
            {
                return None;
            }
            operation.insert("legacy_table".into(), Value::String(legacy_table));
            operation.insert("legacy_columns".into(), json!(legacy_columns));
            operation.insert("legacy_order_by".into(), Value::Array(legacy_order_in));
        }
        operations.push(Value::Object(operation));
    }
    Some(json!({"namespace": namespace, "version": version, "tables": tables, "operations": operations}))
}

/// `lib/storage/manifest.py::validate_document`; `None` is `ManifestError`.
fn validate_document(table: &Value, value: Option<&Value>) -> Option<Map<String, Value>> {
    let Some(Value::Object(document)) = value else { return None };
    let columns = table["columns"].as_array()?;
    let declared: BTreeSet<&str> = columns.iter().filter_map(|c| c["name"].as_str()).collect();
    if document.keys().any(|key| !declared.contains(key.as_str())) {
        return None;
    }
    for column in columns {
        let name = column["name"].as_str()?;
        if truthy(&column["required"]) && !document.contains_key(name) {
            return None;
        }
        let item = match document.get(name) {
            None | Some(Value::Null) => continue,
            Some(item) => item,
        };
        let valid = match column["type"].as_str()? {
            "string" | "bytes" => item.is_string(),
            "integer" => item.is_i64() || item.is_u64(),
            "number" => item.is_number(),
            "boolean" => item.is_boolean(),
            "json" => true,
            "timestamp" => item.is_number() || item.is_string(),
            _ => false,
        };
        if !valid {
            return None;
        }
    }
    Some(document.clone())
}

fn incompatible(message: &str) -> StorageError {
    StorageError::new("plugin_storage_incompatible", message)
}

fn by_name(items: &Value) -> Vec<(String, &Value)> {
    items
        .as_array()
        .map(|items| items.iter().map(|item| (python_str(&item["name"]), item)).collect())
        .unwrap_or_default()
}

fn lookup<'v>(items: &'v [(String, &'v Value)], name: &str) -> Option<&'v Value> {
    items.iter().rev().find(|(key, _)| key == name).map(|(_, item)| *item)
}

/// `_plugin_register`.
fn plugin_register(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let Some(manifest) = validate_manifest(payload.get("manifest")) else {
        return Err(incompatible("Plugin storage manifest is incompatible").into());
    };
    let namespace = manifest["namespace"].as_str().expect("validated namespace").to_owned();
    let version = manifest["version"].as_i64().expect("validated version");
    let encoded = codec::dump(&manifest)?;
    let current = session.fetch_one(
        "SELECT manifest_version, manifest_json FROM storage_plugin_manifests WHERE namespace = ?",
        &params![&namespace],
    )?;
    if let Some(current) = current {
        let current_version = current.int("manifest_version")?;
        if version < current_version {
            return Err(incompatible("Plugin storage version moved backwards").into());
        }
        if version == current_version && current.get("manifest_json").bytes() != Some(encoded.as_slice()) {
            return Err(incompatible("Plugin storage version was redefined").into());
        }
        if version > current_version {
            let previous = codec::load(current.require("manifest_json")?)?;
            let previous_tables = by_name(&previous["tables"]);
            let next_tables = by_name(&manifest["tables"]);
            let previous_operations = by_name(&previous["operations"]);
            let next_operations = by_name(&manifest["operations"]);
            let mut is_incompatible = false;
            let mut seen = BTreeSet::new();
            for (name, table) in previous_tables.iter().rev() {
                if !seen.insert(name.clone()) {
                    continue;
                }
                let Some(upgraded) = lookup(&next_tables, name) else {
                    is_incompatible = true;
                    break;
                };
                let empty = Vec::new();
                let old_columns = table["columns"].as_array().unwrap_or(&empty);
                let new_columns = upgraded["columns"].as_array().unwrap_or(&empty);
                let no_indexes = Value::Array(Vec::new());
                let old_indexes = by_name(table.get("indexes").unwrap_or(&no_indexes));
                let new_indexes = by_name(upgraded.get("indexes").unwrap_or(&no_indexes));
                let prefix = &new_columns[..old_columns.len().min(new_columns.len())];
                let columns_changed = !py_eq(&Value::Array(prefix.to_vec()), &Value::Array(old_columns.clone()));
                let index_changed = old_indexes.iter().any(|(index, definition)| {
                    !lookup(&new_indexes, index).is_some_and(|candidate| py_eq(candidate, definition))
                });
                let added_required =
                    new_columns.iter().skip(old_columns.len()).any(|item| item.get("required").is_some_and(truthy));
                let added_unique = upgraded["indexes"].as_array().unwrap_or(&empty).iter().any(|item| {
                    item.get("unique").is_some_and(truthy) && lookup(&old_indexes, &python_str(&item["name"])).is_none()
                });
                if columns_changed
                    || !py_eq(&upgraded["primary_key"], &table["primary_key"])
                    || index_changed
                    || added_required
                    || added_unique
                {
                    is_incompatible = true;
                    break;
                }
            }
            is_incompatible = is_incompatible
                || previous_operations.iter().any(|(name, operation)| {
                    !lookup(&next_operations, name).is_some_and(|candidate| py_eq(candidate, operation))
                });
            if is_incompatible {
                return Err(incompatible("Plugin storage migration is not append-only compatible").into());
            }
        }
    }
    let now = wall_ms();
    session.execute(
        "INSERT INTO storage_plugin_manifests(namespace, manifest_version, manifest_json, updated_at_ms) \
         VALUES (?, ?, ?, ?) ON CONFLICT(namespace) DO UPDATE SET \
         manifest_version = excluded.manifest_version, manifest_json = excluded.manifest_json, \
         updated_at_ms = excluded.updated_at_ms",
        &params![&namespace, version, encoded, now],
    )?;
    Ok(json!({"namespace": namespace, "version": version}))
}

/// `_plugin_manifest_get`.
fn plugin_manifest_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let namespace = required_text(payload, "namespace", 128)?;
    let row = session
        .fetch_one("SELECT manifest_json FROM storage_plugin_manifests WHERE namespace = ?", &params![namespace])?;
    match row {
        Some(row) => codec::load(row.require("manifest_json")?),
        None => Ok(Value::Null),
    }
}

fn dynamic_noop(_session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    Ok(Value::Null)
}

fn dynamic_query(session: &Session<'_>, operation: &str, payload: &Payload, _result: Value) -> OpResult<Value> {
    plugin_dynamic(session, operation, "query", payload)
}

fn dynamic_command(session: &Session<'_>, operation: &str, payload: &Payload, _result: Value) -> OpResult<Value> {
    plugin_dynamic(session, operation, "command", payload)
}

fn dynamic_maintenance(session: &Session<'_>, operation: &str, payload: &Payload, _result: Value) -> OpResult<Value> {
    plugin_dynamic(session, operation, "maintenance", payload)
}

/// `operations.resolve_operation_contract`'s `plugin.` fallback: receipted
/// exactly when the request kind is `command`, no transaction override. The
/// handler receives no operation name, so the work runs in `after`, which
/// does (still before COMMIT, inside the same transaction).
pub fn dynamic(operation: &str, kind: &str) -> Option<OperationSpec> {
    if !operation.starts_with("plugin.") {
        return None;
    }
    let (kind, after): (Kind, super::After) = match kind {
        "query" => (Kind::Query, dynamic_query),
        "command" => (Kind::Command, dynamic_command),
        "maintenance" => (Kind::Maintenance, dynamic_maintenance),
        _ => return None,
    };
    Some(OperationSpec {
        kind,
        receipt_required: kind == Kind::Command,
        handler: dynamic_noop,
        after: Some(after),
        transaction_timeout_s: None,
        receipt_when_true: None,
    })
}

/// `_plugin_context`.
fn plugin_context(session: &Session<'_>, operation: &str) -> OpResult<(String, Value, Value)> {
    let parts: Vec<&str> = operation.split('.').collect();
    if parts.len() < 3 || parts[0] != "plugin" {
        return fail("database_protocol_error", "Unknown storage operation");
    }
    let namespace = parts[1..parts.len() - 1].join(".");
    let operation_name = parts[parts.len() - 1];
    let row = session
        .fetch_one("SELECT manifest_json FROM storage_plugin_manifests WHERE namespace = ?", &params![&namespace])?;
    let Some(row) = row else {
        return Err(incompatible("Plugin storage namespace is not registered").into());
    };
    let manifest = codec::load(row.require("manifest_json")?)?;
    let empty = Vec::new();
    let Some(spec) =
        manifest["operations"].as_array().unwrap_or(&empty).iter().find(|item| item["name"] == operation_name).cloned()
    else {
        return fail("database_protocol_error", "Unknown plugin storage operation");
    };
    let Some(table) = manifest["tables"]
        .as_array()
        .unwrap_or(&empty)
        .iter()
        .find(|item| py_eq(&item["name"], &spec["table"]))
        .cloned()
    else {
        return internal("StopIteration");
    };
    Ok((namespace, spec, table))
}

fn limit_max(spec: &Value) -> i64 {
    python_int(&spec["limit_max"]).unwrap_or(0)
}

fn row_document(row: &crate::session::Row) -> OpResult<Value> {
    Ok(json!({
        "document": codec::load(row.require("document_json")?)?,
        "version": row.int("version")?,
        "updated_at_ms": row.int("updated_at_ms")?,
    }))
}

/// `_plugin_dynamic`.
fn plugin_dynamic(session: &Session<'_>, operation: &str, kind: &str, payload: &Payload) -> OpResult<Value> {
    let (namespace, spec, table) = plugin_context(session, operation)?;
    if spec["kind"].as_str() != Some(kind) {
        return fail("database_protocol_error", "Plugin operation kind mismatch");
    }
    let action = python_str(&spec["action"]);
    let table_name = python_str(&table["name"]);
    let primary_key = python_str(&table["primary_key"][0]);
    let mut key = String::new();
    if action == "get" || action == "delete" {
        key = required_text_512(payload, &primary_key)?;
    }
    match action.as_str() {
        "get" => {
            let row = session.fetch_one(
                "SELECT document_json, version, updated_at_ms FROM storage_plugin_rows \
                 WHERE namespace = ? AND table_name = ? AND row_key = ?",
                &params![&namespace, &table_name, &key],
            )?;
            match row {
                Some(row) => row_document(&row),
                None => Ok(Value::Null),
            }
        }
        "list" => {
            let limit = integer(payload, "limit", Some(100), Some(1), Some(limit_max(&spec)))?;
            let key_prefix = payload.get("key_prefix").cloned().unwrap_or(Value::String(String::new()));
            let after_key = payload.get("after_key").cloned().unwrap_or(Value::String(String::new()));
            let (Value::String(key_prefix), Value::String(after_key)) = (key_prefix, after_key) else {
                return fail("database_protocol_error", "Plugin list cursor is invalid");
            };
            if key_prefix.chars().count() > 512
                || after_key.chars().count() > 512
                || (!key_prefix.is_empty() && !after_key.is_empty() && !after_key.starts_with(&key_prefix))
            {
                return fail("database_protocol_error", "Plugin list cursor is invalid");
            }
            let filters = match payload.get("filters") {
                Some(value) if truthy(value) => match value {
                    Value::Object(filters) => filters.clone(),
                    _ => return fail("database_protocol_error", "Plugin filters must be an object"),
                },
                _ => Map::new(),
            };
            let declared: BTreeSet<String> = table["columns"]
                .as_array()
                .map(|columns| columns.iter().map(|column| python_str(&column["name"])).collect())
                .unwrap_or_default();
            if filters.keys().any(|key| !declared.contains(key)) {
                return fail("database_protocol_error", "Plugin filter is undeclared");
            }
            let read_limit = if !key_prefix.is_empty() { limit } else { 1000.min((limit * 10).max(limit)) };
            let start = if after_key.is_empty() { &key_prefix } else { &after_key };
            let rows = session.fetch_all(
                "SELECT document_json, version, updated_at_ms FROM storage_plugin_rows \
                 WHERE namespace = ? AND table_name = ? AND row_key > ? \
                 ORDER BY row_key LIMIT ?",
                &params![&namespace, &table_name, start, read_limit],
            )?;
            let mut result = Vec::new();
            for row in &rows {
                let document = codec::load(row.require("document_json")?)?;
                let Value::Object(document_map) = &document else {
                    return internal("AttributeError: get");
                };
                if !key_prefix.is_empty() {
                    let key_value = document_map.get(&primary_key).cloned().unwrap_or(Value::String(String::new()));
                    if !python_str(&key_value).starts_with(&key_prefix) {
                        break;
                    }
                }
                if filters.iter().all(|(key, value)| py_eq(document_map.get(key).unwrap_or(&Value::Null), value)) {
                    result.push(json!({
                        "document": document,
                        "version": row.int("version")?,
                        "updated_at_ms": row.int("updated_at_ms")?,
                    }));
                    if result.len() as i64 >= limit {
                        break;
                    }
                }
            }
            Ok(Value::Array(result))
        }
        "put" => put_document(session, &namespace, &table_name, &table, &primary_key, payload),
        "delete" => delete_document(session, &namespace, &table_name, &primary_key, payload),
        "batch" => {
            let mutations = match payload.get("mutations") {
                Some(Value::Array(items)) if !items.is_empty() && items.len() as i64 <= limit_max(&spec) => items,
                _ => return fail("database_protocol_error", "Plugin batch must contain a bounded mutation list"),
            };
            let mut results = Vec::new();
            let mut seen_keys = BTreeSet::new();
            for mutation in mutations {
                let Value::Object(mutation) = mutation else {
                    return fail("database_protocol_error", "Plugin mutation must be an object");
                };
                let mutation_action = str_or_empty(mutation.get("action"));
                let key_value = match mutation_action.as_str() {
                    "put" => match mutation.get("document") {
                        Some(Value::Object(document)) => document.get(&primary_key).cloned(),
                        _ => None,
                    },
                    "delete" => mutation.get(&primary_key).cloned(),
                    _ => return fail("database_protocol_error", "Unsupported plugin batch mutation"),
                };
                let key = match key_value {
                    Some(Value::String(text)) => text,
                    Some(Value::Number(number)) if number.is_i64() || number.is_u64() => number.to_string(),
                    _ => return fail("database_protocol_error", "Plugin batch keys must be strings or integers"),
                };
                if key.is_empty() || seen_keys.contains(&key) {
                    return fail("database_protocol_error", "Plugin batch keys must be non-empty and unique");
                }
                seen_keys.insert(key);
                let result = if mutation_action == "put" {
                    put_document(session, &namespace, &table_name, &table, &primary_key, mutation)?
                } else {
                    delete_document(session, &namespace, &table_name, &primary_key, mutation)?
                };
                results.push(result);
            }
            Ok(json!({"results": results}))
        }
        "legacy_scan" => legacy_scan(session, &spec, payload),
        _ => fail("database_protocol_error", "Unsupported plugin action"),
    }
}

/// `_plugin_put_document`.
fn put_document(
    session: &Session<'_>,
    namespace: &str,
    table_name: &str,
    table: &Value,
    primary_key: &str,
    payload: &Payload,
) -> OpResult<Value> {
    let Some(document) = validate_document(table, payload.get("document")) else {
        return Err(incompatible("Plugin document violates its manifest").into());
    };
    let key = match document.get(primary_key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) if number.is_i64() || number.is_u64() => number.to_string(),
        _ => return Err(incompatible("Plugin primary key is invalid").into()),
    };
    let current = session.fetch_one(
        "SELECT version FROM storage_plugin_rows WHERE namespace = ? AND table_name = ? AND row_key = ?",
        &params![namespace, table_name, &key],
    )?;
    let actual = match &current {
        Some(row) => row.int("version")?,
        None => 0,
    };
    let expected = expected_version(payload)?;
    if expected.is_some_and(|expected| expected != actual) {
        return fail("database_conflict", "Plugin row version conflict");
    }
    let version = actual + 1;
    let now = wall_ms();
    let mut unique_values = Vec::new();
    let empty = Vec::new();
    for index in table["indexes"].as_array().unwrap_or(&empty) {
        if !index.get("unique").is_some_and(truthy) {
            continue;
        }
        let values: Vec<Value> = index["columns"]
            .as_array()
            .unwrap_or(&empty)
            .iter()
            .map(|column| document.get(&python_str(column)).cloned().unwrap_or(Value::Null))
            .collect();
        if values.iter().any(Value::is_null) {
            continue;
        }
        let unique_value = hex::encode(Sha256::digest(codec::dump(&Value::Array(values))?));
        let index_name = python_str(&index["name"]);
        let owner = session.fetch_one(
            "SELECT row_key FROM storage_plugin_unique_values \
             WHERE namespace = ? AND table_name = ? AND index_name = ? \
             AND index_value = ?",
            &params![namespace, table_name, &index_name, &unique_value],
        )?;
        if owner.is_some_and(|owner| owner.get("row_key").as_str() != Some(key.as_str())) {
            return fail("database_conflict", "Plugin unique constraint conflict");
        }
        unique_values.push((index_name, unique_value));
    }
    session.execute(
        "DELETE FROM storage_plugin_unique_values WHERE namespace = ? AND table_name = ? AND row_key = ?",
        &params![namespace, table_name, &key],
    )?;
    for (index_name, unique_value) in &unique_values {
        session.execute(
            "INSERT INTO storage_plugin_unique_values(namespace, table_name, index_name, index_value, row_key) \
             VALUES (?, ?, ?, ?, ?)",
            &params![namespace, table_name, index_name, unique_value, &key],
        )?;
    }
    session.execute(
        "INSERT INTO storage_plugin_rows(namespace, table_name, row_key, document_json, version, updated_at_ms) \
         VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(namespace, table_name, row_key) DO UPDATE SET \
         document_json = excluded.document_json, version = excluded.version, \
         updated_at_ms = excluded.updated_at_ms",
        &params![namespace, table_name, &key, codec::dump(&Value::Object(document))?, version, now],
    )?;
    Ok(json!({"key": key, "version": version, "updated_at_ms": now}))
}

/// `_plugin_delete_document`.
fn delete_document(
    session: &Session<'_>,
    namespace: &str,
    table_name: &str,
    primary_key: &str,
    payload: &Payload,
) -> OpResult<Value> {
    let key = required_text_512(payload, primary_key)?;
    if let Some(expected) = expected_version(payload)? {
        let current = session.fetch_one(
            "SELECT version FROM storage_plugin_rows WHERE namespace = ? AND table_name = ? AND row_key = ?",
            &params![namespace, table_name, &key],
        )?;
        let actual = match &current {
            Some(row) => row.int("version")?,
            None => 0,
        };
        if expected != actual {
            return fail("database_conflict", "Plugin row version conflict");
        }
    }
    session.execute(
        "DELETE FROM storage_plugin_unique_values WHERE namespace = ? AND table_name = ? AND row_key = ?",
        &params![namespace, table_name, &key],
    )?;
    let count = session.execute(
        "DELETE FROM storage_plugin_rows WHERE namespace = ? AND table_name = ? AND row_key = ?",
        &params![namespace, table_name, &key],
    )?;
    Ok(json!({"key": key, "deleted": count != 0}))
}

/// `_plugin_legacy_scan`.
fn legacy_scan(session: &Session<'_>, spec: &Value, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(100), Some(1), Some(limit_max(spec)))?;
    let offset = integer(payload, "offset", Some(0), Some(0), Some(10_000_000))?;
    let legacy_table = python_str(&spec["legacy_table"]);
    let exists = session.fetch_one(
        "SELECT 1 AS present FROM sqlite_master WHERE type = 'table' AND name = ?",
        &params![&legacy_table],
    )?;
    if exists.is_none() {
        return Ok(json!({"exists": false, "rows": [], "next_offset": null}));
    }
    let columns: Vec<String> =
        spec["legacy_columns"].as_array().map(|items| items.iter().map(python_str).collect()).unwrap_or_default();
    let quoted_columns = columns.iter().map(|column| format!("\"{column}\"")).collect::<Vec<_>>().join(", ");
    let order_columns: Vec<String> = match spec.get("legacy_order_by") {
        Some(Value::Array(items)) if !items.is_empty() => items.iter().map(python_str).collect(),
        _ => columns.iter().take(1).cloned().collect(),
    };
    let quoted_order = order_columns.iter().map(|column| format!("\"{column}\"")).collect::<Vec<_>>().join(", ");
    let rows = session.fetch_all(
        &format!("SELECT {quoted_columns} FROM \"{legacy_table}\" ORDER BY {quoted_order} LIMIT ? OFFSET ?"),
        &params![limit, offset],
    )?;
    let mut documents = Vec::new();
    for row in &rows {
        let mut document = Map::new();
        for column in &columns {
            let value = match row.require(column)? {
                SqlValue::Blob(bytes) => codec::wire_bytes(bytes),
                other => other.to_json()?,
            };
            document.insert(column.clone(), value);
        }
        documents.push(Value::Object(document));
    }
    let next_offset =
        if documents.len() as i64 == limit { Value::from(offset + documents.len() as i64) } else { Value::Null };
    Ok(json!({"exists": true, "rows": documents, "next_offset": next_offset}))
}
