//! `operation_domains/queue.py`: per-conversation durable turn-source queue
//! and autopilot-marker operations (`operations_pkg/_queue.py`,
//! `_queue_shared.py`, `lib/turn_source_queue_contract.py`).

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::codec::{dump_param, load};
use crate::error::{internal, OpResult, StorageError};
use crate::json::{python_float_repr, python_str, truthy};
use crate::params;
use crate::payload::{char_len, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const KIND_REAL: &str = "real";
const KIND_GOAL_CONTINUATION: &str = "goal_continuation";
const KIND_PEER_MSG: &str = "peer_msg";
const KIND_WORKFLOW: &str = "workflow_step";
const KIND_BACKGROUND_RECEIPT: &str = "background_receipt";
const KIND_AUTOPILOT: &str = "autopilot";
const TURN_SOURCE_KINDS: &[&str] =
    &[KIND_REAL, KIND_GOAL_CONTINUATION, KIND_PEER_MSG, KIND_WORKFLOW, KIND_AUTOPILOT, KIND_BACKGROUND_RECEIPT];
const DEFAULT_BACKGROUND_RECEIPT_PAGE_ROWS: i128 = 32;
const QUEUE_REAP_PROBE_CONTRACT: &str = "tofu.queue.reap-probe/v1";
const QUEUE_REAP_PROBE_REQUEST_FIELD: &str = "reap_probe_contract";
const QUEUE_REAP_PROBE_RESPONSE_FIELD: &str = "reapProbeContract";
const QUEUE_REAP_PROBE_CONVERSATIONS_FIELD: &str = "conversations";
const QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD: &str = "hasExpiredLeases";
/// `OPENAPI_SCHEMAS['BackgroundCommandCompletion']` `maxLength` of
/// `command` and `resultPreview`.
const COMPLETION_TEXT_MAX_LENGTH: usize = 2000;

pub fn register(registry: &mut Registry) {
    add(registry, "queue.enqueue", OperationSpec::command(true, queue_enqueue));
    add(registry, "queue.list", OperationSpec::query(queue_list));
    add(registry, "queue.remove", OperationSpec::command(true, queue_remove));
    add(
        registry,
        "queue.injection.ack",
        OperationSpec::command(true, queue_injection_ack).with_after(queue_injection_ack_capture),
    );
    add(registry, "queue.clear", OperationSpec::command(true, queue_clear));
    add(registry, "queue.kind.clear", OperationSpec::command(true, queue_kind_clear));
    add(registry, "queue.dequeue", OperationSpec::command(false, queue_dequeue));
    add(registry, "queue.lease.release", OperationSpec::command(true, queue_lease_release));
    add(registry, "queue.reap", OperationSpec::command(true, queue_reap));
    add(registry, "queue.lease.bind", OperationSpec::command(true, queue_lease_bind));
    add(registry, "queue.finalize", OperationSpec::command(true, queue_finalize));
    add(registry, "queue.depth", OperationSpec::query(queue_depth));
    add(registry, "queue.conversations.list_all", OperationSpec::query(queue_conversations_list_all));
    add(registry, "queue.autopilot.get", OperationSpec::query(queue_autopilot_get));
    add(registry, "queue.autopilot.list_all", OperationSpec::query(queue_autopilot_list_all));
    add(registry, "queue.autopilot.arm", OperationSpec::command(false, queue_autopilot_arm));
    add(registry, "queue.autopilot.clear", OperationSpec::command(true, queue_autopilot_clear));
}

fn protocol(message: &str) -> crate::error::Failure {
    StorageError::protocol(message).into()
}

/// `int(time.time() * 1000)`.
fn wall_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|elapsed| elapsed.as_millis() as i64).unwrap_or(0)
}

fn prefix(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

fn get<'m>(map: &'m Map<String, Value>, key: &str) -> &'m Value {
    static NULL: Value = Value::Null;
    map.get(key).unwrap_or(&NULL)
}

/// `str(value or '')`.
fn str_or_empty(value: &Value) -> String {
    if truthy(value) {
        python_str(value)
    } else {
        String::new()
    }
}

/// `_load(column) or {}` followed by Mapping use: a truthy non-mapping
/// document raises like Python's `AttributeError` on `.get`.
fn load_mapping(column: &SqlValue) -> OpResult<Map<String, Value>> {
    match load(column)? {
        Value::Object(map) => Ok(map),
        value if !truthy(&value) => Ok(Map::new()),
        _ => internal("AttributeError: stored queue document has no .get"),
    }
}

/// `_load(column) or {}` as a JSON value.
fn load_or_empty(column: &SqlValue) -> OpResult<Value> {
    let value = load(column)?;
    Ok(if truthy(&value) { value } else { Value::Object(Map::new()) })
}

