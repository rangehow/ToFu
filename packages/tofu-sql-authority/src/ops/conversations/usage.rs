//! `operations_pkg/_conversation_usage.py`: `conversation.turn_usage`, one
//! conversation's per-Turn usage projection in a turn-time window.

use serde_json::{json, Map, Value};

use super::legacy::archived_conversation_messages;
use super::support::{load, projection_from_turn_row, projection_head_state, py_int, py_str, str_or};
use super::ACTIVITY_DATE_BATCH_SIZE;
use crate::error::{fail, OpResult};
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Session, SqlValue};

const USAGE_ROW_FIELDS: &[&str] = &["model", "preset", "effort", "provider_id", "providerId"];
const SETTINGS_FIELDS: &[&str] = &["model", "preset", "effort"];
const MAX_TITLE_FALLBACK_CHARACTERS: i64 = 256;

/// `tolerant_turn_timestamp(value)` (0 = no usable time).
fn tolerant_turn_timestamp(value: &Value) -> i128 {
    match value {
        Value::Bool(flag) => *flag as i128,
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                integer as i128
            } else if let Some(integer) = number.as_u64() {
                integer as i128
            } else {
                let real = number.as_f64().unwrap_or(f64::NAN);
                if real.is_finite() {
                    real as i128
                } else {
                    0
                }
            }
        }
        Value::String(text) => {
            let text = super::support::py_strip(text);
            let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
            if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
                return 0;
            }
            text.parse::<i128>().unwrap_or(0)
        }
        _ => 0,
    }
}

fn sql_json(value: &SqlValue) -> Value {
    value.to_json().unwrap_or(Value::Null)
}

fn timestamp_value(value: i128) -> Value {
    if let Ok(small) = i64::try_from(value) {
        Value::from(small)
    } else if let Ok(large) = u64::try_from(value) {
        Value::from(large)
    } else {
        crate::json::float_value(value as f64)
    }
}

/// `_usage_row`.
fn usage_row(message: &Map<String, Value>, index: usize, timestamp: Option<Value>) -> Option<Value> {
    let usage = message.get("usage").and_then(Value::as_object).cloned().unwrap_or_default();
    let has_usage = !usage.is_empty();
    let mut row = Map::new();
    row.insert("index".into(), Value::from(index));
    row.insert("usage".into(), Value::Object(usage));
    if let Some(timestamp) = timestamp {
        row.insert("timestamp".into(), timestamp);
    }
    for field in USAGE_ROW_FIELDS {
        if let Some(value) = message.get(*field) {
            row.insert((*field).into(), value.clone());
        }
    }
    if let Some(rounds) = message.get("apiRounds").and_then(Value::as_array) {
        if rounds.iter().any(|item| {
            item.get("usage").is_some_and(Value::is_object)
                || item.get("cost").is_some_and(Value::is_object)
                || item.get("usageReceiptId").and_then(Value::as_str).is_some_and(|s| !s.is_empty())
                || (item.get("taskId").and_then(Value::as_str).is_some_and(|s| !s.is_empty())
                    && item
                        .get("sourceRound")
                        .or_else(|| item.get("round"))
                        .and_then(Value::as_u64)
                        .is_some_and(|n| n > 0))
        }) {
            let fields = [
                "usageReceiptId",
                "taskId",
                "sourceRound",
                "round",
                "model",
                "provider_id",
                "providerId",
                "accountId",
                "subscriptionProvider",
                "requestTimestampMs",
                "pricingEvidence",
                "usage",
                "cost",
            ];
            let receipt_rows = rounds
                .iter()
                .map(|item| {
                    Value::Object(
                        fields
                            .iter()
                            .filter_map(|&field| item.get(field).map(|value| (field.to_owned(), value.clone())))
                            .collect(),
                    )
                })
                .collect();
            let receipt_rows: Vec<Value> = receipt_rows;
            let receipt_rows = receipt_rows
                .into_iter()
                .map(|mut row| {
                    if let Some(usage) = row.get_mut("usage").and_then(Value::as_object_mut) {
                        usage.retain(|key, _| !key.starts_with('_'));
                    }
                    row
                })
                .collect();
            row.insert("apiRounds".to_owned(), Value::Array(receipt_rows));
        }
    }
    (has_usage || row.contains_key("apiRounds")).then_some(Value::Object(row))
}

/// `_title_fallback`.
fn title_fallback(message: &Value, characters: i64) -> String {
    match message.get("content") {
        Some(Value::String(content)) => content.chars().take(characters as usize).collect(),
        _ => String::new(),
    }
}

