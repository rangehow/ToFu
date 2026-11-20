//! `operation_domains/conversations.py`: conversation headers, transcript
//! projections, trash lifecycle and diagnostics
//! (`operations_pkg/_conversations.py`, `_conversation_clone.py`,
//! `_conversation_diagnostic.py`, `_conversation_usage.py`).
//!
//! `conversation.search` is served by the turn-search projection runtime
//! (`crate::search`, a separate SQLite store with its own worker).

use serde_json::{json, Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::codec::{self, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json::{self, python_str, truthy};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

mod clone;
mod diagnostic;
pub(crate) mod legacy;
mod search;
pub(crate) mod support;
mod usage;

use legacy::{archived_conversation_messages, archived_head_window, archived_tail_window, message_window_bounds};
use support::{
    int_or_zero, load, mark_conversation_search_projection_dirty, now_ms, prefix_chars, projection_from_turn_row,
    projection_head_state, py_int, py_str, sorted_repr, str_or, turn_public,
};

pub const ACTIVITY_DATE_BATCH_SIZE: usize = 64;
const ACTIVITY_ARCHIVE_BATCH_SIZE: usize = 4;
const ACTIVITY_DATE_MAX_INTERVALS: usize = 366;
const TITLE_FILTER_SCAN_PAGE_SIZE: i64 = 512;

pub fn register(registry: &mut Registry) {
    add(registry, "conversation.search", OperationSpec::query(search::conversation_search));
    add(registry, "conversation.diagnostic", OperationSpec::query(diagnostic::conversation_diagnostic));
    add(registry, "conversation.get", OperationSpec::query(conversation_get));
    add(registry, "conversation.list", OperationSpec::query(conversation_list));
    add(registry, "conversation.activity_dates", OperationSpec::query(conversation_activity_dates));
    add(registry, "conversation.turn_usage", OperationSpec::query(usage::conversation_turn_usage));
    add(registry, "conversation.count", OperationSpec::query(conversation_count));
    add(registry, "conversation.create", OperationSpec::command(true, conversation_create));
    add(registry, "conversation.settings.update", OperationSpec::command(true, conversation_settings_update));
    add(registry, "conversation.metadata.update", OperationSpec::command(true, conversation_metadata_update));
    add(registry, "conversation.delete", OperationSpec::command(true, conversation_delete));
    add(registry, "conversation.restore", OperationSpec::command(true, conversation_restore));
    add(registry, "conversation.clone", OperationSpec::command(true, conversation_clone));
    add(registry, "conversation.purge", OperationSpec::command(true, conversation_purge));
    add(registry, "conversation.trash.prune", OperationSpec::command(false, conversation_trash_prune));
}

fn protocol<T>(message: &str) -> OpResult<T> {
    fail("database_protocol_error", message)
}

/// `isinstance(value, int) and not isinstance(value, bool)`.
fn strict_int(value: &Value) -> Option<i64> {
    json::as_strict_int(value)
}

/// `_conversation_identity(payload)`.
pub fn conversation_identity(payload: &Payload) -> OpResult<(String, i64)> {
    let conv_id = required_text(payload, "conv_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    Ok((conv_id, user_id))
}

/// `_conversation_document(row, include_messages=, messages=, settings_keys=)`.
pub fn conversation_document(
    row: &Row,
    settings: Value,
    include_messages: bool,
    messages: Vec<Value>,
    settings_keys: Option<&[String]>,
) -> OpResult<Map<String, Value>> {
    let settings = if truthy(&settings) { settings } else { Value::Object(Map::new()) };
    let Value::Object(mut settings) = settings else {
        return fail("database_integrity", "Conversation document is malformed");
    };
    if let Some(keys) = settings_keys {
        let mut projected = Map::new();
        for key in keys {
            if let Some(value) = settings.get(key) {
                projected.insert(key.clone(), value.clone());
            }
        }
        settings = projected;
    }
    let metadata = json!({
        "id": py_str(row.require("id")?),
        "user_id": py_int(row.require("user_id")?)?,
        "title": str_or(row.require("title")?, ""),
        "created_at": py_int(row.require("created_at_ms")?)?,
        "updated_at": py_int(row.require("updated_at_ms")?)?,
        "settings": Value::Object(settings),
        "msg_count": py_int(row.require("msg_count")?)?,
        "search_text": "",
        "rev": py_int(row.require("rev")?)?,
    });
    let mut document = Map::new();
    document.insert("metadata".into(), metadata);
    document.insert("messages".into(), Value::Array(if include_messages { messages } else { Vec::new() }));
    document.insert("source".into(), Value::String("sidecar".into()));
    Ok(document)
}

fn row_document(
    row: &Row,
    include_messages: bool,
    messages: Vec<Value>,
    settings_keys: Option<&[String]>,
) -> OpResult<Map<String, Value>> {
    let settings = load(row.require("settings_json")?)?;
    conversation_document(row, settings, include_messages, messages, settings_keys)
}

// ── Turn → legacy message projection ──────────────────────────────────────

fn turn_actor_to_legacy_role(actor: &str) -> &'static str {
    if matches!(actor, "human" | "critic" | "virtual_user") {
        "user"
    } else {
        "assistant"
    }
}

fn not_none_or_empty(value: Option<&Value>) -> bool {
    !matches!(value, None | Some(Value::Null)) && value != Some(&Value::String(String::new()))
}

/// Python `max(0, int(number))` for an int/float JSON number.
fn nonnegative_int_of_number(value: &Value) -> Option<Value> {
    match value {
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                Some(Value::from(integer.max(0)))
            } else if let Some(integer) = number.as_u64() {
                Some(Value::from(integer))
            } else {
                let truncated = number.as_f64()?.trunc();
                Some(Value::from(if truncated <= 0.0 { 0 } else { truncated as i64 }))
            }
        }
        _ => None,
    }
}

/// `_with_legacy_compaction_fields`.
fn with_legacy_compaction_fields(projection: Map<String, Value>) -> Map<String, Value> {
    let Some(Value::Object(compaction)) = projection.get("compaction") else { return projection };
    if !py_eq(compaction.get("blockId").unwrap_or(&Value::Null), &Value::String("compaction".into())) {
        return projection;
    }
    let compaction = compaction.clone();
    let mut result = projection;
    result.insert("_isCompactionSummary".into(), Value::Bool(true));
    let archive_id = compaction.get("archiveId");
    if not_none_or_empty(archive_id) {
        result.insert("_compactionArchiveId".into(), Value::String(python_str(archive_id.unwrap())));
    }
    if let Some(value) = compaction.get("estimatedPromptTokens").and_then(nonnegative_int_of_number) {
        result.insert("_estimatedPromptTokens".into(), value);
    }
    if let Some(value) = compaction.get("foldedToolRounds").and_then(nonnegative_int_of_number) {
        result.insert("_foldedToolRounds".into(), value);
    }
    let marker_names = [
        ("archiveId", "archiveId"),
        ("conversationId", "convId"),
        ("trigger", "trigger"),
        ("timestamp", "ts"),
        ("tokensBefore", "tokensBefore"),
        ("tokensAfter", "tokensAfter"),
        ("messagesBefore", "msgsBefore"),
        ("messagesAfter", "msgsAfter"),
        ("reductionPercent", "reductionPct"),
        ("foldedToolRounds", "foldedToolRounds"),
    ];
    let mut marker = Map::new();
    for (canonical, legacy_name) in marker_names {
        if not_none_or_empty(compaction.get(canonical)) {
            marker.insert(legacy_name.into(), compaction[canonical].clone());
        }
    }
    if !marker.is_empty() {
        result.insert("_compactions".into(), Value::Array(vec![Value::Object(marker)]));
    }
    result
}