/// Python `json.dumps(value, ensure_ascii=False, separators=(',', ':'))`
/// (insertion order, `float.__repr__` numbers).
fn python_compact_dumps(value: &Value) -> String {
    fn write(out: &mut String, value: &Value) {
        match value {
            Value::Null => out.push_str("null"),
            Value::Bool(true) => out.push_str("true"),
            Value::Bool(false) => out.push_str("false"),
            Value::Number(number) => match (number.as_i64(), number.as_u64()) {
                (Some(value), _) => out.push_str(&value.to_string()),
                (None, Some(value)) => out.push_str(&value.to_string()),
                _ => out.push_str(&python_float_repr(number.as_f64().unwrap_or(0.0))),
            },
            Value::String(text) => out.push_str(&serde_json::to_string(text).expect("string")),
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write(out, item);
                }
                out.push(']');
            }
            Value::Object(map) => {
                out.push('{');
                for (index, (key, item)) in map.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    out.push_str(&serde_json::to_string(key).expect("string"));
                    out.push(':');
                    write(out, item);
                }
                out.push('}');
            }
        }
    }
    let mut out = String::new();
    write(&mut out, value);
    out
}

fn queue_conv_id(payload: &Payload) -> OpResult<String> {
    required_text(payload, "conv_id", 256)
}

fn queue_owner(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), None)
}

fn require_owned_conversation(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<()> {
    if session
        .fetch_one(
            "SELECT 1 AS present FROM storage_conversations \
             WHERE id=? AND user_id=?",
            &params![conv_id, user_id],
        )?
        .is_none()
    {
        return Err(StorageError::new("database_not_found", "Conversation not found").into());
    }
    Ok(())
}

/// `renumber_queue_positions` (`operations_pkg/_queue_shared.py`).
fn queue_renumber(session: &Session<'_>, conversation_id: &str, user_id: i64) -> OpResult<()> {
    let rows = session.fetch_all(
        "SELECT id FROM storage_queue_items \
         WHERE conv_id = ? AND user_id = ? \
         ORDER BY priority, position, id",
        &params![conversation_id, user_id],
    )?;
    for (index, row) in rows.iter().enumerate() {
        session.execute(
            "UPDATE storage_queue_items SET position = ? \
             WHERE id = ? AND user_id = ?",
            &params![(index + 1) as i64, row.get("id"), user_id],
        )?;
    }
    Ok(())
}

/// `_queue_marker_document`.
fn queue_marker_document(row: &Row, out: &mut Map<String, Value>) -> OpResult<()> {
    out.insert("queueId".into(), Value::String(row.text("queue_id")));
    out.insert("userId".into(), Value::from(row.int("user_id")?));
    out.insert("config".into(), load_or_empty(row.require("config_json")?)?);
    out.insert("createdAt".into(), Value::from(row.int("created_at_ms")?));
    Ok(())
}

fn queue_autopilot_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let row = session.fetch_one(
        "SELECT queue_id, user_id, config_json, created_at_ms \
         FROM storage_autopilot_markers WHERE conv_id = ? AND user_id = ?",
        &params![&conv_id, user_id],
    )?;
    match row {
        None => Ok(Value::Null),
        Some(row) => {
            let mut document = Map::new();
            queue_marker_document(&row, &mut document)?;
            Ok(Value::Object(document))
        }
    }
}

