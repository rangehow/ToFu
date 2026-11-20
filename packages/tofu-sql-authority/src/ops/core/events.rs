//! `operations_pkg/_records.py`: the durable task-event log and the
//! rate-limit admission bucket.

use std::collections::BTreeSet;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::codecs::{
    decode_task_event_payload, encode_task_event_payload, execution_event_time_ms, project_request_inspector_event,
    project_usage_container, stamp_event_format_envelope, REQUEST_INSPECTOR_PROJECTION,
};
use super::invocations::{require_conversation_publication_claim, starts_tool_effect, worker_root_for_event_stream};
use super::py::{eq_str, get, str_or_empty, truncate};
use super::records::{column_str, now_ms};
use crate::codec::{dump, load};
use crate::error::{fail, Failure, OpResult};
use crate::json::{self, truthy};
use crate::params;
use crate::payload::{integer, required_text, required_text_512, Payload};
use crate::session::{Session, SqlValue};

/// `task_event_contract.TASK_STREAM_KIND`.
const TASK_STREAM_KIND: &str = "task";
const EVENT_READ_GUARD_CONTRACT: &str = "event-read-guard/v1";
const EVENT_READ_GUARD_SCOPED_CONTRACT: &str = "event-read-guard/v2";
const EVENT_READ_GUARD_MAX_TYPES: usize = 64;
/// `task_event_contract.STRUCTURAL_EVENT_TYPES`, sorted.
const STRUCTURAL_EVENT_TYPES: [&str; 6] =
    ["flow_iteration", "messages_snapshot", "round_end", "round_start", "round_usage", "tool_wire_projection"];
/// `lib.orchestration.message_compat.LEGACY_FLOW_EVENT_PREFIX`.
const LEGACY_FLOW_EVENT_PREFIX: &str = "endpoint_";
/// `schema.LEGACY_TASK_EVENT_RETENTION_INDEX_NAME`.
const LEGACY_TASK_EVENT_RETENTION_INDEX_NAME: &str = "idx_storage_events_retention";
const RATE_LIMIT_PRUNE_BATCH: i64 = 256;

/// `schema.TASK_EVENT_RETENTION_SPECS`.
fn retention_spec(class: &str) -> Option<(&'static str, &'static str)> {
    match class {
        "streaming" => Some((
            "idx_storage_events_streaming_retention_v2",
            "stream_kind = 'task' AND event_type NOT IN ('flow_iteration','messages_snapshot','round_end','round_start','round_usage','tool_wire_projection') AND event_type <> ''",
        )),
        "structural" => Some((
            "idx_storage_events_structural_retention_v2",
            "stream_kind = 'task' AND (event_type IN ('flow_iteration','messages_snapshot','round_end','round_start','round_usage','tool_wire_projection') OR event_type = '')",
        )),
        _ => None,
    }
}

fn protocol<T>(message: &str) -> OpResult<T> {
    fail("database_protocol_error", message)
}

fn char_len(text: &str) -> usize {
    text.chars().count()
}

/// `_advance_event_read_guard`.
fn advance_event_read_guard(session: &Session<'_>, task_id: &str, appends: i64, prunes: i64) -> OpResult<()> {
    session.execute(
        "INSERT INTO storage_event_read_guards \
         (task_id, source_id, append_version, prune_version) VALUES (?, ?, ?, ?) \
         ON CONFLICT(task_id) DO UPDATE SET \
         append_version = storage_event_read_guards.append_version + excluded.append_version, \
         prune_version = storage_event_read_guards.prune_version + excluded.prune_version",
        &params![task_id, uuid::Uuid::new_v4().simple().to_string(), appends, prunes],
    )?;
    Ok(())
}

fn event_string_field(event: &Value, field: &str) -> String {
    if event.is_object() {
        truncate(&str_or_empty(get(event, field)), 128)
    } else {
        String::new()
    }
}