/// `_turn_to_legacy_message(turn)`.
fn turn_to_legacy_message(turn: Map<String, Value>) -> OpResult<Value> {
    let mut projection = match turn.get("projection") {
        Some(Value::Object(map)) => map.clone(),
        Some(value) if truthy(value) => return internal("dict() of a non-mapping projection"),
        _ => Map::new(),
    };
    projection.shift_remove("role");
    let projection = with_legacy_compaction_fields(projection);
    let created_at = match turn.get("createdAt") {
        Some(value) if truthy(value) => value.as_i64().unwrap_or(0),
        _ => 0,
    };
    let timestamp = match projection.get("timestamp") {
        Some(value) if truthy(value) => value.clone(),
        _ => Value::from(created_at),
    };
    let actor = turn.get("actor").filter(|value| truthy(value)).map(python_str).unwrap_or_default();
    let mut message = projection;
    let field = |key: &str| turn.get(key).cloned().unwrap_or(Value::Null);
    message.insert("role".into(), Value::String(turn_actor_to_legacy_role(&actor).into()));
    message.insert("_turnId".into(), field("turnId"));
    message.insert("_attemptId".into(), field("currentAttemptId"));
    message.insert("_turnActor".into(), field("actor"));
    message.insert("_turnKind".into(), field("kind"));
    message.insert(
        "_turnLaneId".into(),
        turn.get("laneId").filter(|value| truthy(value)).cloned().unwrap_or(Value::String("main".into())),
    );
    message.insert("_turnStatus".into(), field("status"));
    message.insert(
        "_turnSettlement".into(),
        turn.get("settlement").filter(|value| truthy(value)).cloned().unwrap_or(Value::Object(Map::new())),
    );
    message.insert("_commandPending".into(), Value::Null);
    message.insert(
        "_projectionRevision".into(),
        Value::from(turn.get("projectionRevision").and_then(Value::as_i64).unwrap_or(0)),
    );
    message.insert("timestamp".into(), timestamp);
    Ok(Value::Object(message))
}

fn legacy_message(session: &Session<'_>, row: &Row) -> OpResult<Value> {
    turn_to_legacy_message(turn_public(session, row)?)
}

/// `_derive_turn_messages`.
fn derive_turn_messages(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<Vec<Value>> {
    let rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND lane_id='main' \
         ORDER BY ordinal",
        &params![conv_id, user_id],
    )?;
    rows.iter().map(|row| legacy_message(session, row)).collect()
}

/// `_derive_turn_message_window`.
fn derive_turn_message_window(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    window: i64,
    before_sequence: Option<i64>,
) -> OpResult<(Vec<Value>, i64, i64, i64)> {
    let count_row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND lane_id='main'",
        &params![conv_id, user_id],
    )?;
    let total_count = match count_row {
        Some(row) => py_int(row.get("c"))?,
        None => 0,
    };
    let (start, end) = message_window_bounds(total_count, window, before_sequence);
    if start == end {
        return Ok((Vec::new(), total_count, start, end));
    }
    let rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND lane_id='main' \
         ORDER BY ordinal LIMIT ? OFFSET ?",
        &params![conv_id, user_id, end - start, start],
    )?;
    let messages = rows.iter().map(|row| legacy_message(session, row)).collect::<OpResult<Vec<_>>>()?;
    Ok((messages, total_count, start, end))
}

fn archived_messages_json(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<SqlValue> {
    let archive = session.fetch_one(
        "SELECT messages_json FROM storage_conversations \
         WHERE id=? AND user_id=?",
        &params![conv_id, user_id],
    )?;
    match archive {
        Some(row) => Ok(row.get("messages_json").clone()),
        None => fail("database_integrity", "Conversation archive disappeared"),
    }
}

// ── conversation.get ──────────────────────────────────────────────────────

/// `_main_lane_sequence`.
fn main_lane_sequence(session: &Session<'_>, conv_id: &str, user_id: i64, turn_id: &str) -> OpResult<Option<i64>> {
    let Some(anchor) = session.fetch_one(
        "SELECT ordinal FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND turn_id=? AND lane_id='main'",
        &params![conv_id, user_id, turn_id],
    )?
    else {
        return Ok(None);
    };
    let rank = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND lane_id='main' AND ordinal<?",
        &params![conv_id, user_id, py_int(anchor.get("ordinal"))?],
    )?;
    Ok(Some(match rank {
        Some(row) => py_int(row.get("c"))?,
        None => 0,
    }))
}

/// `_backfill_turn_message_counts`.
fn backfill_turn_message_counts(session: &Session<'_>, rows: &mut [Row]) -> OpResult<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let mut identities = Vec::with_capacity(rows.len());
    for row in rows.iter() {
        identities.push((py_str(row.require("id")?), py_int(row.require("user_id")?)?));
    }
    let predicate = vec!["(conversation_id=? AND user_id=?)"; identities.len()].join(" OR ");
    let mut values = Vec::with_capacity(identities.len() * 2);
    for (id, user) in &identities {
        values.push(SqlValue::from(id.as_str()));
        values.push(SqlValue::from(*user));
    }
    let counts = session.fetch_all(
        &format!(
            "SELECT conversation_id AS cid, user_id, count(*) AS n \
             FROM storage_conversation_turns \
             WHERE ({predicate}) AND lane_id = 'main' \
             GROUP BY conversation_id,user_id"
        ),
        &values,
    )?;
    let mut by_identity = std::collections::HashMap::new();
    for item in &counts {
        by_identity.insert((py_str(item.get("cid")), py_int(item.get("user_id"))?), py_int(item.get("n"))?);
    }
    for (row, identity) in rows.iter_mut().zip(identities) {
        if let Some(count) = by_identity.get(&identity) {
            row.set("msg_count", SqlValue::Integer(*count));
        }
    }
    Ok(())
}