/// Internal recovery feed across owners; never exposed through a route.
fn queue_autopilot_list_all(session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    let rows = session.fetch_all(
        "SELECT conv_id, user_id, queue_id, config_json, created_at_ms \
         FROM storage_autopilot_markers ORDER BY user_id, conv_id",
        &[],
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in &rows {
        let mut document = Map::new();
        document.insert("convId".into(), Value::String(row.text("conv_id")));
        queue_marker_document(row, &mut document)?;
        result.push(Value::Object(document));
    }
    Ok(Value::Array(result))
}

fn queue_autopilot_arm(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let queue_id = required_text(payload, "queue_id", 256)?;
    let config = match payload.get("config") {
        None => Map::new(),
        Some(Value::Object(config)) => config.clone(),
        Some(_) => return Err(protocol("Invalid autopilot marker config")),
    };
    require_owned_conversation(session, &conv_id, user_id)?;
    session.lock_key("autopilot_marker", &format!("{user_id}:{conv_id}"));
    let current = session.fetch_one(
        "SELECT queue_id, user_id, config_json, created_at_ms \
         FROM storage_autopilot_markers WHERE conv_id = ? AND user_id = ?",
        &params![&conv_id, user_id],
    )?;
    if let Some(current) = current {
        let mut document = Map::new();
        document.insert("armed".into(), Value::Bool(false));
        queue_marker_document(&current, &mut document)?;
        return Ok(Value::Object(document));
    }
    let now = wall_ms();
    let config = Value::Object(config);
    session.execute(
        "INSERT INTO storage_autopilot_markers \
         (conv_id, user_id, queue_id, config_json, created_at_ms) \
         VALUES (?, ?, ?, ?, ?)",
        &[conv_id.into(), user_id.into(), (&queue_id).into(), dump_param(&config)?, now.into()],
    )?;
    Ok(json!({"armed": true, "queueId": queue_id, "config": config, "createdAt": now}))
}

fn queue_autopilot_clear(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    session.lock_key("autopilot_marker", &format!("{user_id}:{conv_id}"));
    let deleted = session.execute(
        "DELETE FROM storage_autopilot_markers WHERE conv_id = ? AND user_id = ?",
        &params![&conv_id, user_id],
    )?;
    Ok(json!({"cleared": deleted != 0}))
}

/// `_queue_kind`.
fn queue_kind(payload: &Payload) -> OpResult<String> {
    match payload.get("kind") {
        None => Ok(KIND_REAL.to_owned()),
        Some(Value::String(kind)) if TURN_SOURCE_KINDS.contains(&kind.as_str()) => Ok(kind.clone()),
        Some(_) => Err(protocol("Invalid queue kind")),
    }
}

fn queue_priority(payload: &Payload) -> OpResult<i64> {
    integer(payload, "priority", Some(100), Some(0), Some(1000))
}

/// `lib/background_command_projection.py::background_command_completion`
/// (with `bounded_completion_preview`).
fn background_command_completion(payload: &Map<String, Value>) -> Option<Value> {
    let Some(Value::String(command_id)) = payload.get("_backgroundCommand") else { return None };
    if command_id.is_empty() {
        return None;
    }
    let empty = Map::new();
    let metadata = match payload.get("_backgroundCommandCompletion") {
        Some(Value::Object(metadata)) => metadata,
        _ => &empty,
    };
    let mut result = Map::new();
    result.insert("commandId".into(), Value::String(prefix(command_id, 128)));
    result.insert("status".into(), Value::String("completed".into()));
    result.insert("command".into(), Value::String(str_or_empty(get(metadata, "command"))));
    result.insert("resultPreview".into(), Value::String(str_or_empty(get(metadata, "resultPreview"))));
    result.insert("resultTruncated".into(), Value::Bool(truthy(get(metadata, "resultTruncated"))));
    if let Some(Value::Number(exit_code)) = metadata.get("exitCode") {
        if exit_code.is_i64() || exit_code.is_u64() {
            result.insert("exitCode".into(), Value::Number(exit_code.clone()));
        }
    }
    for key in ["timedOut", "interrupted"] {
        if let Some(Value::Bool(flag)) = metadata.get(key) {
            result.insert(key.into(), Value::Bool(*flag));
        }
    }
    for field in ["command", "resultPreview"] {
        let text = result[field].as_str().expect("str field").to_owned();
        if char_len(&text) > COMPLETION_TEXT_MAX_LENGTH {
            result.insert(field.into(), Value::String(prefix(&text, COMPLETION_TEXT_MAX_LENGTH)));
            if field == "resultPreview" {
                result.insert("resultTruncated".into(), Value::Bool(true));
            }
        }
    }
    Some(Value::Object(result))
}

/// `_queue_item`: durable fields plus the get_queue preview contract.
fn queue_item(row: &Row) -> OpResult<Map<String, Value>> {
    let payload = load_mapping(row.require("payload_json")?)?;
    let kind = row.require("kind")?;
    let text = if truthy(get(&payload, "_peerMessage")) {
        python_str(get(&payload, "_peerText"))
    } else {
        str_or_empty(payload.get("text").unwrap_or(&Value::String(String::new())))
    };
    let mut result = Map::new();
    result.insert("queueId".into(), Value::String(row.text("id")));
    result.insert("userId".into(), Value::from(row.int("user_id")?));
    result.insert("position".into(), Value::from(row.int("position")?));
    result
        .insert("kind".into(), Value::String(if kind.truthy() { kind.text_or_empty() } else { KIND_REAL.to_owned() }));
    result.insert("priority".into(), Value::from(row.int("priority")?));
    result.insert("timestamp".into(), Value::from(row.int("created_at_ms")?));
    result.insert("text".into(), Value::String(prefix(&text, 2000)));
    result.insert("hasImages".into(), Value::Bool(truthy(get(&payload, "images"))));
    result.insert("hasPdfs".into(), Value::Bool(truthy(get(&payload, "pdfTexts"))));
    result.insert("hasAttachments".into(), Value::Bool(truthy(get(&payload, "attachments"))));
    result.insert("hasRefs".into(), Value::Bool(truthy(get(&payload, "convRefs"))));
    result.insert("hasQuotes".into(), Value::Bool(truthy(get(&payload, "replyQuotes"))));
    result.insert("payload".into(), Value::Object(payload.clone()));
    result.insert("config".into(), load_or_empty(row.require("config_json")?)?);
    for (column_name, public_name) in
        [("input_turn_id", "inputTurnId"), ("output_turn_id", "outputTurnId"), ("attempt_id", "attemptId")]
    {
        let value = row.get(column_name).text_or_empty();
        if !value.is_empty() {
            result.insert(public_name.into(), Value::String(value));
        }
    }
    let embedded_message_id = match payload.get("_user_msg") {
        Some(Value::Object(embedded_user)) => get(embedded_user, "_msgId").clone(),
        _ => Value::String(String::new()),
    };
    let source_message_id =
        str_or_empty(if truthy(&embedded_message_id) { &embedded_message_id } else { get(&payload, "_msgId") });
    if !source_message_id.is_empty() {
        result.insert("sourceMessageId".into(), Value::String(source_message_id));
    }
    if truthy(get(&payload, "_peerMessage")) {
        result.insert("isPeerMessage".into(), Value::Bool(true));
        result.insert("fromConv".into(), payload.get("_fromConv").cloned().unwrap_or(Value::String(String::new())));
        result.insert("isPeerHuman".into(), Value::Bool(truthy(get(&payload, "_peerHuman"))));
    }
    if let Some(completion) = background_command_completion(&payload) {
        result.insert("backgroundCommand".into(), completion);
    }
    if truthy(get(&payload, "_steerFallback")) {
        result.insert("steerFallback".into(), Value::Bool(true));
    }
    Ok(result)
}

fn queue_rows(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<Vec<Row>> {
    session.fetch_all(
        "SELECT id, user_id, conv_id, payload_json, config_json, position, kind, \
         priority, created_at_ms, leased_until_ms, lease_task_id, \
         input_turn_id, output_turn_id, attempt_id \
         FROM storage_queue_items WHERE conv_id = ? AND user_id = ? \
         ORDER BY priority, position",
        &params![conv_id, user_id],
    )
}

fn preview_item(row: &Row, deduped: bool) -> OpResult<Value> {
    let mut result = queue_item(row)?;
    result.shift_remove("payload");
    result.shift_remove("config");
    result.insert("deduped".into(), Value::Bool(deduped));
    Ok(Value::Object(result))
}

fn queue_enqueue(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let queue_id = required_text(payload, "queue_id", 256)?;
    let kind = queue_kind(payload)?;
    let priority = queue_priority(payload)?;
    let empty = Value::Object(Map::new());
    let message = payload.get("message").unwrap_or(&empty);
    let config = payload.get("config").unwrap_or(&empty);
    if !message.is_object() || !config.is_object() {
        return Err(protocol("Invalid queue document"));
    }
    require_owned_conversation(session, &conv_id, user_id)?;
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let mut rows = queue_rows(session, &conv_id, user_id)?;
    if kind == KIND_REAL {
        let deleted = session.execute(
            "DELETE FROM storage_queue_items \
             WHERE conv_id = ? AND user_id = ? AND kind = ?",
            &params![&conv_id, user_id, KIND_GOAL_CONTINUATION],
        )?;
        if deleted != 0 {
            queue_renumber(session, &conv_id, user_id)?;
            rows = queue_rows(session, &conv_id, user_id)?;
        }
    }
    if kind == KIND_AUTOPILOT || kind == KIND_GOAL_CONTINUATION {
        for row in &rows {
            if row.get("kind").as_str() == Some(kind.as_str()) {
                return preview_item(row, true);
            }
        }
    }
    let position = rows.len() as i64 + 1;
    let created_at = integer(payload, "created_at_ms", Some(wall_ms()), Some(0), None)?;
    session.execute(
        "INSERT INTO storage_queue_items \
         (id, user_id, conv_id, payload_json, config_json, position, kind, \
         priority, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &[
            (&queue_id).into(),
            user_id.into(),
            (&conv_id).into(),
            dump_param(message)?,
            dump_param(config)?,
            position.into(),
            (&kind).into(),
            priority.into(),
            created_at.into(),
        ],
    )?;
    let inserted = session.fetch_one(
        "SELECT id, user_id, conv_id, payload_json, config_json, position, kind, \
         priority, created_at_ms, leased_until_ms, lease_task_id, \
         input_turn_id, output_turn_id, attempt_id \
         FROM storage_queue_items WHERE id=? AND user_id=?",
        &params![&queue_id, user_id],
    )?;
    match inserted {
        Some(inserted) => preview_item(&inserted, false),
        None => internal("TypeError: inserted queue row is missing"),
    }
}

/// A JSON value that is a Python `int` (bool excluded) as a wide integer.
fn strict_int_wide(value: Option<&Value>) -> Option<i128> {
    match value {
        Some(Value::Number(number)) => number.as_i64().map(i128::from).or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    }
}

/// The queue page cursor `(priority, createdAt, queueId)`.
type PageKey = (i128, i128, String);

fn queue_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let mode = match payload.get("mode") {
        None => "full",
        Some(Value::String(mode))
            if matches!(
                mode.as_str(),
                "full" | "preview" | "dispatchable" | "background_pending" | "background_page"
            ) =>
        {
            mode.as_str()
        }
        Some(_) => return Err(protocol("Invalid queue list mode")),
    };
    let paged = mode == "background_page";
    let page_size = match payload.get("page_size") {
        None => Some(DEFAULT_BACKGROUND_RECEIPT_PAGE_ROWS),
        value => strict_int_wide(value),
    };
    let max_bytes = match payload.get("max_bytes") {
        None => Some(8 * 1024 * 1024),
        value => strict_int_wide(value),
    };
    let bounds_error = || protocol("Invalid queue page bounds or cursor");
    let (Some(page_size), Some(max_bytes)) = (page_size, max_bytes) else { return Err(bounds_error()) };
    if page_size < 1 || max_bytes < 1 {
        return Err(bounds_error());
    }
    let cursor: Option<PageKey> = match payload.get("cursor") {
        None | Some(Value::Null) => None,
        Some(Value::Object(cursor)) => {
            let mut parts = [0i128; 2];
            for (slot, key) in parts.iter_mut().zip(["priority", "createdAt"]) {
                match strict_int_wide(cursor.get(key)) {
                    Some(value) if (0..(1i128 << 64)).contains(&value) => *slot = value,
                    _ => return Err(bounds_error()),
                }
            }
            match cursor.get("queueId") {
                Some(Value::String(queue_id)) if (1..=256).contains(&queue_id.len()) => {
                    Some((parts[0], parts[1], queue_id.clone()))
                }
                _ => return Err(bounds_error()),
            }
        }
        Some(_) => return Err(bounds_error()),
    };
    let mut result: Vec<Value> = Vec::new();
    let mut page_bytes: i128 = 0;
    let mut last: Option<Value> = None;
    let mut more = false;
    let mut rows = queue_rows(session, &conv_id, user_id)?;
    let key_of = |row: &Row| -> OpResult<PageKey> {
        Ok((row.int("priority")? as i128, row.int("created_at_ms")? as i128, row.text("id")))
    };
    if paged {
        let mut keyed = rows.into_iter().map(|row| Ok((key_of(&row)?, row))).collect::<OpResult<Vec<_>>>()?;
        keyed.sort_by(|left, right| {
            (left.0 .0, left.0 .1, left.0 .2.as_bytes()).cmp(&(right.0 .0, right.0 .1, right.0 .2.as_bytes()))
        });
        rows = keyed.into_iter().map(|(_, row)| row).collect();
    }
    for row in &rows {
        if paged {
            if let Some(cursor) = &cursor {
                let key = key_of(row)?;
                if (key.0, key.1, key.2.as_bytes()) <= (cursor.0, cursor.1, cursor.2.as_bytes()) {
                    continue;
                }
            }
        }
        let kind = row.get("kind").as_str().unwrap_or("");
        if mode == "background_pending" || mode == "background_page" {
            if kind != KIND_BACKGROUND_RECEIPT && kind != KIND_WORKFLOW {
                continue;
            }
            if row.get("attempt_id").truthy() {
                continue;
            }
            match load_or_empty(row.require("payload_json")?)? {
                Value::Object(message) if truthy(get(&message, "_backgroundCommand")) => {}
                _ => continue,
            }
        } else if mode == "dispatchable" {
            if kind == KIND_AUTOPILOT || kind == KIND_BACKGROUND_RECEIPT {
                continue;
            }
            if kind == KIND_WORKFLOW {
                if let Value::Object(message) = load_or_empty(row.require("payload_json")?)? {
                    if truthy(get(&message, "_backgroundCommand")) {
                        continue;
                    }
                }
            }
        }
        let mut item = queue_item(row)?;
        if mode == "preview" {
            item.shift_remove("payload");
            item.shift_remove("config");
        }
        let item = Value::Object(item);
        if paged {
            let row_bytes = python_compact_dumps(&item).len() as i128 + i128::from(!result.is_empty());
            let next_cursor = json!({
                "priority": row.int("priority")?,
                "createdAt": row.int("created_at_ms")?,
                "queueId": row.text("id"),
            });
            let overhead = python_compact_dumps(&json!({"items": [], "nextCursor": next_cursor.clone()})).len() as i128;
            if !result.is_empty()
                && (result.len() as i128 >= page_size || page_bytes + row_bytes + overhead > max_bytes)
            {
                more = true;
                break;
            }
            page_bytes += row_bytes;
            last = Some(next_cursor);
        }
        result.push(item);
    }
    if paged {
        let next_cursor = if more { last.unwrap_or(Value::Null) } else { Value::Null };
        return Ok(json!({"items": result, "nextCursor": next_cursor}));
    }
    Ok(Value::Array(result))
}