/// `_selected_turn_rows`.
fn selected_turn_rows(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    start: i64,
    end: i64,
) -> OpResult<(usize, Vec<(usize, String)>)> {
    let turns = session.fetch_all(
        "SELECT t.turn_id,t.conversation_id,t.user_id,t.current_attempt_id,\
         t.status,t.projection_revision,t.projection_checkpoint_revision,\
         t.projection_materialized_revision,t.projection_patch_count,\
         t.projection_patch_bytes,cp.turn_id AS checkpoint_turn_id,\
         t.created_at AS turn_created_at,json_extract(COALESCE(cp.projection_json,t.projection_json), '$.timestamp') AS projection_timestamp \
         FROM storage_conversation_turns AS t LEFT JOIN \
         storage_turn_projection_checkpoints AS cp ON \
         cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id \
         AND cp.user_id=t.user_id AND cp.attempt_id=t.current_attempt_id \
         AND cp.projection_revision=t.projection_checkpoint_revision \
         WHERE t.user_id=? AND t.conversation_id=? AND t.lane_id='main' \
         ORDER BY t.ordinal",
        &params![user_id, conv_id],
    )?;
    let mut selected = Vec::new();
    for (index, turn) in turns.iter().enumerate() {
        let mut projected = sql_json(turn.get("projection_timestamp"));
        if !turn.get("projection_checkpoint_revision").is_null()
            || !turn.get("projection_materialized_revision").is_null()
            || turn.get("projection_patch_count").truthy()
            || turn.get("projection_patch_bytes").truthy()
        {
            let head = projection_head_state(turn)?;
            if head.checkpoint_active() && turn.get("checkpoint_turn_id").is_null() {
                return fail("database_integrity", "Turn projection checkpoint is missing");
            }
            if head.active() {
                projected = projection_from_turn_row(session, turn)?.get("timestamp").cloned().unwrap_or(Value::Null);
            }
        }
        let mut turn_time = tolerant_turn_timestamp(&projected);
        if turn_time == 0 {
            let created = turn.get("turn_created_at");
            turn_time = if created.truthy() { py_int(created)? as i128 } else { 0 };
        }
        if turn_time == 0 || (start as i128 <= turn_time && turn_time < end as i128) {
            selected.push((index, py_str(turn.get("turn_id"))));
        }
    }
    Ok((turns.len(), selected))
}

/// `_turn_usage_rows`.
fn turn_usage_rows(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    start: i64,
    end: i64,
    selected: &[(usize, String)],
) -> OpResult<Vec<Value>> {
    let mut rows = Vec::new();
    for batch in selected.chunks(ACTIVITY_DATE_BATCH_SIZE) {
        let mut values = vec![SqlValue::from(user_id), SqlValue::from(conv_id)];
        values.extend(batch.iter().map(|(_, turn_id)| SqlValue::from(turn_id.as_str())));
        let fetched = session.fetch_all(
            &format!(
                "SELECT * FROM storage_conversation_turns WHERE user_id=? \
                 AND conversation_id=? AND lane_id='main' AND turn_id IN ({})",
                vec!["?"; batch.len()].join(",")
            ),
            &values,
        )?;
        let by_id: std::collections::HashMap<String, &crate::session::Row> =
            fetched.iter().map(|row| (py_str(row.get("turn_id")), row)).collect();
        for (index, turn_id) in batch {
            let Some(turn) = by_id.get(turn_id) else {
                return fail("database_integrity", "Turn disappeared during a usage read");
            };
            let projection = projection_from_turn_row(session, turn)?;
            let mut turn_time = tolerant_turn_timestamp(projection.get("timestamp").unwrap_or(&Value::Null));
            if turn_time == 0 {
                let created = turn.get("created_at");
                turn_time = if created.truthy() { py_int(created)? as i128 } else { 0 };
            }
            if turn_time != 0 && !(start as i128 <= turn_time && turn_time < end as i128) {
                continue;
            }
            let Value::Object(message) = &projection else { continue };
            if let Some(row) = usage_row(message, *index, Some(timestamp_value(turn_time))) {
                rows.push(row);
            }
        }
    }
    Ok(rows)
}

/// `_first_turn_fallback`.
fn first_turn_fallback(session: &Session<'_>, conv_id: &str, user_id: i64, characters: i64) -> OpResult<String> {
    let Some(first) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE user_id=? \
         AND conversation_id=? AND lane_id='main' ORDER BY ordinal LIMIT 1",
        &params![user_id, conv_id],
    )?
    else {
        return Ok(String::new());
    };
    Ok(title_fallback(&projection_from_turn_row(session, &first)?, characters))
}