fn conversation_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = conversation_identity(payload)?;
    let include_messages = payload.get("derive_messages").map(truthy).unwrap_or(true);
    let message_window = match payload.get("message_window") {
        None => 0,
        Some(value) => match strict_int(value) {
            Some(window) if (0..=500).contains(&window) => window,
            _ => return protocol("Invalid conversation message window"),
        },
    };
    let mut before_sequence = match get(payload, "before_sequence") {
        Value::Null => None,
        value => match strict_int(value) {
            Some(sequence) => Some(sequence),
            None => return protocol("Invalid conversation message cursor"),
        },
    };
    if before_sequence.is_some() && message_window == 0 {
        return protocol("Conversation message cursor requires a message window");
    }
    if !include_messages && (message_window != 0 || before_sequence.is_some()) {
        return protocol("Conversation message window requires transcript projection");
    }
    let anchor_turn_id = match get(payload, "anchor_turn_id") {
        Value::Null => None,
        Value::String(text)
            if (1..=128).contains(&text.chars().count()) && message_window != 0 && before_sequence.is_none() =>
        {
            Some(text.clone())
        }
        _ => return protocol("Invalid conversation message anchor"),
    };

    let Some(mut row) = session.fetch_one(
        "SELECT id, user_id, title, created_at_ms, \
         updated_at_ms, settings_json, msg_count, rev \
         FROM storage_conversations WHERE id = ? AND user_id = ?",
        &params![conv_id.as_str(), user_id],
    )?
    else {
        return Ok(Value::Null);
    };

    let mut messages = Vec::new();
    let mut message_page: Option<Map<String, Value>> = None;
    let mut anchor_sequence = None;
    if let Some(anchor) = &anchor_turn_id {
        anchor_sequence = main_lane_sequence(session, &conv_id, user_id, anchor)?;
        before_sequence = Some(match anchor_sequence {
            None => 0,
            Some(sequence) => sequence + 1 + message_window.div_euclid(2),
        });
    }
    if include_messages {
        if message_window != 0 {
            let (window_messages, mut total_count, mut start, mut end) =
                derive_turn_message_window(session, &conv_id, user_id, message_window, before_sequence)?;
            messages = window_messages;
            if total_count != 0 {
                row.set("msg_count", SqlValue::Integer(total_count));
            } else {
                let archive = archived_messages_json(session, &conv_id, user_id)?;
                let expected_count = py_int(row.get("msg_count"))?;
                let archived_window = match before_sequence {
                    None => archived_tail_window(&archive, message_window, expected_count),
                    Some(before) => archived_head_window(&archive, message_window, before, expected_count),
                };
                if let Some((window_messages, window_total, window_start, window_end)) = archived_window {
                    messages = window_messages;
                    total_count = window_total;
                    start = window_start;
                    end = window_end;
                } else {
                    let archived = archived_conversation_messages(&archive)?;
                    total_count = archived.len() as i64;
                    (start, end) = message_window_bounds(total_count, message_window, before_sequence);
                    messages = archived[start as usize..end as usize].to_vec();
                }
                row.set("msg_count", SqlValue::Integer(total_count));
            }
            let mut page = Map::new();
            page.insert("total_count".into(), Value::from(total_count));
            page.insert("start".into(), Value::from(start));
            page.insert("end".into(), Value::from(end));
            if anchor_turn_id.is_some() {
                page.insert("anchor_sequence".into(), anchor_sequence.map(Value::from).unwrap_or(Value::Null));
            }
            message_page = Some(page);
        } else {
            messages = derive_turn_messages(session, &conv_id, user_id)?;
            if !messages.is_empty() {
                row.set("msg_count", SqlValue::Integer(messages.len() as i64));
            } else {
                let archive = archived_messages_json(session, &conv_id, user_id)?;
                messages = archived_conversation_messages(&archive)?;
                row.set("msg_count", SqlValue::Integer(messages.len() as i64));
            }
        }
    } else {
        backfill_turn_message_counts(session, std::slice::from_mut(&mut row))?;
    }
    let mut document = row_document(&row, include_messages, messages, None)?;
    if let Some(page) = message_page {
        document.insert("message_page".into(), Value::Object(page));
    }
    Ok(Value::Object(document))
}

// ── conversation.list ─────────────────────────────────────────────────────

/// The `settings_keys` projection: a deduplicated list of at most 64 names.
fn settings_keys(payload: &Payload) -> OpResult<Option<Vec<String>>> {
    match get(payload, "settings_keys") {
        Value::Null => Ok(None),
        Value::Array(items) => {
            let mut keys: Vec<String> = Vec::new();
            for item in items {
                match item {
                    Value::String(key) if !key.is_empty() => {
                        if !keys.contains(key) {
                            keys.push(key.clone());
                        }
                    }
                    _ => return protocol("Invalid conversation settings projection"),
                }
            }
            keys.truncate(64);
            Ok(Some(keys))
        }
        _ => protocol("Invalid conversation settings projection"),
    }
}

/// `_conversation_catalog_page`.
fn conversation_catalog_page(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    if payload.get("include_messages").is_some_and(|value| *value != Value::Bool(false)) {
        return protocol("Conversation catalog pages cannot include transcripts");
    }
    if payload.get("order_by").is_some_and(|value| *value != Value::String("updated_at_desc".into())) {
        return protocol("Invalid conversation catalog ordering");
    }
    let limit = integer(payload, "limit", Some(500), Some(1), Some(1000))?;
    let settings_keys = settings_keys(payload)?;
    let folder_id =
        if payload.contains_key("folder_id") { Some(required_text(payload, "folder_id", 512)?) } else { None };
    let before_updated_at = match get(payload, "before_updated_at") {
        Value::Null => None,
        value => match strict_int(value) {
            Some(timestamp) if timestamp >= 0 => Some(timestamp),
            _ => return protocol("Invalid conversation catalog cursor timestamp"),
        },
    };
    let before_id = match payload.get("before_id") {
        None => String::new(),
        Some(Value::String(text)) if text.chars().count() <= 256 => text.clone(),
        _ => return protocol("Invalid conversation catalog cursor id"),
    };
    if before_updated_at.is_none() && !before_id.is_empty() {
        return protocol("Conversation catalog cursor id requires a timestamp");
    }
    let mut base_where = vec!["user_id = ?".to_owned()];
    let mut base_params = vec![SqlValue::from(user_id)];
    if let Some(folder) = folder_id {
        base_where.push("json_extract(settings_json, '$.folderId') = ?".into());
        base_params.push(SqlValue::from(folder));
    }
    let count_row = session.fetch_one(
        &format!("SELECT COUNT(*) AS c FROM storage_conversations WHERE {}", base_where.join(" AND ")),
        &base_params,
    )?;
    let total_count = match count_row {
        Some(row) => py_int(row.get("c"))?,
        None => 0,
    };
    let mut page_where = base_where.clone();
    let mut page_params = base_params.clone();
    if let Some(before) = before_updated_at {
        page_where.push("(updated_at_ms < ? OR (updated_at_ms = ? AND id < ?))".into());
        page_params.extend([SqlValue::from(before), SqlValue::from(before), SqlValue::from(before_id.as_str())]);
    }
    page_params.push(SqlValue::from(limit + 1));
    let mut rows = session.fetch_all(
        &format!(
            "SELECT id, user_id, title, created_at_ms, updated_at_ms, \
             settings_json, msg_count, rev FROM storage_conversations WHERE {} \
             ORDER BY updated_at_ms DESC, id DESC LIMIT ?",
            page_where.join(" AND ")
        ),
        &page_params,
    )?;
    let has_more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);
    backfill_turn_message_counts(session, &mut rows)?;
    let items = rows
        .iter()
        .map(|row| row_document(row, false, Vec::new(), settings_keys.as_deref()).map(Value::Object))
        .collect::<OpResult<Vec<_>>>()?;
    Ok(json!({"items": items, "total_count": total_count, "has_more": has_more}))
}