fn queue_remove(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let queue_id = required_text(payload, "queue_id", 256)?;
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let deleted = session.execute(
        "DELETE FROM storage_queue_items \
         WHERE id = ? AND conv_id = ? AND user_id = ?",
        &params![&queue_id, &conv_id, user_id],
    )?;
    if deleted != 0 {
        queue_renumber(session, &conv_id, user_id)?;
    }
    Ok(json!({"removed": deleted != 0}))
}

fn placeholders(count: usize) -> String {
    vec!["?"; count].join(",")
}

/// `_queue_injection_ack`: acknowledge at most one inbox drain; never
/// cancel accepted Turn pairs.
fn queue_injection_ack(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let Some(Value::Array(values)) = payload.get("queue_ids") else {
        return Err(protocol("Invalid injection queue IDs"));
    };
    let mut ids = Vec::with_capacity(values.len());
    for value in values {
        let mut wrapped = Map::new();
        wrapped.insert("id".into(), value.clone());
        ids.push(required_text(&wrapped, "id", 256)?);
    }
    ids.sort_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    ids.dedup();
    if ids.is_empty() {
        return Ok(json!({"removed": 0, "removedQueueIds": []}));
    }
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let mut rows = Vec::new();
    for window in ids.chunks(512) {
        let sql = format!(
            "SELECT id, kind, payload_json, position FROM storage_queue_items \
             WHERE conv_id=? AND user_id=? \
             AND id IN ({}) \
             AND COALESCE(input_turn_id,'')='' \
             AND COALESCE(output_turn_id,'')='' AND COALESCE(attempt_id,'')='' \
             AND kind IN (?,?,?) ORDER BY position, id",
            placeholders(window.len())
        );
        let mut values: Vec<SqlValue> = vec![(&conv_id).into(), user_id.into()];
        values.extend(window.iter().map(SqlValue::from));
        values.extend([KIND_PEER_MSG.into(), KIND_WORKFLOW.into(), KIND_BACKGROUND_RECEIPT.into()]);
        rows.extend(session.fetch_all(&sql, &values)?);
    }
    let mut keyed =
        rows.into_iter().map(|row| Ok((row.int("position")?, row.text("id"), row))).collect::<OpResult<Vec<_>>>()?;
    keyed.sort_by(|left, right| (left.0, left.1.as_bytes()).cmp(&(right.0, right.1.as_bytes())));
    let mut removed_ids: Vec<String> = Vec::new();
    for (_, id, row) in &keyed {
        let message = match load_or_empty(row.require("payload_json")?)? {
            Value::Object(message) => message,
            _ => continue,
        };
        if truthy(get(&message, "_peerHuman")) {
            continue;
        }
        let kind = row.get("kind").as_str().unwrap_or("");
        let completion_id = get(&message, "_backgroundCommand");
        if (kind == KIND_PEER_MSG && truthy(get(&message, "_peerMessage")))
            || ((kind == KIND_WORKFLOW || kind == KIND_BACKGROUND_RECEIPT)
                && completion_id.as_str().is_some_and(|text| !text.is_empty()))
        {
            removed_ids.push(id.clone());
        }
    }
    if !removed_ids.is_empty() {
        for window in removed_ids.chunks(512) {
            let sql = format!(
                "DELETE FROM storage_queue_items WHERE conv_id=? AND user_id=? \
                 AND id IN ({})",
                placeholders(window.len())
            );
            let mut values: Vec<SqlValue> = vec![(&conv_id).into(), user_id.into()];
            values.extend(window.iter().map(SqlValue::from));
            session.execute(&sql, &values)?;
        }
        queue_renumber(session, &conv_id, user_id)?;
    }
    Ok(json!({"removed": removed_ids.len(), "removedQueueIds": removed_ids}))
}

