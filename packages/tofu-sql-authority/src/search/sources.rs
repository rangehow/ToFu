//! `turn_search_sources.py`: bounded owner-scoped search readers; SQL selects
//! messages, never transcripts.
//!
//! Python raises `ValueError(reason)` for malformed/oversized/changed
//! sources; the port carries it as `Failure::Internal("ValueError:<reason>")`
//! so an enclosing authority query still classifies it exactly like any
//! other non-storage exception (`database_internal`).

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use super::chunks::{field_text, FIELDS};
use crate::codec::STORAGE_PROJECTION_CODEC_KEY;
use crate::error::{Failure, OpResult};
use crate::json;
use crate::ops::conversations::legacy::{decode_archived_sequence, ARCHIVED_MESSAGE_CODEC_KEY};
use crate::ops::conversations::support::projection_from_turn_row;
use crate::params;
use crate::session::{Row, Session, SqlValue};

pub const MAX_BYTES: i64 = 2_000_000;
pub const MAX_MESSAGE_BYTES: i64 = 250_000;
pub const ARCHIVED_SOURCE_PREFIX: &str = "history:";

const VALUE_ERROR: &str = "ValueError:";

pub fn value_error(reason: &str) -> Failure {
    Failure::Internal(format!("{VALUE_ERROR}{reason}"))
}

/// `str(exc)` when the failure is a Python `ValueError` (orjson's
/// `JSONDecodeError` included), else `None`.
pub fn value_error_reason(failure: &Failure) -> Option<&str> {
    match failure {
        Failure::Internal(reason) => {
            if let Some(stripped) = reason.strip_prefix(VALUE_ERROR) {
                Some(stripped)
            } else if reason.starts_with("orjson.JSONDecodeError") {
                Some("orjson.JSONDecodeError")
            } else {
                None
            }
        }
        _ => None,
    }
}

/// `_one`: malformed JSON in SQL becomes `ValueError('malformed_source')`.
fn one(session: &Session<'_>, sql: &str, params: &[SqlValue]) -> OpResult<Option<Row>> {
    match session.fetch_one(sql, params) {
        Err(Failure::Sqlite(rusqlite::Error::SqliteFailure(error, Some(message))))
            if error.code == rusqlite::ErrorCode::Unknown && message.contains("malformed JSON") =>
        {
            Err(value_error("malformed_source"))
        }
        other => other,
    }
}

fn length(expression: &str) -> String {
    format!("length(CAST(({expression}) AS BLOB))")
}

fn json_field(expression: &str, key: &str) -> String {
    format!("json_extract({expression},'$.{key}')")
}

fn public_fields(projection: &Value) -> OpResult<Value> {
    let Value::Object(projection) = projection else { return Err(value_error("malformed_source")) };
    if projection.contains_key(STORAGE_PROJECTION_CODEC_KEY) || projection.contains_key(ARCHIVED_MESSAGE_CODEC_KEY) {
        return Err(value_error("malformed_source"));
    }
    let mut fields = Map::new();
    for key in FIELDS {
        fields.insert(key.into(), Value::String(field_text(projection.get(key))));
    }
    Ok(Value::Object(fields))
}

fn selected(expression: &str) -> String {
    let args: Vec<String> =
        FIELDS.iter().chain(["role"].iter()).map(|key| format!("'{key}',{}", json_field(expression, key))).collect();
    format!("json_object({})", args.join(","))
}

/// `orjson.loads(value) if isinstance(value, (str, bytes)) else value`.
fn loads_column(value: &SqlValue) -> OpResult<Value> {
    let decoded = match value {
        SqlValue::Text(text) => json::loads(text.as_bytes()),
        SqlValue::Blob(bytes) => json::loads(bytes),
        SqlValue::Null => return Ok(Value::Null),
        SqlValue::Integer(integer) => return Ok(Value::from(*integer)),
        SqlValue::Real(real) => return Ok(json::float_value(*real)),
    };
    decoded.map_err(|_| value_error("orjson.JSONDecodeError"))
}

fn int_or_zero(value: &SqlValue) -> OpResult<i64> {
    if !value.truthy() {
        return Ok(0);
    }
    value.as_int().ok_or_else(|| Failure::Internal("int() of a non-integer column".into()))
}