/// `_conversation_title_match_ids`.
fn conversation_title_match_ids(
    session: &Session<'_>,
    filters: &[String],
    values: &[SqlValue],
    order_by: &str,
    title_contains: &str,
    limit: i64,
) -> OpResult<Vec<String>> {
    if limit <= 0 {
        return Ok(Vec::new());
    }
    let needle = title_contains.to_lowercase();
    let mut matched_ids: Vec<String> = Vec::new();
    let mut cursor_updated_at: Option<i64> = None;
    let mut cursor_id = String::new();
    while (matched_ids.len() as i64) < limit {
        let mut page_where = filters.to_vec();
        let mut page_params = values.to_vec();
        if let Some(updated_at) = cursor_updated_at {
            page_where.push("(updated_at_ms < ? OR (updated_at_ms = ? AND id < ?))".into());
            page_params.extend([
                SqlValue::from(updated_at),
                SqlValue::from(updated_at),
                SqlValue::from(cursor_id.as_str()),
            ]);
        } else if !cursor_id.is_empty() {
            page_where.push("id > ?".into());
            page_params.push(SqlValue::from(cursor_id.as_str()));
        }
        page_params.push(SqlValue::from(TITLE_FILTER_SCAN_PAGE_SIZE));
        let predicate = if page_where.is_empty() { "1 = 1".to_owned() } else { page_where.join(" AND ") };
        let order = if order_by == "updated_at_desc" {
            " ORDER BY updated_at_ms DESC, id DESC LIMIT ?"
        } else {
            " ORDER BY id ASC LIMIT ?"
        };
        let rows = session.fetch_all(
            &format!("SELECT id,title,updated_at_ms FROM storage_conversations WHERE {predicate}{order}"),
            &page_params,
        )?;
        if rows.is_empty() {
            break;
        }
        for row in &rows {
            if str_or(row.get("title"), "").to_lowercase().contains(&needle) {
                matched_ids.push(py_str(row.get("id")));
                if matched_ids.len() as i64 >= limit {
                    break;
                }
            }
        }
        if (rows.len() as i64) < TITLE_FILTER_SCAN_PAGE_SIZE {
            break;
        }
        let last_row = rows.last().expect("nonempty page");
        cursor_id = py_str(last_row.get("id"));
        if order_by == "updated_at_desc" {
            cursor_updated_at = Some(py_int(last_row.get("updated_at_ms"))?);
        }
    }
    Ok(matched_ids)
}

fn placeholders(count: usize) -> String {
    vec!["?"; count].join(",")
}

fn conversation_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let catalog_page = match payload.get("catalog_page") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid conversation catalog mode"),
    };
    if catalog_page {
        return conversation_catalog_page(session, payload);
    }
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let project_path =
        if payload.contains_key("project_path") { Some(required_text(payload, "project_path", 4096)?) } else { None };
    let title_contains = if payload.contains_key("title_contains") {
        Some(required_text(payload, "title_contains", 512)?)
    } else {
        None
    };
    let ids = match get(payload, "ids") {
        Value::Null => None,
        Value::Array(items) => {
            let mut ids: Vec<String> = Vec::new();
            for item in items.iter().filter(|item| truthy(item)) {
                let text = python_str(item);
                if !ids.contains(&text) {
                    ids.push(text);
                }
            }
            if ids.is_empty() {
                return Ok(Value::Array(Vec::new()));
            }
            Some(ids)
        }
        _ => return protocol("Invalid conversation id filter"),
    };
    let mut time_filters = Vec::new();
    for key in ["updated_at_gte", "updated_at_gt", "created_at_lt"] {
        let value = match get(payload, key) {
            Value::Null => None,
            value => match strict_int(value) {
                Some(timestamp) => Some(timestamp),
                None => return protocol("Invalid conversation time filter"),
            },
        };
        time_filters.push(value);
    }
    let (updated_at_gte, updated_at_gt, created_at_lt) = (time_filters[0], time_filters[1], time_filters[2]);
    let order_by = match payload.get("order_by") {
        None => "updated_at_desc".to_owned(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(_) | Value::Object(_)) => return internal("TypeError: unhashable order_by"),
        Some(_) => String::new(),
    };
    let order_sql = match order_by.as_str() {
        "updated_at_desc" => "updated_at_ms DESC, id DESC",
        "id_asc" => "id ASC",
        _ => return protocol("Invalid conversation ordering"),
    };
    let include_messages = match payload.get("include_messages") {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid conversation message projection"),
    };
    let settings_keys = settings_keys(payload)?;
    let metadata_only = !include_messages
        && updated_at_gte.is_none()
        && updated_at_gt.is_none()
        && created_at_lt.is_none()
        && ids.is_none()
        && project_path.is_none()
        && title_contains.is_none();
    let default_limit = if metadata_only { 10000 } else { 1000 };
    let limit = integer(payload, "limit", Some(default_limit), Some(0), Some(10000))?;
    let mut filters: Vec<String> = vec!["user_id = ?".into()];
    let mut values: Vec<SqlValue> = vec![SqlValue::from(user_id)];
    if let Some(path) = &project_path {
        filters.push("json_extract(settings_json, '$.projectPath') = ?".into());
        values.push(SqlValue::from(path.as_str()));
    }
    if let Some(ids) = &ids {
        filters.push(format!("id IN ({})", placeholders(ids.len())));
        values.extend(ids.iter().map(|id| SqlValue::from(id.as_str())));
    }
    if let Some(value) = updated_at_gte {
        filters.push("updated_at_ms >= ?".into());
        values.push(SqlValue::from(value));
    }
    if let Some(value) = updated_at_gt {
        filters.push("updated_at_ms > ?".into());
        values.push(SqlValue::from(value));
    }
    if let Some(value) = created_at_lt {
        filters.push("created_at_ms < ?".into());
        values.push(SqlValue::from(value));
    }
    if let Some(title) = &title_contains {
        let matched = conversation_title_match_ids(session, &filters, &values, &order_by, title, limit)?;
        if matched.is_empty() {
            return Ok(Value::Array(Vec::new()));
        }
        filters.push(format!("id IN ({})", placeholders(matched.len())));
        values.extend(matched.into_iter().map(SqlValue::from));
    }
    let settings_projection =
        if settings_keys.as_ref().is_some_and(Vec::is_empty) { "'{}' AS settings_json" } else { "settings_json" };
    let projection = if include_messages {
        format!(
            "id, user_id, title, messages_json, created_at_ms, updated_at_ms, {settings_projection}, msg_count, rev "
        )
    } else {
        format!("id, user_id, title, created_at_ms, updated_at_ms, {settings_projection}, msg_count, rev ")
    };
    values.push(SqlValue::from(limit));
    let mut rows = session.fetch_all(
        &format!(
            "SELECT {projection}FROM storage_conversations WHERE {} ORDER BY {order_sql} LIMIT ?",
            filters.join(" AND ")
        ),
        &values,
    )?;
    backfill_turn_message_counts(session, &mut rows)?;
    let projected =
        if include_messages { derive_turn_messages_bulk(session, &rows)? } else { vec![Vec::new(); rows.len()] };
    let documents = rows
        .iter()
        .zip(projected)
        .map(|(row, messages)| {
            row_document(row, include_messages, messages, settings_keys.as_deref()).map(Value::Object)
        })
        .collect::<OpResult<Vec<_>>>()?;
    Ok(Value::Array(documents))
}

/// `_derive_turn_messages_bulk`: per-row projected messages.
fn derive_turn_messages_bulk(session: &Session<'_>, rows: &[Row]) -> OpResult<Vec<Vec<Value>>> {
    if rows.is_empty() {
        return Ok(Vec::new());
    }
    let mut identities = Vec::with_capacity(rows.len());
    for row in rows {
        identities.push((py_str(row.require("id")?), py_int(row.require("user_id")?)?));
    }
    let predicate = vec!["(conversation_id=? AND user_id=?)"; identities.len()].join(" OR ");
    let mut values = Vec::new();
    for (id, user) in &identities {
        values.push(SqlValue::from(id.as_str()));
        values.push(SqlValue::from(*user));
    }
    let turn_rows = session.fetch_all(
        &format!(
            "SELECT * FROM storage_conversation_turns \
             WHERE ({predicate}) AND lane_id = 'main' \
             ORDER BY user_id,conversation_id,ordinal"
        ),
        &values,
    )?;
    let mut by_identity: std::collections::HashMap<(String, i64), Vec<&Row>> = std::collections::HashMap::new();
    for turn_row in &turn_rows {
        let identity = (py_str(turn_row.require("conversation_id")?), py_int(turn_row.require("user_id")?)?);
        by_identity.entry(identity).or_default().push(turn_row);
    }
    let mut result = Vec::with_capacity(rows.len());
    for (row, identity) in rows.iter().zip(identities) {
        let projected = by_identity
            .get(&identity)
            .map(|turns| turns.iter().map(|turn| legacy_message(session, turn)).collect::<OpResult<Vec<_>>>())
            .transpose()?
            .unwrap_or_default();
        result.push(if projected.is_empty() {
            archived_conversation_messages(row.get("messages_json"))?
        } else {
            projected
        });
    }
    Ok(result)
}