const CONVERSATION_SYNC_EVENT_CONTRACT: &str = "tofu.conversation-sync.event/v1";
const STORAGE_COMMITTED_EVENTS_CONTRACT: &str = "storage.committed-events/v1";

// private port of lib/storage_authority_sql/operations_pkg/_turns_core.py::_append_conversation_change
// (no attempt reference, no explicit occurred_at); shared owner: turns
fn append_conversation_change(
    session: &Session<'_>,
    conversation_id: &str,
    user_id: i64,
    change_type: &str,
    payload: Value,
) -> OpResult<Value> {
    session.lock_key("conversation_sync", &format!("{user_id}:{conversation_id}"));
    let now = wall_ms();
    let head = session.fetch_one(
        "SELECT sync_sequence FROM storage_conversation_sync_heads \
         WHERE conversation_id=? AND user_id=?",
        &params![conversation_id, user_id],
    )?;
    let head = match head {
        Some(row) => {
            let value = row.require("sync_sequence")?;
            if value.truthy() {
                row.int("sync_sequence")?
            } else {
                0
            }
        }
        None => 0,
    };
    let sequence = head + 1;
    let event = json!({
        "contract": CONVERSATION_SYNC_EVENT_CONTRACT,
        "type": change_type,
        "conversationId": conversation_id,
        "syncSeq": sequence,
        "occurredAt": now,
        "payload": payload,
    });
    if sequence == 1 {
        session.execute(
            "INSERT INTO storage_conversation_sync_heads \
             (conversation_id,user_id,sync_sequence,updated_at) VALUES (?,?,?,?)",
            &params![conversation_id, user_id, sequence, now],
        )?;
    } else {
        let updated = session.execute(
            "UPDATE storage_conversation_sync_heads \
             SET sync_sequence=?,updated_at=? \
             WHERE conversation_id=? AND user_id=? AND sync_sequence=?",
            &params![sequence, now, conversation_id, user_id, sequence - 1],
        )?;
        if updated != 1 {
            return Err(
                StorageError::new("database_conflict", "Conversation sync sequence changed concurrently").into()
            );
        }
    }
    session.execute(
        "INSERT INTO storage_conversation_changes \
         (conversation_id,user_id,sync_sequence,change_type,turn_id,attempt_id,\
         attempt_sequence,event_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        &[
            conversation_id.into(),
            user_id.into(),
            sequence.into(),
            change_type.into(),
            "".into(),
            "".into(),
            SqlValue::Null,
            dump_param(&event)?,
            now.into(),
        ],
    )?;
    Ok(event)
}