/// `_append_event_row`.
fn append_event_row(session: &Session<'_>, payload: &Payload, advance_read_guard: bool) -> OpResult<Value> {
    let task_id = required_text_512(payload, "task_id")?;
    let sequence = match payload.get("sequence").and_then(json::as_strict_int) {
        Some(sequence) if sequence >= 0 => sequence,
        _ => return protocol("Invalid event sequence"),
    };
    let raw_event = payload.get("event").cloned().unwrap_or(Value::Null);
    let event = project_usage_container(&raw_event).unwrap_or(raw_event);
    let event = stamp_event_format_envelope(event).map_err(crate::error::StorageError::protocol)?;
    let event_timestamp = execution_event_time_ms(&event, now_ms()).map_err(crate::error::StorageError::protocol)?;
    let raw_encoded = dump(&event)?;
    let encoded = encode_task_event_payload(raw_encoded.clone())?;
    let stream_value = payload.get("stream_kind").unwrap_or(&Value::Null);
    let stream_kind = if truthy(stream_value) { json::python_str(stream_value) } else { TASK_STREAM_KIND.to_owned() };
    if stream_kind != TASK_STREAM_KIND {
        return protocol(
            "event.append only accepts task streams; Project Brain uses its transactional semantic command boundary",
        );
    }
    let event_type = event_string_field(&event, "type");
    let event_kind = event_string_field(&event, "kind");
    let worker_claim = payload.get("worker_claim").unwrap_or(&Value::Null);
    let fenced = (|| -> OpResult<()> {
        let worker_root = worker_root_for_event_stream(session, &task_id, payload.get("user_id"))?;
        if worker_root.is_some() || !worker_claim.is_null() {
            let owner = if !worker_claim.is_null() { integer(payload, "user_id", None, Some(1), None)? } else { 0 };
            let root = worker_root.unwrap_or_else(|| task_id.clone());
            let attempt_id = root.strip_prefix("conversation-attempt:").unwrap_or(&root).to_owned();
            require_conversation_publication_claim(
                session,
                &attempt_id,
                &root,
                owner,
                worker_claim,
                ["done", "error", "aborted"].contains(&event_type.as_str()),
                !starts_tool_effect(&event),
            )?;
        }
        Ok(())
    })();
    match fenced {
        Ok(()) => {}
        Err(Failure::Storage(error)) if error.code == "database_conflict" => {
            return Ok(
                json!({"inserted": false, "rejected": "worker_claim_stale", "task_id": task_id, "sequence": sequence}),
            );
        }
        Err(other) => return Err(other),
    }
    if let Some(require_live) = payload.get("require_attempts_live").filter(|value| !value.is_null()) {
        let attempts = match require_live {
            Value::Array(items)
                if (1..=2).contains(&items.len())
                    && items
                        .iter()
                        .all(|item| matches!(item, Value::String(id) if (1..=128).contains(&char_len(id)))) =>
            {
                items.iter().map(|item| item.as_str().expect("attempt id").to_owned()).collect::<Vec<_>>()
            }
            _ => return protocol("Invalid require_attempts_live fence"),
        };
        let placeholders = vec!["?"; attempts.len()].join(", ");
        let parameters: Vec<SqlValue> = attempts.iter().map(SqlValue::from).collect();
        let rows = session.fetch_all(
            &format!("SELECT attempt_id, status FROM storage_generation_attempts WHERE attempt_id IN ({placeholders})"),
            &parameters,
        )?;
        let live: BTreeSet<String> = rows
            .iter()
            .filter(
                |row| matches!(row.get("status"), SqlValue::Text(status) if status == "pending" || status == "running"),
            )
            .filter_map(|row| row.get("attempt_id").as_str().map(str::to_owned))
            .collect();
        if attempts.iter().any(|attempt| !live.contains(attempt)) {
            return Ok(
                json!({"inserted": false, "rejected": "attempt_not_live", "task_id": task_id, "sequence": sequence}),
            );
        }
    }
    let count = session.execute(
        "INSERT INTO storage_events(task_id, sequence, stream_kind, \
         event_type, event_kind, event_json, created_at_ms) \
         VALUES (?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT(task_id, sequence) DO NOTHING",
        &params![&task_id, sequence, &stream_kind, &event_type, &event_kind, encoded, event_timestamp],
    )?;
    if count == 0 {
        let row = session.fetch_one(
            "SELECT event_json FROM storage_events WHERE task_id = ? AND sequence = ?",
            &params![&task_id, sequence],
        )?;
        let existing = match row {
            None => None,
            Some(row) => {
                let decoded = decode_task_event_payload(row.get("event_json"))?;
                Some(dump(&load(&SqlValue::Blob(decoded))?)?)
            }
        };
        if existing.as_deref() != Some(raw_encoded.as_slice()) {
            return fail("database_conflict", "Event sequence has a conflicting payload");
        }
    }
    if count > 0 && advance_read_guard {
        advance_event_read_guard(session, &task_id, 1, 0)?;
    }
    Ok(json!({"inserted": count > 0, "task_id": task_id, "sequence": sequence}))
}