/// `turn_snapshot(session, user_id, turn_id)`.
pub fn turn_snapshot(session: &Session<'_>, user_id: i64, turn_id: &str) -> OpResult<Option<Row>> {
    let Some(mut row) = one(
        session,
        "SELECT t.conversation_id,t.user_id,t.turn_id,t.lane_id,t.ordinal,t.actor,t.status,\
         t.projection_revision,t.updated_at,t.current_attempt_id,t.projection_checkpoint_revision,\
         t.projection_materialized_revision,t.projection_patch_count,t.projection_patch_bytes,\
         c.title AS conversation_title,c.updated_at_ms AS conversation_updated_at_ms \
         FROM storage_conversation_turns t JOIN storage_conversations c \
         ON c.id=t.conversation_id AND c.user_id=t.user_id WHERE t.user_id=? AND t.turn_id=?",
        &params![user_id, turn_id],
    )?
    else {
        return Ok(None);
    };
    let where_ = " FROM storage_conversation_turns WHERE user_id=? AND turn_id=? AND projection_revision=?";
    let params = params![user_id, turn_id, row.get("projection_revision")];
    let codec = format!("json_type(projection_json,'$.{STORAGE_PROJECTION_CODEC_KEY}')");
    let shape = "json_type(projection_json)";
    let Some(marker) = one(session, &format!("SELECT {codec} IS NOT NULL AS codec,{shape} AS shape{where_}"), &params)?
    else {
        return Err(value_error("source_changed"));
    };
    if marker.get("shape").as_str() != Some("object") {
        return Err(value_error("malformed_source"));
    }
    let checkpoint_revision = row.get("projection_checkpoint_revision").clone();
    let full = !checkpoint_revision.is_null()
        || !row.get("projection_materialized_revision").is_null()
        || marker.get("codec").truthy();
    let projection = if full {
        let (base_where, base_params): (&str, Vec<SqlValue>) = if !checkpoint_revision.is_null() {
            let attempt = row.get("current_attempt_id");
            let attempt = if attempt.truthy() { attempt.clone() } else { SqlValue::Text(String::new()) };
            (
                " FROM storage_turn_projection_checkpoints WHERE user_id=? AND turn_id=? \
                 AND conversation_id=? AND attempt_id=? AND projection_revision=?",
                params![user_id, turn_id, row.get("conversation_id"), attempt, &checkpoint_revision].to_vec(),
            )
        } else {
            (where_, params.to_vec())
        };
        let Some(size) = one(session, &format!("SELECT {} AS n{base_where}", length("projection_json")), &base_params)?
        else {
            return Err(value_error("malformed_source"));
        };
        if (int_or_zero(size.get("n"))? + int_or_zero(row.get("projection_patch_bytes"))?) * 8 > MAX_BYTES {
            return Err(value_error("source_budget"));
        }
        if checkpoint_revision.is_null() {
            let Some(base) = one(session, &format!("SELECT projection_json{where_}"), &params)? else {
                return Err(value_error("source_changed"));
            };
            row.set("projection_json", base.get("projection_json").clone());
        }
        projection_from_turn_row(session, &row)?
    } else {
        let selected = selected("projection_json");
        let Some(size) = one(session, &format!("SELECT {} AS n{where_}", length(&selected)), &params)? else {
            return Err(value_error("source_changed"));
        };
        if int_or_zero(size.get("n"))? * 8 > MAX_BYTES {
            return Err(value_error("source_budget"));
        }
        let Some(selected_row) = one(session, &format!("SELECT {selected} AS value{where_}"), &params)? else {
            return Err(value_error("source_changed"));
        };
        loads_column(selected_row.get("value"))?
    };
    row.set("projection_json", SqlValue::Blob(json::dumps(&public_fields(&projection)?)));
    Ok(Some(row))
}

/// One `history_page` result.
pub struct HistoryPage {
    pub rows: Vec<Row>,
    pub cursor: String,
    pub remaining: bool,
    pub skipped: i64,
}

fn parse_history_cursor(cursor: &str, owner: i64, conv: &str) -> Option<(i64, String, i64)> {
    let Ok(Value::Array(items)) = json::loads(cursor.as_bytes()) else { return None };
    let [saved_owner, saved_conv, kind, source, index] = items.as_slice() else { return None };
    if !crate::codec::py_eq(saved_owner, &Value::from(owner)) || saved_conv.as_str() != Some(conv) {
        return None;
    }
    let kind = json::as_strict_int(kind).filter(|kind| *kind == 0 || *kind == 1)?;
    let source = source.as_str()?.to_owned();
    let index = json::as_strict_int(index).filter(|index| *index >= 0)?;
    Some((kind, source, index))
}