// ── conversation.activity_dates ───────────────────────────────────────────

/// Python `int(text)` for a decimal literal (`ValueError` → `None`).
fn python_int_text(text: &str) -> Option<i128> {
    let text = support::py_strip(text);
    let (sign, digits) = match text.as_bytes().first() {
        Some(b'+') => (1, &text[1..]),
        Some(b'-') => (-1, &text[1..]),
        _ => (1, text),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    if !digits.bytes().all(|byte| byte.is_ascii_digit() || byte == b'_') {
        return None;
    }
    let cleaned: String = digits.chars().filter(|character| *character != '_').collect();
    cleaned.parse::<i128>().ok().map(|value| value * sign)
}

fn saturate(value: i128) -> i64 {
    value.clamp(i64::MIN as i128, i64::MAX as i128) as i64
}

/// `_activity_timestamp(value)` for a JSON value.
fn activity_timestamp_json(value: &Value) -> OpResult<i64> {
    Ok(match value {
        Value::Null => 0,
        Value::Bool(flag) => *flag as i64,
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                integer
            } else if let Some(integer) = number.as_u64() {
                saturate(integer as i128)
            } else {
                let real = number.as_f64().unwrap_or(f64::NAN);
                if real.is_nan() {
                    0
                } else if real.is_infinite() {
                    return internal("OverflowError: cannot convert float infinity to integer");
                } else {
                    real.trunc() as i64
                }
            }
        }
        Value::String(text) => python_int_text(text).map(saturate).unwrap_or(0),
        _ => 0,
    })
}

/// `_activity_timestamp(value)` for a SQLite value.
fn activity_timestamp_sql(value: &SqlValue) -> OpResult<i64> {
    match value {
        SqlValue::Null => Ok(0),
        SqlValue::Integer(integer) => Ok(*integer),
        SqlValue::Real(real) => activity_timestamp_json(&json::float_value(*real)),
        SqlValue::Text(text) => Ok(python_int_text(text).map(saturate).unwrap_or(0)),
        SqlValue::Blob(bytes) => {
            Ok(std::str::from_utf8(bytes).ok().and_then(python_int_text).map(saturate).unwrap_or(0))
        }
    }
}

/// `_activity_interval`.
fn activity_interval(timestamp_ms: i64, boundaries_ms: &[i64]) -> Option<usize> {
    if timestamp_ms < boundaries_ms[0] || timestamp_ms >= *boundaries_ms.last().expect("boundaries") {
        return None;
    }
    let index = boundaries_ms.partition_point(|boundary| *boundary <= timestamp_ms) as isize - 1;
    if index >= 0 && (index as usize) < boundaries_ms.len() - 1 {
        Some(index as usize)
    } else {
        None
    }
}

fn header_fallback(header: &Row) -> OpResult<i64> {
    let updated = header.get("updated_at_ms");
    let created = header.get("created_at_ms");
    let chosen = if updated.truthy() {
        updated
    } else if created.truthy() {
        created
    } else {
        return Ok(0);
    };
    activity_timestamp_sql(chosen)
}

fn conversation_activity_dates(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let mut parsed = Vec::new();
    for key in ["updated_at_gte", "created_at_lt"] {
        parsed.push(match get(payload, key) {
            Value::Null => None,
            value => match strict_int(value) {
                Some(timestamp) => Some(timestamp),
                None => return protocol("Invalid conversation time filter"),
            },
        });
    }
    let (updated_at_gte, created_at_lt) = (parsed[0], parsed[1]);
    let Some(updated_at_gte) = updated_at_gte else {
        return protocol("updated_at_gte is required");
    };
    let boundaries_ms: Vec<i64> = match get(payload, "day_boundaries_ms") {
        Value::Array(items) if (2..=ACTIVITY_DATE_MAX_INTERVALS + 1).contains(&items.len()) => {
            let parsed: Option<Vec<i64>> = items.iter().map(strict_int).collect();
            match parsed {
                Some(values) if values.windows(2).all(|pair| pair[0] < pair[1]) => values,
                _ => return protocol("Invalid activity date boundaries"),
            }
        }
        _ => return protocol("Invalid activity date boundaries"),
    };
    let limit = integer(payload, "limit", Some(10_000), Some(1), Some(10_000))?;
    let mut filters = vec!["user_id = ?", "updated_at_ms >= ?"];
    let mut values = vec![SqlValue::from(user_id), SqlValue::from(updated_at_gte)];
    if let Some(created) = created_at_lt {
        filters.push("created_at_ms < ?");
        values.push(SqlValue::from(created));
    }
    values.push(SqlValue::from(limit));
    let candidates = session.fetch_all(
        &format!(
            "SELECT id,created_at_ms,updated_at_ms FROM storage_conversations WHERE {} \
             ORDER BY updated_at_ms DESC,id DESC LIMIT ?",
            filters.join(" AND ")
        ),
        &values,
    )?;
    let mut counts = vec![0i64; boundaries_ms.len() - 1];
    for batch in candidates.chunks(ACTIVITY_DATE_BATCH_SIZE) {
        let mut ids: Vec<String> = Vec::new();
        let mut by_id: std::collections::HashMap<String, &Row> = std::collections::HashMap::new();
        for row in batch {
            let id = py_str(row.get("id"));
            if by_id.insert(id.clone(), row).is_none() {
                ids.push(id);
            }
        }
        let mut turn_values = vec![SqlValue::from(user_id)];
        turn_values.extend(ids.iter().map(|id| SqlValue::from(id.as_str())));
        let turn_rows = session.fetch_all(
            &format!(
                "SELECT t.turn_id,t.conversation_id,t.conversation_id AS cid,t.user_id,\
                 t.current_attempt_id,t.status,t.projection_revision,\
                 t.projection_checkpoint_revision,\
                 t.projection_materialized_revision,t.projection_patch_count,\
                 t.projection_patch_bytes,cp.turn_id AS checkpoint_turn_id,\
                 t.created_at AS turn_created_at,\
                 json_extract(COALESCE(cp.projection_json,t.projection_json), '$.timestamp') AS projection_timestamp \
                 FROM storage_conversation_turns AS t LEFT JOIN \
                 storage_turn_projection_checkpoints AS cp ON \
                 cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id \
                 AND cp.user_id=t.user_id AND cp.attempt_id=t.current_attempt_id \
                 AND cp.projection_revision=t.projection_checkpoint_revision \
                 WHERE t.user_id=? AND t.lane_id='main' \
                 AND t.conversation_id IN ({}) ORDER BY t.conversation_id,t.ordinal",
                placeholders(ids.len())
            ),
            &turn_values,
        )?;
        let mut turn_backed = std::collections::HashSet::new();
        let mut active_by_id: Vec<(String, std::collections::BTreeSet<usize>)> =
            ids.iter().map(|id| (id.clone(), std::collections::BTreeSet::new())).collect();
        for turn in &turn_rows {
            let conversation_id = py_str(turn.get("cid"));
            let Some(header) = by_id.get(&conversation_id) else { continue };
            turn_backed.insert(conversation_id.clone());
            let mut projected_json: Option<Value> = None;
            let projected_sql = turn.get("projection_timestamp");
            let has_external_projection = !turn.get("projection_checkpoint_revision").is_null()
                || !turn.get("projection_materialized_revision").is_null()
                || turn.get("projection_patch_count").truthy()
                || turn.get("projection_patch_bytes").truthy();
            if has_external_projection {
                let head = projection_head_state(turn)?;
                if head.checkpoint_active() && turn.get("checkpoint_turn_id").is_null() {
                    return fail("database_integrity", "Turn projection checkpoint is missing");
                }
                if head.active() {
                    let projection = projection_from_turn_row(session, turn)?;
                    projected_json = Some(projection.get("timestamp").cloned().unwrap_or(Value::Null));
                }
            }
            let fallback = header_fallback(header)?;
            let message_timestamp = match &projected_json {
                Some(value) if truthy(value) => activity_timestamp_json(value)?,
                Some(_) => activity_timestamp_sql(turn.get("turn_created_at"))?,
                None if projected_sql.truthy() => activity_timestamp_sql(projected_sql)?,
                None => activity_timestamp_sql(turn.get("turn_created_at"))?,
            };
            let timestamp_ms = if message_timestamp != 0 { message_timestamp } else { fallback };
            if let Some(interval) = activity_interval(timestamp_ms, &boundaries_ms) {
                if let Some((_, active)) = active_by_id.iter_mut().find(|(id, _)| *id == conversation_id) {
                    active.insert(interval);
                }
            }
        }
        let legacy_ids: Vec<String> = ids.iter().filter(|id| !turn_backed.contains(*id)).cloned().collect();
        for archive_ids in legacy_ids.chunks(ACTIVITY_ARCHIVE_BATCH_SIZE) {
            let mut archive_values = vec![SqlValue::from(user_id)];
            archive_values.extend(archive_ids.iter().map(|id| SqlValue::from(id.as_str())));
            let archives = session.fetch_all(
                &format!(
                    "SELECT id,messages_json FROM storage_conversations \
                     WHERE user_id=? AND id IN ({}) ORDER BY id",
                    placeholders(archive_ids.len())
                ),
                &archive_values,
            )?;
            let mut by_archive_id: std::collections::HashMap<String, &Row> = std::collections::HashMap::new();
            for row in &archives {
                by_archive_id.insert(py_str(row.get("id")), row);
            }
            let expected: std::collections::HashSet<&String> = archive_ids.iter().collect();
            let actual: std::collections::HashSet<&String> = by_archive_id.keys().collect();
            if expected != actual {
                return fail("database_integrity", "Conversation archive disappeared");
            }
            for conversation_id in archive_ids {
                let header = by_id[conversation_id];
                let fallback = header_fallback(header)?;
                for message in archived_conversation_messages(by_archive_id[conversation_id].get("messages_json"))? {
                    let raw = message.get("timestamp").cloned().unwrap_or(Value::from(0));
                    let timestamp = activity_timestamp_json(&raw)?;
                    let timestamp_ms = if timestamp != 0 { timestamp } else { fallback };
                    if let Some(interval) = activity_interval(timestamp_ms, &boundaries_ms) {
                        if let Some((_, active)) = active_by_id.iter_mut().find(|(id, _)| id == conversation_id) {
                            active.insert(interval);
                        }
                    }
                }
            }
        }
        for (_, active) in active_by_id {
            for interval in active {
                counts[interval] += 1;
            }
        }
    }
    Ok(json!({"candidate_count": candidates.len(), "counts": counts}))
}