pub fn event_append(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    append_event_row(session, payload, true)
}

pub fn event_append_batch(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let events = match payload.get("events") {
        Some(Value::Array(items)) if !items.is_empty() => items,
        _ => return protocol("Invalid event batch"),
    };
    let mut results = Vec::with_capacity(events.len());
    let mut appended_by_task: Vec<(String, i64)> = Vec::new();
    for event in events {
        let Value::Object(item) = event else { return protocol("Invalid event batch item") };
        let result = append_event_row(session, item, false)?;
        if result["inserted"] == Value::Bool(true) {
            let task = result["task_id"].as_str().expect("task id").to_owned();
            match appended_by_task.iter_mut().find(|(known, _)| *known == task) {
                Some(entry) => entry.1 += 1,
                None => appended_by_task.push((task, 1)),
            }
        }
        results.push(result);
    }
    for (task, count) in &appended_by_task {
        advance_event_read_guard(session, task, *count, 0)?;
    }
    let inserted = results.iter().filter(|item| item["inserted"] == Value::Bool(true)).count();
    let rejected = results.iter().filter(|item| item.get("rejected").is_some()).count();
    let deduplicated =
        results.iter().filter(|item| item["inserted"] != Value::Bool(true) && item.get("rejected").is_none()).count();
    Ok(json!({"results": results, "inserted": inserted, "deduplicated": deduplicated, "rejected": rejected}))
}

fn event_type_prefix_safe() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    // Python `re.match(r'^...$')`: `$` also matches before a final newline.
    PATTERN.get_or_init(|| Regex::new(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}\n?$").expect("prefix pattern"))
}

/// `payload.get(key) or []` that must be a list.
fn list_or_empty<'p>(payload: &'p Payload, key: &str) -> Option<Vec<&'p Value>> {
    let value = payload.get(key).unwrap_or(&Value::Null);
    if !truthy(value) {
        return Some(Vec::new());
    }
    match value {
        Value::Array(items) => Some(items.iter().collect()),
        _ => None,
    }
}

/// `_event_type_filter`.
fn event_type_filter(payload: &Payload) -> OpResult<(String, Vec<SqlValue>)> {
    let types = match list_or_empty(payload, "types") {
        Some(types) if types.len() <= 64 => types,
        _ => return protocol("Invalid types in storage request"),
    };
    let prefixes = match list_or_empty(payload, "type_prefixes") {
        Some(prefixes) if prefixes.len() <= 16 => prefixes,
        _ => return protocol("Invalid type_prefixes in storage request"),
    };
    for value in &types {
        if !matches!(value, Value::String(text) if (1..=128).contains(&char_len(text))) {
            return protocol("Invalid types in storage request");
        }
    }
    let mut clauses = Vec::new();
    let mut parameters = Vec::new();
    if !types.is_empty() {
        clauses.push(format!("event_type IN ({})", vec!["?"; types.len()].join(",")));
        parameters.extend(types.iter().map(|value| SqlValue::from(value.as_str().expect("type"))));
    }
    for prefix in prefixes {
        match prefix {
            Value::String(text) if event_type_prefix_safe().is_match(text) => {
                clauses.push("event_type GLOB ?".to_owned());
                parameters.push(SqlValue::from(format!("{text}*")));
            }
            _ => return protocol("Invalid type_prefixes in storage request"),
        }
    }
    if clauses.is_empty() {
        return Ok((String::new(), Vec::new()));
    }
    Ok((format!(" AND ({})", clauses.join(" OR ")), parameters))
}