/// `_queue_injection_ack_capture`: publish only actual removals in the same
/// transaction as the batch.
fn queue_injection_ack_capture(
    session: &Session<'_>,
    _operation_name: &str,
    payload: &Payload,
    result: Value,
) -> OpResult<Value> {
    let removed = result["removedQueueIds"].clone();
    if !truthy(&removed) {
        return Ok(result);
    }
    let user_id = queue_owner(payload)?;
    let conv_id = queue_conv_id(payload)?;
    let event = append_conversation_change(
        session,
        &conv_id,
        user_id,
        "turn.upsert",
        json!({"turns": [], "attempts": [], "removedQueueIds": removed}),
    )?;
    // private port of _turns_core.py::_committed_events_result/_committed_event_notice; shared owner: turns
    Ok(json!({
        "_storageCommitContract": STORAGE_COMMITTED_EVENTS_CONTRACT,
        "value": result,
        "events": [{"contract": "storage.conversation-commit/v1", "userId": user_id, "event": event}],
    }))
}

fn queue_clear(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let deleted = session
        .execute("DELETE FROM storage_queue_items WHERE conv_id = ? AND user_id = ?", &params![&conv_id, user_id])?;
    Ok(json!({"cleared": deleted}))
}

/// Delete one owner-scoped source kind without touching other intent.
fn queue_kind_clear(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let kind = queue_kind(payload)?;
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let deleted = session.execute(
        "DELETE FROM storage_queue_items \
         WHERE conv_id = ? AND user_id = ? AND kind = ?",
        &params![&conv_id, user_id, &kind],
    )?;
    if deleted != 0 {
        queue_renumber(session, &conv_id, user_id)?;
    }
    Ok(json!({"cleared": deleted}))
}