fn conversation_count(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let row =
        session.fetch_one("SELECT COUNT(*) AS c FROM storage_conversations WHERE user_id = ?", &params![user_id])?;
    let count = match row {
        Some(row) => py_int(row.get("c"))?,
        None => 0,
    };
    Ok(json!({"count": count}))
}

// ── Header commands ───────────────────────────────────────────────────────

fn conversation_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = conversation_identity(payload)?;
    let settings = match get(payload, "settings") {
        value if !truthy(value) => Value::Object(Map::new()),
        value @ Value::Object(_) => value.clone(),
        _ => return protocol("Invalid conversation settings"),
    };
    let title = match get(payload, "title") {
        value if !truthy(value) => "New Chat".to_owned(),
        Value::String(text) => text.clone(),
        _ => return protocol("Invalid conversation title"),
    };
    let now = now_ms();
    let created_at = integer(payload, "created_at", Some(now), Some(0), None)?;
    let updated_at = integer(payload, "updated_at", Some(created_at), Some(0), None)?;
    session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
    let active_id =
        session.fetch_one("SELECT 1 AS present FROM storage_conversations WHERE id=?", &params![conv_id.as_str()])?;
    let trashed_id = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_trash \
         WHERE conversation_id=?",
        &params![conv_id.as_str()],
    )?;
    if active_id.is_some() || trashed_id.is_some() {
        return fail("database_conflict", "Conversation already exists");
    }
    session.execute(
        "INSERT INTO storage_conversations \
         (id,user_id,title,messages_json,created_at_ms,updated_at_ms,\
         settings_json,msg_count,search_text,rev) \
         VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0)",
        &params![
            conv_id.as_str(),
            user_id,
            prefix_chars(&title, 500),
            codec::dump_param(&json!([]))?,
            created_at,
            updated_at,
            codec::dump_param(&settings)?,
            "",
        ],
    )?;
    mark_conversation_search_projection_dirty(session, &conv_id, user_id)?;
    Ok(json!({"applied": true, "rev": 0}))
}

fn conversation_settings_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = conversation_identity(payload)?;
    let Value::Object(updates) = get(payload, "updates") else {
        return protocol("Invalid conversation settings updates");
    };
    let replace = match payload.get("replace") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid conversation settings replace flag"),
    };
    let expected_settings = get(payload, "expected_settings");
    if replace && !expected_settings.is_object() {
        return protocol("Replacing conversation settings requires expected_settings");
    }
    session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
    let Some(row) = session.fetch_one(
        "SELECT settings_json, rev FROM storage_conversations \
         WHERE id = ? AND user_id = ?",
        &params![conv_id.as_str(), user_id],
    )?
    else {
        return Ok(json!({"applied": false, "missing": true, "rev": null}));
    };
    let actual = py_int(row.get("rev"))?;
    if !get(payload, "expected_rev").is_null() {
        let expected = integer(payload, "expected_rev", None, Some(0), None)?;
        if expected != actual {
            return Ok(json!({"applied": false, "missing": false, "rev": actual}));
        }
    }
    let settings = load(row.get("settings_json"))?;
    let settings = if truthy(&settings) { settings } else { Value::Object(Map::new()) };
    let Value::Object(current) = settings else {
        return fail("database_integrity", "Conversation settings are malformed");
    };
    let merged = if replace {
        if !py_eq(&Value::Object(current), expected_settings) {
            return Ok(json!({"applied": false, "missing": false, "conflict": true, "rev": actual}));
        }
        updates.clone()
    } else {
        let mut merged = current;
        for (key, value) in updates {
            merged.insert(key.clone(), value.clone());
        }
        merged
    };
    session.execute(
        "UPDATE storage_conversations SET settings_json = ? \
         WHERE id = ? AND user_id = ?",
        &params![codec::dump_param(&Value::Object(merged))?, conv_id.as_str(), user_id],
    )?;
    Ok(json!({"applied": true, "missing": false, "conflict": false, "rev": actual}))
}