fn stored_event(raw: &SqlValue) -> OpResult<Value> {
    let decoded = decode_task_event_payload(raw)?;
    load(&SqlValue::Blob(decoded))
}

pub fn event_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let projection = payload.get("payload_projection").cloned().unwrap_or(Value::from("full"));
    if !eq_str(&projection, "full") && !eq_str(&projection, REQUEST_INSPECTOR_PROJECTION) {
        return protocol("Invalid event payload_projection");
    }
    let task_id = required_text_512(payload, "task_id")?;
    let after = integer(payload, "after_sequence", Some(-1), Some(-1), None)?;
    let limit = integer(payload, "limit", Some(500), Some(1), Some(1000))?;
    let (type_clause, type_params) = event_type_filter(payload)?;
    let mut select_payload = "event_json".to_owned();
    let mut select_params: Vec<SqlValue> = Vec::new();
    if let Some(payload_types) = payload.get("payload_types").filter(|value| !value.is_null()) {
        let valid = match payload_types {
            Value::Array(items) => {
                items.len() <= 64
                    && items
                        .iter()
                        .all(|item| matches!(item, Value::String(text) if (1..=128).contains(&char_len(text))))
            }
            _ => false,
        };
        if !valid {
            return protocol("Invalid payload_types");
        }
        let items = payload_types.as_array().expect("payload types");
        select_params = items.iter().map(|item| SqlValue::from(item.as_str().expect("type"))).collect();
        select_payload = if items.is_empty() {
            "NULL".to_owned()
        } else {
            format!("CASE WHEN event_type IN ({}) THEN event_json ELSE NULL END", vec!["?"; items.len()].join(","))
        };
    }
    let mut parameters = select_params;
    parameters.push(SqlValue::from(&task_id));
    parameters.push(SqlValue::from(after));
    parameters.extend(type_params);
    parameters.push(SqlValue::from(limit));
    let rows = session.fetch_all(
        &format!(
            "SELECT sequence, event_type, {select_payload} AS event_json, created_at_ms FROM storage_events \
             WHERE task_id = ? AND sequence > ?{type_clause} ORDER BY sequence LIMIT ?"
        ),
        &parameters,
    )?;
    let project = eq_str(&projection, REQUEST_INSPECTOR_PROJECTION);
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let event = if row.get("event_json").is_null() {
            json!({"type": row.get("event_type").to_json()?})
        } else {
            stored_event(row.get("event_json"))?
        };
        let event = if project { project_request_inspector_event(event) } else { event };
        result.push(json!({
            "sequence": row.int("sequence")?,
            "event": event,
            "created_at_ms": row.int("created_at_ms")?,
        }));
    }
    Ok(Value::Array(result))
}

pub fn event_latest(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text_512(payload, "task_id")?;
    let metadata_only = match payload.get("metadata_only") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid metadata_only"),
    };
    let select_payload = if metadata_only { "NULL" } else { "event_json" };
    let row = session.fetch_one(
        &format!(
            "SELECT sequence, event_type, {select_payload} AS event_json, created_at_ms FROM storage_events \
             WHERE task_id = ? ORDER BY sequence DESC LIMIT 1"
        ),
        &params![&task_id],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    let event = if metadata_only {
        json!({"type": row.get("event_type").to_json()?})
    } else {
        stored_event(row.get("event_json"))?
    };
    Ok(json!({"sequence": row.int("sequence")?, "event": event, "created_at_ms": row.int("created_at_ms")?}))
}