/// `_archive_usage`.
fn archive_usage(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    start: i64,
    end: i64,
    characters: i64,
) -> OpResult<(usize, String, Vec<Value>)> {
    let Some(archive) = session.fetch_one(
        "SELECT messages_json FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id, user_id],
    )?
    else {
        return fail("database_integrity", "Conversation archive disappeared");
    };
    let messages = archived_conversation_messages(archive.get("messages_json"))?;
    let mut rows = Vec::new();
    for (index, message) in messages.iter().enumerate() {
        let Value::Object(message) = message else { continue };
        let raw = message.get("timestamp");
        if let Some(stamp) = raw.and_then(crate::json::as_strict_int) {
            if stamp != 0 && !(start <= stamp && stamp < end) {
                continue;
            }
        } else if let Some(Value::Number(number)) = raw {
            if number.as_u64().is_some() && number.as_i64().is_none() {
                continue;
            }
        }
        let timestamp =
            if message.contains_key("timestamp") { Some(raw.cloned().unwrap_or(Value::Null)) } else { None };
        if let Some(row) = usage_row(message, index, timestamp) {
            rows.push(row);
        }
    }
    let fallback = match messages.first() {
        Some(first) if characters != 0 => title_fallback(first, characters),
        _ => String::new(),
    };
    Ok((messages.len(), fallback, rows))
}

/// `_window(payload)`.
fn window(payload: &Payload) -> OpResult<(i64, i64, i64)> {
    let start = payload.get("timestamp_gte").and_then(crate::json::as_strict_int);
    let end = payload.get("timestamp_lt").and_then(crate::json::as_strict_int);
    let (Some(start), Some(end)) = (start, end) else {
        return fail("database_protocol_error", "Invalid conversation usage window");
    };
    if start >= end {
        return fail("database_protocol_error", "Invalid conversation usage window");
    }
    let characters =
        integer(payload, "title_fallback_characters", Some(0), Some(0), Some(MAX_TITLE_FALLBACK_CHARACTERS))?;
    Ok((start, end, characters))
}

/// `_conversation_turn_usage`.
pub fn conversation_turn_usage(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = required_text(payload, "conv_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let (start, end, characters) = window(payload)?;
    let Some(header) = session.fetch_one(
        "SELECT id,title,created_at_ms,updated_at_ms,settings_json \
         FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.as_str(), user_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let settings = load(header.get("settings_json"))?;
    let settings = if crate::json::truthy(&settings) { settings } else { Value::Object(Map::new()) };
    let Value::Object(settings) = settings else {
        return fail("database_integrity", "Conversation document is malformed");
    };
    let title = str_or(header.get("title"), "");
    let (mut message_count, selected) = selected_turn_rows(session, &conv_id, user_id, start, end)?;
    let (fallback, turns) = if message_count != 0 {
        let turns = turn_usage_rows(session, &conv_id, user_id, start, end, &selected)?;
        let fallback = if characters != 0 && title.is_empty() {
            first_turn_fallback(session, &conv_id, user_id, characters)?
        } else {
            String::new()
        };
        (fallback, turns)
    } else {
        let (count, fallback, turns) =
            archive_usage(session, &conv_id, user_id, start, end, if title.is_empty() { characters } else { 0 })?;
        message_count = count;
        (fallback, turns)
    };
    let mut projected_settings = Map::new();
    for field in SETTINGS_FIELDS {
        if let Some(value) = settings.get(*field) {
            projected_settings.insert((*field).into(), value.clone());
        }
    }
    Ok(json!({
        "metadata": {
            "id": py_str(header.get("id")),
            "title": title,
            "created_at": py_int(header.get("created_at_ms"))?,
            "updated_at": py_int(header.get("updated_at_ms"))?,
            "settings": projected_settings,
        },
        "message_count": message_count,
        "title_fallback": fallback,
        "turns": turns,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tolerant_timestamps_match_the_native_engine() {
        assert_eq!(tolerant_turn_timestamp(&json!(" +12 ")), 12);
        assert_eq!(tolerant_turn_timestamp(&json!("1.5")), 0);
        assert_eq!(tolerant_turn_timestamp(&json!(2.9)), 2);
        assert_eq!(tolerant_turn_timestamp(&json!(true)), 1);
        assert_eq!(tolerant_turn_timestamp(&json!(null)), 0);
    }
}