fn conversation_metadata_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = conversation_identity(payload)?;
    let updates = match get(payload, "updates") {
        Value::Object(updates) if !updates.is_empty() => updates,
        _ => return protocol("Invalid conversation metadata updates"),
    };
    let unknown: Vec<String> =
        updates.keys().filter(|key| !matches!(key.as_str(), "title" | "updated_at")).cloned().collect();
    if !unknown.is_empty() {
        return protocol(&format!("Unsupported conversation metadata updates: {}", sorted_repr(unknown)));
    }
    let title = match updates.get("title") {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(text.clone()),
        Some(_) => return protocol("Invalid conversation title"),
    };
    let updated_at = match updates.get("updated_at") {
        None | Some(Value::Null) => None,
        Some(value) => match strict_int(value) {
            Some(timestamp) if timestamp >= 0 => Some(timestamp),
            _ => return protocol("Invalid update timestamp"),
        },
    };
    session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
    let Some(row) = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.as_str(), user_id],
    )?
    else {
        return Ok(json!({"applied": false, "missing": true, "rev": null}));
    };
    let mut assignments = Vec::new();
    let mut values = Vec::new();
    if let Some(title) = title {
        assignments.push("title=?");
        values.push(SqlValue::from(title));
    }
    if let Some(updated_at) = updated_at {
        assignments.push("updated_at_ms=?");
        values.push(SqlValue::from(updated_at));
    }
    values.push(SqlValue::from(conv_id.as_str()));
    values.push(SqlValue::from(user_id));
    session.execute(
        &format!("UPDATE storage_conversations SET {} WHERE id=? AND user_id=?", assignments.join(",")),
        &values,
    )?;
    mark_conversation_search_projection_dirty(session, &conv_id, user_id)?;
    Ok(json!({"applied": true, "missing": false, "rev": py_int(row.get("rev"))?}))
}

// ── Trash lifecycle ───────────────────────────────────────────────────────

/// Private port of `operations_pkg/_model_continuation.py::_prefix`; shared owner: archives.
pub fn continuation_prefix(user_id: i64, conversation_id: &str) -> String {
    use sha2::{Digest, Sha256};
    format!("{user_id}:{}:", hex::encode(Sha256::digest(conversation_id.as_bytes())))
}

/// `delete_conversation_continuations` (`_model_continuation.py`).
fn delete_conversation_continuations(session: &Session<'_>, conversation_id: &str, user_id: i64) -> OpResult<()> {
    session.execute(
        "DELETE FROM storage_records WHERE namespace=? AND record_key LIKE ?",
        &params!["model.continuation.v1", format!("{}%", continuation_prefix(user_id, conversation_id))],
    )?;
    Ok(())
}

/// `_drop_active_conversation_rows`.
fn drop_active_conversation_rows(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    retain_recoverable_rows: bool,
) -> OpResult<bool> {
    mark_conversation_search_projection_dirty(session, conv_id, user_id)?;
    delete_conversation_continuations(session, conv_id, user_id)?;
    let live_rows = session.fetch_all(
        "SELECT turn_id,conversation_id,user_id,current_attempt_id \
         FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? \
         AND current_attempt_id IS NOT NULL",
        &params![conv_id, user_id],
    )?;
    for row in &live_rows {
        crate::ops::turns::head::discard_projection_cache_for_row(session, row)?;
    }
    let timer_ids =
        session.fetch_all("SELECT id FROM storage_timers WHERE conv_id=? AND user_id=?", &params![conv_id, user_id])?;
    for timer in &timer_ids {
        session.execute("DELETE FROM storage_timer_poll_log WHERE timer_id=?", &[timer.get("id").clone()])?;
    }
    let identity = params![conv_id, user_id];
    session.execute("DELETE FROM storage_timers WHERE conv_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_queue_items WHERE conv_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_autopilot_markers WHERE conv_id=? AND user_id=?", &identity)?;
    session.execute(
        "UPDATE storage_scheduled_tasks SET enabled=0 \
         WHERE user_id=? AND (target_conv_id=? OR source_conv_id=?)",
        &params![user_id, conv_id, conv_id],
    )?;
    session.execute(
        "DELETE FROM storage_attempt_events WHERE attempt_id IN (\
         SELECT a.attempt_id FROM storage_generation_attempts AS a \
         JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id \
         WHERE t.conversation_id=? AND t.user_id=?)",
        &identity,
    )?;
    support::debit_stored_bytes_for_delete(session, "conversation_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_raw_archives WHERE conversation_id=? AND user_id=?", &identity)?;
    session.execute(
        "DELETE FROM storage_generation_attempts WHERE turn_id IN (\
         SELECT turn_id FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=?)",
        &identity,
    )?;
    session
        .execute("DELETE FROM storage_turn_projection_checkpoints WHERE conversation_id=? AND user_id=?", &identity)?;
    if !retain_recoverable_rows {
        session.execute("DELETE FROM storage_compaction_archives WHERE conversation_id=? AND user_id=?", &identity)?;
    }
    session.execute("DELETE FROM storage_conversation_turns WHERE conversation_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_turn_tombstones WHERE conversation_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_conversation_changes WHERE conversation_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_conversation_sync_heads WHERE conversation_id=? AND user_id=?", &identity)?;
    let deleted = session.execute("DELETE FROM storage_conversations WHERE id = ? AND user_id = ?", &identity)?;
    Ok(deleted != 0)
}