pub fn event_bounds(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text_512(payload, "task_id")?;
    let include_guard = match payload.get("include_read_guard") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid include_read_guard"),
    };
    let mut types: Option<Vec<String>> = None;
    if let Some(values) = payload.get("types") {
        let valid = match values {
            Value::Array(items) => {
                (1..=EVENT_READ_GUARD_MAX_TYPES).contains(&items.len())
                    && items
                        .iter()
                        .all(|item| matches!(item, Value::String(text) if (1..=128).contains(&char_len(text))))
            }
            _ => false,
        };
        if !valid {
            return protocol("Invalid event guard types");
        }
        let unique: BTreeSet<String> =
            values.as_array().expect("types").iter().map(|item| item.as_str().expect("type").to_owned()).collect();
        types = Some(unique.into_iter().collect());
    }
    let mut extra_columns = String::new();
    let mut parameters = vec![SqlValue::from(&task_id)];
    if include_guard {
        extra_columns = ", (SELECT source_id FROM storage_event_read_guards WHERE task_id=?) AS source_id\
, (SELECT append_version FROM storage_event_read_guards WHERE task_id=?) AS append_version\
, (SELECT prune_version FROM storage_event_read_guards WHERE task_id=?) AS prune_version"
            .to_owned();
        parameters = vec![SqlValue::from(&task_id); 4];
        if types.is_some() {
            extra_columns.push_str(", (SELECT COUNT(*) FROM storage_events WHERE task_id=?) AS whole_count");
            parameters.push(SqlValue::from(&task_id));
        }
    }
    let mut filter = "task_id = ?".to_owned();
    if let Some(types) = &types {
        filter.push_str(&format!(" AND event_type IN ({})", vec!["?"; types.len()].join(",")));
        parameters.extend(types.iter().map(SqlValue::from));
    }
    let row = session.fetch_one(
        &format!(
            "SELECT COUNT(*) AS retained_count, MIN(sequence) AS base_cursor, \
             MAX(sequence) AS latest_sequence{extra_columns} FROM storage_events WHERE {filter}"
        ),
        &parameters,
    )?;
    let retained_count = match &row {
        Some(row) if row.get("retained_count").truthy() => row.int("retained_count")?,
        _ => 0,
    };
    let (base_cursor, next_cursor) = if retained_count != 0 {
        let row = row.as_ref().expect("bounds row");
        let base = row.int("base_cursor")?.max(0);
        (base, base.max(row.int("latest_sequence")?) + 1)
    } else {
        (0, 0)
    };
    let mut result = json!({"retained_count": retained_count, "base_cursor": base_cursor, "next_cursor": next_cursor});
    if include_guard {
        let epoch = session.event_read_epoch.clone();
        if epoch.chars().count() != 32 {
            return fail("database_unavailable", "Event read lease is unavailable");
        }
        let source_id = row.as_ref().map(|row| row.get("source_id").clone()).unwrap_or(SqlValue::Null);
        let mut witness = if source_id.is_null() {
            json!(["legacy", result.clone()])
        } else {
            let row = row.as_ref().expect("bounds row");
            let append = row.get("append_version");
            let prune = row.get("prune_version");
            let valid = matches!(&source_id, SqlValue::Text(id)
                    if id.chars().count() == 32 && id.chars().all(|c| "0123456789abcdef".contains(c)))
                && matches!(append, SqlValue::Integer(value) if *value >= 0)
                && matches!(prune, SqlValue::Integer(value) if *value >= 0);
            if !valid {
                return fail("database_integrity", "Event read witness is invalid");
            }
            let mut append_version = append.as_int().expect("append version");
            if types.is_some() {
                let whole_count = row.int("whole_count")?;
                if whole_count < retained_count {
                    return fail("database_integrity", "Event scope count is invalid");
                }
                append_version -= whole_count - retained_count;
            }
            json!([source_id.as_str().expect("source id"), append_version, prune.as_int().expect("prune version")])
        };
        if let Some(types) = &types {
            witness = json!([types, witness, result.clone()]);
        }
        let digest = Sha256::digest(dump(&json!([epoch, task_id, witness]))?);
        let token = hex::encode(digest);
        let mut guard = Map::new();
        guard.insert("contract".into(), Value::from(EVENT_READ_GUARD_CONTRACT));
        guard.insert("stamp".into(), Value::from(token));
        if let Some(types) = &types {
            guard.insert("contract".into(), Value::from(EVENT_READ_GUARD_SCOPED_CONTRACT));
            guard.insert("types".into(), json!(types));
        }
        result["read_guard"] = Value::Object(guard);
    }
    Ok(result)
}