/// `leased_until_ms is None or int(leased_until_ms) < now_ms`.
fn lease_free(row: &Row, now_ms: i64) -> OpResult<bool> {
    if row.get("leased_until_ms").is_null() {
        return Ok(true);
    }
    Ok(row.int("leased_until_ms")? < now_ms)
}

fn queue_dequeue(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let now_ms = integer(payload, "now_ms", None, Some(0), None)?;
    let lease_ms = integer(payload, "lease_ms", None, Some(1), Some(3600 * 1000))?;
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let rows = queue_rows(session, &conv_id, user_id)?;
    // A row whose Turn pair already exists runs before every source whose
    // Turn is created at dispatch, earliest pair first.
    let paired = rows.iter().find(|candidate| candidate.get("attempt_id").truthy());
    let mut chosen = None;
    if let Some(paired) = paired {
        if lease_free(paired, now_ms)? {
            chosen = Some(paired);
        }
    } else {
        for candidate in &rows {
            let kind = candidate.get("kind").as_str().unwrap_or("");
            if kind == KIND_AUTOPILOT || kind == KIND_BACKGROUND_RECEIPT {
                continue;
            }
            if kind == KIND_WORKFLOW
                && truthy(get(&load_mapping(candidate.require("payload_json")?)?, "_backgroundCommand"))
            {
                continue;
            }
            if lease_free(candidate, now_ms)? {
                chosen = Some(candidate);
                break;
            }
        }
    }
    let Some(row) = chosen else { return Ok(Value::Null) };
    session.execute(
        "UPDATE storage_queue_items SET leased_until_ms = ?, lease_task_id = ? \
         WHERE id = ? AND user_id = ?",
        &params![now_ms + lease_ms, "", row.get("id"), user_id],
    )?;
    Ok(Value::Object(queue_item(row)?))
}

fn queue_lease_release(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let queue_id = required_text(payload, "queue_id", 256)?;
    let user_id = queue_owner(payload)?;
    let updated = session.execute(
        "UPDATE storage_queue_items SET leased_until_ms = NULL, \
         lease_task_id = '' WHERE id = ? AND user_id = ?",
        &params![&queue_id, user_id],
    )?;
    Ok(json!({"released": updated != 0}))
}

fn conversation_refs(rows: &[Row]) -> OpResult<Vec<Value>> {
    let mut conversations = Vec::new();
    for row in rows {
        if row.get("conv_id").truthy() {
            conversations.push(json!({"userId": row.int("user_id")?, "convId": row.text("conv_id")}));
        }
    }
    Ok(conversations)
}

/// `_queue_reap`: release expired queue leases and return conversations to drain.
fn queue_reap(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let now_ms = integer(payload, "now_ms", None, Some(0), None)?;
    let force = truthy(crate::payload::get(payload, "force_reclaim"));
    let predicate = if force {
        "kind != 'autopilot' AND leased_until_ms IS NOT NULL"
    } else {
        "kind != 'autopilot' AND leased_until_ms IS NOT NULL AND leased_until_ms < ?"
    };
    let args: Vec<SqlValue> = if force { Vec::new() } else { vec![now_ms.into()] };
    let rows = session.fetch_all(
        &format!(
            "SELECT DISTINCT user_id, conv_id FROM storage_queue_items WHERE {predicate} ORDER BY user_id, conv_id"
        ),
        &args,
    )?;
    session.execute(
        &format!("UPDATE storage_queue_items SET leased_until_ms = NULL, lease_task_id = '' WHERE {predicate}"),
        &args,
    )?;
    let conversations = conversation_refs(&rows)?;
    // `ok=False` is the storage receipt contract for a clean no-write verdict.
    Ok(json!({"ok": !conversations.is_empty(), "conversations": conversations}))
}