/// `history_page(session, identity, cursor)`.
pub fn history_page(session: &Session<'_>, owner: i64, conv: &str, cursor: &str) -> OpResult<HistoryPage> {
    let (mut kind, mut source, mut index) = (0i64, String::new(), 0i64);
    if !cursor.is_empty() {
        match parse_history_cursor(cursor, owner, conv) {
            Some(parsed) => (kind, source, index) = parsed,
            None => return Err(value_error("invalid_history_cursor")),
        }
    }
    let mut rows = Vec::new();
    let mut reasons: BTreeMap<&'static str, i64> = BTreeMap::new();
    let mut used = 0i64;
    let mut remaining = true;
    for _ in 0..8 {
        let (where_, params): (&str, Vec<SqlValue>) = if kind == 0 {
            (
                " FROM storage_conversations c WHERE user_id=? AND id=? AND NOT EXISTS \
                 (SELECT 1 FROM storage_conversation_turns t WHERE t.user_id=c.user_id AND t.conversation_id=c.id)",
                params![owner, conv].to_vec(),
            )
        } else {
            (
                " FROM storage_compaction_archives WHERE user_id=? AND conversation_id=? AND archive_id=?",
                params![owner, conv, source.as_str()].to_vec(),
            )
        };
        let count_expr = "CASE WHEN json_type(messages_json)='array' THEN json_array_length(messages_json) ELSE -1 END";
        let counted = (|| -> OpResult<i64> {
            let count = one(session, &format!("SELECT {count_expr} AS n{where_}"), &params)?;
            let n = match count {
                Some(count) => count.get("n").as_int().ok_or_else(|| Failure::Internal("int(None)".into()))?,
                None => 0,
            };
            if n < 0 {
                return Err(value_error("malformed_source"));
            }
            Ok(n)
        })();
        let n = match counted {
            Ok(n) => n,
            Err(failure) if value_error_reason(&failure).is_some() => {
                *reasons.entry("malformed_source").or_default() += 1;
                0
            }
            Err(failure) => return Err(failure),
        };
        if index >= n {
            let next_source = one(
                session,
                "SELECT archive_id FROM storage_compaction_archives \
                 WHERE user_id=? AND conversation_id=? AND archive_id>? ORDER BY archive_id LIMIT 1",
                &params![owner, conv, source.as_str()],
            )?;
            let Some(next_source) = next_source else {
                remaining = false;
                break;
            };
            kind = 1;
            source = next_source.get("archive_id").text_or_empty();
            index = 0;
            continue;
        }
        let message = format!("json_extract(messages_json,'$[{index}]')");
        let outcome = (|| -> OpResult<bool> {
            let size = one(session, &format!("SELECT {} AS n{where_}", length(&message)), &params)?
                .ok_or_else(|| Failure::Internal("TypeError: 'NoneType' object is not subscriptable".into()))?;
            let mut charge = int_or_zero(size.get("n"))? * 8;
            if charge > MAX_BYTES {
                return Err(value_error("source_budget"));
            }
            if used + charge > MAX_BYTES {
                return Ok(false);
            }
            let raw = one(session, &format!("SELECT {message} AS value{where_}"), &params)?
                .ok_or_else(|| Failure::Internal("TypeError: 'NoneType' object is not subscriptable".into()))?;
            let value = loads_column(raw.get("value"))?;
            let Value::Object(map) = &value else { return Err(value_error("malformed_source")) };
            if let Some(envelope) = map.get(ARCHIVED_MESSAGE_CODEC_KEY) {
                let declared = match envelope {
                    Value::Object(envelope) => envelope.get("decodedBytes").and_then(json::as_strict_int),
                    _ => None,
                };
                let Some(declared) = declared.filter(|declared| *declared >= 1) else {
                    return Err(value_error("malformed_source"));
                };
                if declared > MAX_MESSAGE_BYTES {
                    return Err(value_error("source_budget"));
                }
                charge = charge.max(declared * 8);
                if used + charge > MAX_BYTES {
                    return Ok(false);
                }
            }
            used += charge;
            let decoded = match decode_archived_sequence(&Value::Array(vec![value])) {
                Ok(mut decoded) => decoded.remove(0),
                Err(()) => return Err(value_error("malformed_source")),
            };
            let role = decoded.get("role").and_then(Value::as_str).unwrap_or_default().to_owned();
            if role == "user" || role == "assistant" {
                let mut row = Row::default();
                row.set("user_id", SqlValue::Integer(owner));
                row.set("conversation_id", SqlValue::Text(conv.to_owned()));
                let address = json::dumps(&Value::Array(vec![Value::String(source.clone()), Value::from(index)]));
                row.set(
                    "turn_id",
                    SqlValue::Text(format!("{ARCHIVED_SOURCE_PREFIX}{}", String::from_utf8_lossy(&address))),
                );
                row.set("ordinal", SqlValue::Integer(index));
                row.set("actor", SqlValue::Text(role));
                row.set("lane_id", SqlValue::Text("main".into()));
                row.set("status", SqlValue::Text("settled".into()));
                row.set("projection_json", SqlValue::Blob(json::dumps(&public_fields(&decoded)?)));
                rows.push(row);
            }
            Ok(true)
        })();
        match outcome {
            Ok(true) => {}
            Ok(false) => break,
            Err(failure) => match value_error_reason(&failure) {
                Some(reason) => {
                    let reason = if reason == "source_budget" { "source_budget" } else { "malformed_source" };
                    *reasons.entry(reason).or_default() += 1;
                }
                None => return Err(failure),
            },
        }
        index += 1;
    }
    let next_cursor = json::dumps(&Value::Array(vec![
        Value::from(owner),
        Value::String(conv.to_owned()),
        Value::from(kind),
        Value::String(source),
        Value::from(index),
    ]));
    Ok(HistoryPage {
        rows,
        cursor: String::from_utf8_lossy(&next_cursor).into_owned(),
        remaining,
        skipped: reasons.values().sum(),
    })
}