/// `_purge_conversation_identity`.
fn purge_conversation_identity(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<bool> {
    let active = drop_active_conversation_rows(session, conv_id, user_id, false)?;
    let identity = params![conv_id, user_id];
    session.execute("DELETE FROM storage_compaction_archives WHERE conversation_id=? AND user_id=?", &identity)?;
    let trashed_turns = session
        .execute("DELETE FROM storage_conversation_trash_turns WHERE conversation_id=? AND user_id=?", &identity)?;
    let trashed_header =
        session.execute("DELETE FROM storage_conversation_trash WHERE conversation_id=? AND user_id=?", &identity)?;
    Ok(active || trashed_turns != 0 || trashed_header != 0)
}

/// `_materialize_external_turn_projections_for_trash`.
fn materialize_external_turn_projections_for_trash(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<()> {
    let rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND (\
         projection_checkpoint_revision IS NOT NULL OR \
         projection_materialized_revision IS NOT NULL OR \
         projection_patch_count<>0 OR projection_patch_bytes<>0) \
         ORDER BY turn_id",
        &params![conv_id, user_id],
    )?;
    for row in &rows {
        let projection = projection_from_turn_row(session, row)?;
        let changed = session.execute(
            "UPDATE storage_conversation_trash_turns SET projection_json=? \
             WHERE conversation_id=? AND user_id=? AND turn_id=?",
            &params![codec::dump_param(&projection)?, conv_id, user_id, py_str(row.get("turn_id"))],
        )?;
        if changed != 1 {
            return fail("database_integrity", "Turn disappeared while its trash projection was materialized");
        }
    }
    Ok(())
}

/// `header[column] or _dump([])`: the frozen transcript bytes pass through.
fn messages_or_empty(value: &SqlValue) -> OpResult<SqlValue> {
    if value.truthy() {
        Ok(value.clone())
    } else {
        codec::dump_param(&json!([]))
    }
}

fn conversation_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = conversation_identity(payload)?;
    session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
    let identity = params![conv_id.as_str(), user_id];
    let Some(header) = session.fetch_one(
        "SELECT title,messages_json,created_at_ms,updated_at_ms,settings_json,\
         msg_count,rev \
         FROM storage_conversations WHERE id=? AND user_id=?",
        &identity,
    )?
    else {
        let already_deleted = session
            .fetch_one(
                "SELECT 1 AS present FROM storage_conversation_trash \
                 WHERE conversation_id=? AND user_id=?",
                &identity,
            )?
            .is_some();
        return Ok(json!({"deleted": false, "alreadyDeleted": already_deleted}));
    };
    let now = now_ms();
    let count_row = session.fetch_one(
        "SELECT COUNT(*) AS c FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND lane_id='main'",
        &identity,
    )?;
    let main_count = match count_row {
        Some(row) => int_or_zero(row.get("c"))?,
        None => 0,
    };
    let archived_count = int_or_zero(header.get("msg_count"))?;
    let trash_msg_count = if archived_count != 0 { archived_count } else { main_count };
    session.execute("DELETE FROM storage_conversation_trash_turns WHERE conversation_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_conversation_trash WHERE conversation_id=? AND user_id=?", &identity)?;
    session.execute(
        "INSERT INTO storage_conversation_trash(\
         conversation_id,user_id,title,messages_json,created_at_ms,\
         updated_at_ms,settings_json,msg_count,rev,deleted_at_ms) \
         VALUES (?,?,?,?,?,?,?,?,?,?)",
        &params![
            conv_id.as_str(),
            user_id,
            str_or(header.get("title"), "New Chat"),
            messages_or_empty(header.get("messages_json"))?,
            int_or_zero(header.get("created_at_ms"))?,
            int_or_zero(header.get("updated_at_ms"))?,
            codec::dump_param(&Value::Object(clone::settings_without_live_runtime(header.get("settings_json"))?))?,
            trash_msg_count,
            int_or_zero(header.get("rev"))?,
            now,
        ],
    )?;
    session.execute(
        "INSERT INTO storage_conversation_trash_turns(\
         conversation_id,user_id,turn_id,lane_id,parent_turn_id,ordinal,actor,\
         kind,run_id,status,projection_json,projection_revision,\
         settlement_json,created_at,updated_at) \
         SELECT conversation_id,user_id,turn_id,lane_id,parent_turn_id,ordinal,\
         actor,kind,run_id,status,projection_json,projection_revision,\
         settlement_json,created_at,updated_at \
         FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=?",
        &identity,
    )?;
    materialize_external_turn_projections_for_trash(session, &conv_id, user_id)?;
    session.execute(
        "UPDATE storage_conversation_trash_turns \
         SET status='interrupted',settlement_json=?,updated_at=? \
         WHERE conversation_id=? AND user_id=? \
         AND status IN ('pending','running')",
        &params![
            codec::dump_param(&json!({
                "outcome": "interrupted",
                "cause": "conversation_deleted",
                "resumeOptions": [],
            }))?,
            now,
            conv_id.as_str(),
            user_id,
        ],
    )?;
    if !drop_active_conversation_rows(session, &conv_id, user_id, true)? {
        return fail("database_integrity", "Conversation disappeared during delete");
    }
    Ok(json!({"deleted": true, "recoverable": true, "deletedAt": now}))
}

fn conversation_restore(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = conversation_identity(payload)?;
    session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
    if session
        .fetch_one("SELECT 1 AS present FROM storage_conversations WHERE id=?", &params![conv_id.as_str()])?
        .is_some()
    {
        return Ok(json!({"restored": false, "conflict": true, "missing": false}));
    }
    let identity = params![conv_id.as_str(), user_id];
    let Some(header) = session.fetch_one(
        "SELECT * FROM storage_conversation_trash \
         WHERE conversation_id=? AND user_id=?",
        &identity,
    )?
    else {
        return Ok(json!({"restored": false, "conflict": false, "missing": true}));
    };
    let restored_revision = int_or_zero(header.get("rev"))? + 1;
    let settings = clone::settings_without_live_runtime(header.get("settings_json"))?;
    session.execute(
        "INSERT INTO storage_conversations(\
         id,user_id,title,messages_json,created_at_ms,updated_at_ms,\
         settings_json,msg_count,search_text,rev) \
         VALUES (?,?,?,?,?,?,?,?,?,?)",
        &params![
            conv_id.as_str(),
            user_id,
            str_or(header.get("title"), "New Chat"),
            messages_or_empty(header.get("messages_json"))?,
            int_or_zero(header.get("created_at_ms"))?,
            int_or_zero(header.get("updated_at_ms"))?,
            codec::dump_param(&Value::Object(settings))?,
            int_or_zero(header.get("msg_count"))?,
            "",
            restored_revision,
        ],
    )?;
    session.execute(
        "INSERT INTO storage_conversation_turns(\
         turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,\
         kind,run_id,status,current_attempt_id,projection_json,\
         projection_revision,settlement_json,created_at,updated_at) \
         SELECT turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,\
         actor,kind,run_id,status,NULL,projection_json,projection_revision,\
         settlement_json,created_at,updated_at \
         FROM storage_conversation_trash_turns \
         WHERE conversation_id=? AND user_id=?",
        &identity,
    )?;
    let restored_turns = session.fetch_all(
        "SELECT turn_id FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? ORDER BY lane_id,ordinal",
        &identity,
    )?;
    mark_conversation_search_projection_dirty(session, &conv_id, user_id)?;
    session.execute("DELETE FROM storage_conversation_trash_turns WHERE conversation_id=? AND user_id=?", &identity)?;
    session.execute("DELETE FROM storage_conversation_trash WHERE conversation_id=? AND user_id=?", &identity)?;
    Ok(json!({
        "restored": true,
        "conflict": false,
        "missing": false,
        "rev": restored_revision,
        "turnCount": restored_turns.len(),
    }))
}

fn conversation_clone(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (source_id, user_id) = conversation_identity(payload)?;
    clone::conversation_clone(session, payload, &source_id, user_id)
}

fn conversation_purge(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = conversation_identity(payload)?;
    session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
    Ok(json!({"purged": purge_conversation_identity(session, &conv_id, user_id)?}))
}

fn conversation_trash_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let deleted_before = integer(payload, "deleted_before_ms", None, Some(1), None)?;
    let maximum = integer(payload, "max_conversations", Some(4), Some(1), Some(64))?;
    let rows = session.fetch_all(
        "SELECT conversation_id,user_id FROM storage_conversation_trash \
         WHERE deleted_at_ms<? \
         ORDER BY deleted_at_ms,conversation_id,user_id LIMIT ?",
        &params![deleted_before, maximum],
    )?;
    let mut purged = 0;
    for row in &rows {
        let conv_id = py_str(row.get("conversation_id"));
        let user_id = py_int(row.get("user_id"))?;
        session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
        purged += purge_conversation_identity(session, &conv_id, user_id)? as i64;
    }
    let remaining = session
        .fetch_one(
            "SELECT 1 AS present FROM storage_conversation_trash \
             WHERE deleted_at_ms<? LIMIT 1",
            &params![deleted_before],
        )?
        .is_some();
    Ok(json!({"purgedConversations": purged, "remaining": remaining}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_int_parsing_and_intervals() {
        assert_eq!(python_int_text(" 1_000 "), Some(1000));
        assert_eq!(python_int_text("1.5"), None);
        assert_eq!(python_int_text("-7"), Some(-7));
        assert_eq!(activity_interval(5, &[1, 5, 9]), Some(1));
        assert_eq!(activity_interval(9, &[1, 5, 9]), None);
    }
}