pub fn event_inspector_summary(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let raw_task_ids = match payload.get("task_ids") {
        Some(Value::Array(items)) if !items.is_empty() => items,
        _ => return protocol("task_ids must be non-empty"),
    };
    let mut task_ids: Vec<String> = Vec::new();
    for value in raw_task_ids {
        let mut single = Map::new();
        single.insert("task_id".into(), value.clone());
        let task_id = required_text_512(&single, "task_id")?;
        if !task_ids.contains(&task_id) {
            task_ids.push(task_id);
        }
    }
    let structural_placeholders = vec!["?"; STRUCTURAL_EVENT_TYPES.len()].join(",");
    let select_cols = "SELECT task_id, \
SUM(CASE WHEN event_type='messages_snapshot' AND event_kind='request' THEN 1 ELSE 0 END) AS request_count, \
SUM(CASE WHEN event_type='messages_snapshot' AND event_kind='state' THEN 1 ELSE 0 END) AS state_count, \
SUM(CASE WHEN event_type='messages_snapshot' AND event_kind='' THEN 1 ELSE 0 END) AS legacy_count, \
COUNT(*) AS event_count, MIN(created_at_ms) AS first_event_at_ms ";
    let from_where = "FROM storage_events WHERE stream_kind=? AND ";
    let mut children_from_where = from_where.to_owned();
    let key_index = session.fetch_all("SELECT name FROM pragma_index_list('storage_events') WHERE origin='pk'", &[])?;
    if let Some(index) = key_index.first() {
        children_from_where = format!(
            "FROM storage_events INDEXED BY \"{}\" WHERE stream_kind=? AND ",
            column_str(index.get("name")).replace('"', "\"\"")
        );
    }
    let type_filter = format!(" AND (event_type IN ({structural_placeholders}) OR event_type LIKE ?) GROUP BY task_id");
    let summary_sql = format!("{select_cols}{from_where}task_id=?{type_filter}");
    let children_sql = format!("{select_cols}{children_from_where}task_id >= ? AND task_id < ?{type_filter}");
    let legacy_pattern = format!("{LEGACY_FLOW_EVENT_PREFIX}%");
    let mut by_task: Vec<(String, crate::session::Row)> = Vec::new();
    let mut absorb = |rows: Vec<crate::session::Row>| {
        for row in rows {
            let task = column_str(row.get("task_id"));
            match by_task.iter_mut().find(|(known, _)| *known == task) {
                Some(entry) => entry.1 = row,
                None => by_task.push((task, row)),
            }
        }
    };
    for task_id in &task_ids {
        let mut parameters = vec![SqlValue::from(TASK_STREAM_KIND), SqlValue::from(task_id)];
        parameters.extend(STRUCTURAL_EVENT_TYPES.iter().map(|value| SqlValue::from(*value)));
        parameters.push(SqlValue::from(&legacy_pattern));
        absorb(session.fetch_all(&summary_sql, &parameters)?);
        let mut parameters = vec![
            SqlValue::from(TASK_STREAM_KIND),
            SqlValue::from(format!("{task_id}#agent:")),
            SqlValue::from(format!("{task_id}#agent;")),
        ];
        parameters.extend(STRUCTURAL_EVENT_TYPES.iter().map(|value| SqlValue::from(*value)));
        parameters.push(SqlValue::from(&legacy_pattern));
        absorb(session.fetch_all(&children_sql, &parameters)?);
    }
    by_task.sort_by(|a, b| a.0.cmp(&b.0));
    let count = |row: &crate::session::Row, column: &str| -> OpResult<i64> {
        if row.get(column).truthy() {
            row.int(column)
        } else {
            Ok(0)
        }
    };
    let mut records = Vec::with_capacity(by_task.len());
    for (task, row) in &by_task {
        records.push(json!({
            "task_id": task,
            "request_count": count(row, "request_count")?,
            "state_count": count(row, "state_count")?,
            "legacy_count": count(row, "legacy_count")?,
            "event_count": count(row, "event_count")?,
            "first_event_at_ms": count(row, "first_event_at_ms")?,
        }));
    }
    Ok(json!({"records": records}))
}