fn queue_lease_bind(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let queue_id = required_text(payload, "queue_id", 256)?;
    let user_id = queue_owner(payload)?;
    let task_id = required_text(payload, "task_id", 256)?;
    let now_ms = integer(payload, "now_ms", None, Some(0), None)?;
    let lease_ms = integer(payload, "lease_ms", None, Some(1), Some(3600 * 1000))?;
    let updated = session.execute(
        "UPDATE storage_queue_items SET lease_task_id = ?, \
         leased_until_ms = ? WHERE id = ? AND user_id = ?",
        &params![&task_id, now_ms + lease_ms, &queue_id, user_id],
    )?;
    Ok(json!({"bound": updated != 0}))
}

fn queue_finalize(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let queue_id = required_text(payload, "queue_id", 256)?;
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let deleted = session.execute(
        "DELETE FROM storage_queue_items \
         WHERE id = ? AND conv_id = ? AND user_id = ?",
        &params![&queue_id, &conv_id, user_id],
    )?;
    if deleted != 0 {
        queue_renumber(session, &conv_id, user_id)?;
    }
    Ok(json!({"finalized": deleted != 0}))
}

fn queue_depth(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = queue_conv_id(payload)?;
    let user_id = queue_owner(payload)?;
    let row = session.fetch_one(
        "SELECT COUNT(*) AS count FROM storage_queue_items \
         WHERE conv_id = ? AND user_id = ? \
         AND kind NOT IN ('autopilot', 'background_receipt')",
        &params![&conv_id, user_id],
    )?;
    let depth = match row {
        Some(row) => row.int("count")?,
        None => 0,
    };
    Ok(json!({"depth": depth}))
}

/// Internal recovery feed, oldest queued conversation first.
fn queue_conversations_list_all(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let reap_probe_contract = match payload.get(QUEUE_REAP_PROBE_REQUEST_FIELD) {
        None | Some(Value::Null) => false,
        Some(Value::String(contract)) if contract == QUEUE_REAP_PROBE_CONTRACT => true,
        Some(_) => return Err(protocol("Invalid queue reap probe contract")),
    };
    let kind = payload.get("kind").filter(|kind| !kind.is_null());
    if reap_probe_contract {
        if kind.is_some() {
            return Err(protocol("Queue reap probe cannot filter one source kind"));
        }
        let now_ms = integer(payload, "now_ms", None, Some(0), None)?;
        let rows = session.fetch_all(
            "SELECT user_id, conv_id, MIN(created_at_ms) AS oldest_created_at, \
             MAX(CASE WHEN leased_until_ms IS NOT NULL \
             AND leased_until_ms < ? THEN 1 ELSE 0 END) AS has_expired_lease \
             FROM storage_queue_items WHERE kind NOT IN \
             ('autopilot', 'background_receipt') \
             GROUP BY user_id, conv_id \
             ORDER BY oldest_created_at, user_id, conv_id",
            &params![now_ms],
        )?;
        let conversations = conversation_refs(&rows)?;
        let has_expired = rows.iter().any(|row| row.get("has_expired_lease").truthy());
        let mut result = Map::new();
        result.insert(QUEUE_REAP_PROBE_RESPONSE_FIELD.into(), Value::String(QUEUE_REAP_PROBE_CONTRACT.into()));
        result.insert(QUEUE_REAP_PROBE_CONVERSATIONS_FIELD.into(), Value::Array(conversations));
        result.insert(QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD.into(), Value::Bool(has_expired));
        return Ok(Value::Object(result));
    }
    let rows = if let Some(kind) = kind {
        let mut wrapped = Map::new();
        wrapped.insert("kind".into(), kind.clone());
        let kind = queue_kind(&wrapped)?;
        session.fetch_all(
            "SELECT user_id, conv_id, MIN(created_at_ms) AS oldest_created_at \
             FROM storage_queue_items WHERE kind = ? \
             GROUP BY user_id, conv_id \
             ORDER BY oldest_created_at, user_id, conv_id",
            &params![&kind],
        )?
    } else {
        session.fetch_all(
            "SELECT user_id, conv_id, MIN(created_at_ms) AS oldest_created_at \
             FROM storage_queue_items WHERE kind NOT IN \
             ('autopilot', 'background_receipt') \
             GROUP BY user_id, conv_id \
             ORDER BY oldest_created_at, user_id, conv_id",
            &[],
        )?
    };
    Ok(Value::Array(conversation_refs(&rows)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_bytes_use_python_compact_json() {
        let value = json!({"a": [1, 1e-5, "é\u{1}"], "b": {"c": null}});
        assert_eq!(python_compact_dumps(&value), "{\"a\":[1,1e-05,\"é\\u0001\"],\"b\":{\"c\":null}}");
    }

    #[test]
    fn completion_preview_is_bounded() {
        let long = "x".repeat(2001);
        let payload = json!({
            "_backgroundCommand": "bg_1",
            "_backgroundCommandCompletion": {"resultPreview": long, "exitCode": 0, "timedOut": 1},
        });
        let completion = background_command_completion(payload.as_object().unwrap()).unwrap();
        assert_eq!(completion["resultPreview"].as_str().unwrap().len(), 2000);
        assert_eq!(completion["resultTruncated"], json!(true));
        assert_eq!(completion["exitCode"], json!(0));
        assert!(completion.get("timedOut").is_none());
    }
}