pub fn event_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let cutoff = integer(payload, "created_before_ms", None, Some(0), None)?;
    let limit = integer(payload, "limit", Some(100), Some(1), Some(1000))?;
    let retention_class = required_text(payload, "retention_class", 32)?;
    let Some((required_index, tier_predicate)) = retention_spec(&retention_class) else {
        return protocol("Invalid event retention class");
    };
    if !session.index_exists(required_index)? {
        let legacy_index_present = session.index_exists(LEGACY_TASK_EVENT_RETENTION_INDEX_NAME)?;
        return Ok(json!({
            "deleted": 0,
            "deferred": true,
            "reason": if legacy_index_present { "legacy_index_offline_required" } else { "missing_index" },
            "required_index": required_index,
        }));
    }
    let deleted = session.fetch_all(
        &format!(
            "DELETE FROM storage_events WHERE (task_id, sequence) IN (\
             SELECT task_id, sequence FROM storage_events \
             WHERE {tier_predicate} \
             AND created_at_ms < ? ORDER BY created_at_ms LIMIT ?) RETURNING task_id"
        ),
        &params![cutoff, limit],
    )?;
    let deleted_count = deleted.len() as i64;
    let changed: BTreeSet<String> = deleted.iter().map(|row| column_str(row.get("task_id"))).collect();
    for task in &changed {
        advance_event_read_guard(session, task, 0, 1)?;
    }
    Ok(json!({"deleted": deleted_count, "has_more": deleted_count >= limit, "index_mode": "tier_partial_v2"}))
}

pub fn rate_limit_record_and_check(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let endpoint = required_text(payload, "endpoint", 256)?;
    let client_key = required_text(payload, "client_key", 512)?;
    let event_id = required_text(payload, "event_id", 200)?;
    let limit = integer(payload, "limit", None, Some(1), Some(1_000_000))?;
    let per_seconds = integer(payload, "per_seconds", None, Some(1), Some(7 * 24 * 60 * 60))?;
    let now = now_ms();
    let window_start = now - per_seconds * 1000;
    let expires_at = now + per_seconds * 1000;
    let pruned = session.execute(
        "DELETE FROM storage_rate_limit_events WHERE event_id IN (\
         SELECT event_id FROM storage_rate_limit_events \
         WHERE expires_at_ms <= ? ORDER BY expires_at_ms, event_id LIMIT ?)",
        &params![now, RATE_LIMIT_PRUNE_BATCH],
    )? as i64;
    session.lock_key("rate_limit_bucket", &format!("{}:{endpoint}{client_key}", char_len(&endpoint)));
    let row = session.fetch_one(
        "SELECT COUNT(*) AS event_count FROM storage_rate_limit_events \
         WHERE endpoint = ? AND client_key = ? AND occurred_at_ms >= ?",
        &params![&endpoint, &client_key, window_start],
    )?;
    let current = match row {
        Some(row) => row.int("event_count")?,
        None => 0,
    };
    if current >= limit {
        return Ok(json!({"allowed": false, "count": current, "pruned": pruned}));
    }
    session.execute(
        "INSERT INTO storage_rate_limit_events(\
         event_id, endpoint, client_key, occurred_at_ms, expires_at_ms) \
         VALUES (?, ?, ?, ?, ?)",
        &params![&event_id, &endpoint, &client_key, now, expires_at],
    )?;
    Ok(json!({"allowed": true, "count": current + 1, "pruned": pruned}))
}
