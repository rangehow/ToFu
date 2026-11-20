//! `operations_pkg/_turns_read.py`: turn/attempt reads, event replay pages
//! and conversation-sync snapshots (`turn.visible.sync` lives in
//! `visible.rs`).

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::core::{
    attempt_public, col, conversation_change_from_row, conversation_sync_head, now_ms, py_int, stored_object, text,
    turn_identity, turn_public, turn_public_metadata, Obj, SYNC_PRIVATE_SETTING_KEYS,
};
use super::head::projection_from_turn_row;
use super::normalize::background_command_completion;
use super::pages::{stale_turn_projection_page, turn_projection_page};
use crate::codec::load;
use crate::error::{fail, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const LINEAGE_HEX_DIGITS: usize = 16;
const CONVERSATION_CHANGE_DELETE_KEY_CHUNK: usize = 256;
const DELTA_OVERLAP_MS: i64 = 5000;
const TOMBSTONE_RETENTION_MS: i64 = 7 * 24 * 60 * 60 * 1000;
const MAX_TURN_IMAGES: i64 = 20;
const CHANGE_PAGE_MIN_BYTES: i64 = 65_536;
const CHANGE_PAGE_MAX_BYTES: i64 = 16_777_216;
pub const CONVERSATION_EXECUTOR_DISPATCH_MODE: &str = "conversation_executor";
const I64_MAX: i64 = i64::MAX;

const CHANGE_ROWS_FROM: &str = "FROM storage_conversation_changes AS c LEFT JOIN storage_attempt_events AS e ON c.attempt_sequence IS NOT NULL AND e.attempt_id=c.attempt_id AND e.sequence=c.attempt_sequence AND e.conversation_id=c.conversation_id AND e.turn_id=c.turn_id WHERE c.conversation_id=? AND c.user_id=? ";
const CHANGE_ROW_COLUMNS: &str = "SELECT c.conversation_id,c.user_id,c.sync_sequence,c.change_type,c.turn_id,c.attempt_id,c.attempt_sequence,c.event_json,c.created_at,e.payload_json AS attempt_event_json ";

fn sync_lineage_epoch(session: &Session<'_>) -> OpResult<String> {
    if session.event_read_epoch.is_empty() {
        return fail("database_internal", "Conversation sync lineage requires the backend open identity");
    }
    let mut digest = Sha256::new();
    digest.update(b"tofu.conversation-sync.lineage-epoch\0");
    digest.update(session.event_read_epoch.as_bytes());
    Ok(hex::encode(digest.finalize())[..LINEAGE_HEX_DIGITS].to_owned())
}

fn change_row_digest(row: &Row) -> String {
    let mut columns = vec!["change_type", "turn_id", "attempt_id", "attempt_sequence", "created_at", "event_json"];
    if !row.get("attempt_sequence").is_null() {
        columns.push("attempt_event_json");
    }
    let mut digest = Sha256::new();
    for column in columns {
        let encoded: Vec<u8> = match row.get(column) {
            SqlValue::Null => b"n".to_vec(),
            SqlValue::Blob(bytes) => [b"b".as_slice(), bytes].concat(),
            SqlValue::Text(text) => [b"s".as_slice(), text.as_bytes()].concat(),
            SqlValue::Integer(value) => [b"i".as_slice(), value.to_string().as_bytes()].concat(),
            SqlValue::Real(value) => {
                let decoded = crate::json::float_value(*value);
                [b"j".as_slice(), super::normalize::py_json_dumps(&decoded, true).as_bytes()].concat()
            }
        };
        digest.update((encoded.len() as u64).to_be_bytes());
        digest.update(&encoded);
    }
    hex::encode(digest.finalize())[..LINEAGE_HEX_DIGITS].to_owned()
}

fn change_digest_at(session: &Session<'_>, conv_id: &str, user_id: i64, sequence: i64) -> OpResult<Value> {
    if sequence <= 0 {
        return Ok(Value::Null);
    }
    let sql = format!("{CHANGE_ROW_COLUMNS}{CHANGE_ROWS_FROM}AND c.sync_sequence=?");
    Ok(match session.fetch_one(&sql, &params![conv_id, user_id, sequence])? {
        Some(row) => Value::String(change_row_digest(&row)),
        None => Value::Null,
    })
}

fn with_sync_lineage(
    mut result: Obj,
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    head: i64,
    head_digest: Option<Value>,
) -> OpResult<Value> {
    result.insert("authorityEpoch".into(), Value::String(sync_lineage_epoch(session)?));
    let digest = match head_digest {
        Some(digest) => digest,
        None => change_digest_at(session, conv_id, user_id, head)?,
    };
    result.insert("headDigest".into(), digest);
    Ok(Value::Object(result))
}

/// `_turn_get`.
pub fn turn_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let turn_id = required_text(payload, "turn_id", 128)?;
    let include_projection = match payload.get("include_projection") {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid Turn projection flag"),
    };
    let preview = get(payload, "content_preview_chars");
    if !preview.is_null() {
        let valid = matches!(preview, Value::Number(number) if number.as_i64().is_some_and(|chars| chars >= 1));
        if include_projection || !valid {
            return fail("database_protocol_error", "Invalid Turn content preview");
        }
        return turn_content_preview(session, &conv_id, user_id, &turn_id, preview.as_i64().unwrap_or(1));
    }
    let columns = if include_projection {
        "*"
    } else {
        "turn_id,presentation_id,conversation_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_revision,model_context_display_offset,settlement_json,created_at,updated_at"
    };
    let sql =
        format!("SELECT {columns} FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?");
    let Some(row) = session.fetch_one(&sql, &params![conv_id, user_id, turn_id])? else { return Ok(Value::Null) };
    if include_projection {
        turn_public(session, &row)
    } else {
        Ok(Value::Object(turn_public_metadata(&row)?))
    }
}

pub fn turn_resume_plan_read(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let turn_id = required_text(payload, "turn_id", 128)?;
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let revision = integer(payload, "projection_revision", None, Some(0), Some(i64::MAX))?;
    let Some(row) = session.fetch_one(
        "SELECT t.current_attempt_id,t.projection_revision,t.status AS turn_status,a.status AS attempt_status FROM storage_conversation_turns t LEFT JOIN storage_generation_attempts a ON a.attempt_id=t.current_attempt_id AND a.turn_id=t.turn_id AND a.conversation_id=t.conversation_id WHERE t.conversation_id=? AND t.user_id=? AND t.turn_id=?",
        &params![conv_id.clone(), user_id, turn_id.clone()],
    )? else {
        return Ok(json!({"status": "source_unavailable"}));
    };
    if row.text("current_attempt_id") != attempt_id || row.int("projection_revision")? != revision {
        return Ok(json!({"status": "stale_source"}));
    }
    let status = row.text("attempt_status");
    if !matches!(status.as_str(), "interrupted" | "truncated" | "failed") || row.text("turn_status") != status {
        return Ok(json!({"status": "source_unavailable"}));
    }
    Ok(json!({"status": "source_pinned", "sourceStatus": status, "conversationId": conv_id,
        "turnId": turn_id, "attemptId": attempt_id, "projectionRevision": revision}))
}

fn turn_content_preview(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    turn_id: &str,
    chars: i64,
) -> OpResult<Value> {
    let Some(row) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        &params![conv_id, user_id, turn_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let public = turn_public(session, &row)?;
    let content = public.get("projection").and_then(|projection| projection.get("content"));
    let preview = match content {
        Some(Value::String(content)) => Value::String(content.chars().take(chars as usize).collect()),
        _ => Value::Null,
    };
    let mut metadata = turn_public_metadata(&row)?;
    metadata.insert("contentPreview".into(), preview);
    Ok(Value::Object(metadata))
}

/// `_turn_timing_trace_get`.
pub fn turn_timing_trace_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let Some(row) = session.fetch_one(
        "SELECT a.attempt_id,a.status AS attempt_status,a.timing_trace_json,t.turn_id,t.conversation_id,t.user_id,t.current_attempt_id,t.projection_json,t.projection_revision,t.projection_checkpoint_revision,t.projection_materialized_revision,t.projection_patch_count,t.projection_patch_bytes FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id WHERE a.task_id=? AND t.user_id=? ORDER BY COALESCE(a.settled_at,a.started_at,a.created_at) DESC LIMIT 1",
        &params![task_id.clone(), user_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let mut timing_trace = load(row.get("timing_trace_json"))?;
    if !truthy(&timing_trace) {
        timing_trace = Value::Object(Obj::new());
    }
    if !matches!(&timing_trace, Value::Object(map) if !map.is_empty()) {
        let projection = projection_from_turn_row(session, &row)?;
        let candidate = projection.get("timingTrace").cloned().unwrap_or(Value::Null);
        timing_trace = match &candidate {
            Value::Object(trace) if text(get(trace, "taskId")) == task_id => candidate.clone(),
            _ => Value::Null,
        };
    }
    if !timing_trace.is_object() {
        return Ok(Value::Null);
    }
    Ok(json!({
        "attemptId": row.text("attempt_id"),
        "attemptStatus": row.text("attempt_status"),
        "turnId": row.text("turn_id"),
        "timingTrace": timing_trace,
    }))
}

/// `_turn_timing_trace_list`.
pub fn turn_timing_trace_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let limit = integer(payload, "limit", Some(30), Some(1), Some(100))?;
    let mut args = vec![SqlValue::Text(conv_id), SqlValue::Integer(user_id)];
    let mut before_clause = "";
    if !get(payload, "before_created_at").is_null() {
        let before = integer(payload, "before_created_at", None, Some(0), Some(I64_MAX))?;
        before_clause = "AND a.created_at<? ";
        args.push(SqlValue::Integer(before));
    }
    args.push(SqlValue::Integer(limit + 1));
    let sql = format!(
        "SELECT a.attempt_id,a.task_id,a.status,a.turn_id,a.created_at,a.settled_at FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id WHERE a.conversation_id=? AND t.user_id=? AND a.task_id<>'' {before_clause}ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT ?"
    );
    let rows = session.fetch_all(&sql, &args)?;
    let has_more = rows.len() as i64 > limit;
    let mut records = Vec::new();
    for row in rows.iter().take(limit as usize) {
        records.push(json!({
            "attempt_id": row.text("attempt_id"),
            "task_id": row.text("task_id"),
            "status": row.text("status"),
            "turn_id": row.text("turn_id"),
            "created_at": row.opt_int("created_at").unwrap_or(0),
            "settled_at": if row.get("settled_at").is_null() { Value::Null } else { Value::from(row.int("settled_at")?) },
        }));
    }
    Ok(json!({"records": records, "has_more": has_more}))
}

/// `legacy_turn_image_payload`: `(media_type, base64)`.
fn legacy_turn_image_payload(image: &Value) -> Option<(String, String)> {
    const MAX_ENCODED: usize = (8 * 1024 * 1024usize).div_ceil(3) * 4;
    const ALLOWED: &[&str] = &["image/png", "image/jpeg", "image/gif", "image/webp"];
    fn data_uri(value: &Value) -> Option<(String, String)> {
        let Value::String(value) = value else { return None };
        let rest = value.strip_prefix("data:")?;
        let marker = rest.find(";base64,")?;
        let media_type = super::normalize::py_strip(&rest[..marker]).to_lowercase();
        if !ALLOWED.contains(&media_type.as_str()) {
            return None;
        }
        let payload = &rest[marker + ";base64,".len()..];
        let length = payload.chars().count();
        if !(1..=MAX_ENCODED).contains(&length) || length % 4 != 0 {
            return None;
        }
        Some((media_type, payload.to_owned()))
    }
    let Value::Object(image) = image else { return None };
    if let Some(Value::String(encoded)) = image.get("base64") {
        if !encoded.is_empty() {
            let length = encoded.chars().count();
            if length > MAX_ENCODED || length % 4 != 0 {
                return None;
            }
            let declared =
                if truthy(get(image, "mediaType")) { get(image, "mediaType") } else { get(image, "mimeType") };
            let mut media_type = super::normalize::py_strip(&text(declared)).to_lowercase();
            if !ALLOWED.contains(&media_type.as_str()) {
                let (preview_type, _) = data_uri(get(image, "preview"))?;
                media_type = preview_type;
            }
            return Some((media_type, encoded.clone()));
        }
    }
    data_uri(get(image, "preview"))
}

/// `_turn_image_get`.
pub fn turn_image_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let turn_id = required_text(payload, "turn_id", 128)?;
    let expected = integer(payload, "projection_revision", None, Some(1), Some(I64_MAX))?;
    let index = integer(payload, "image_index", None, Some(0), Some(MAX_TURN_IMAGES - 1))?;
    let row = session.fetch_one(
        "SELECT turn_id,conversation_id,user_id,current_attempt_id,status,projection_json,projection_revision,projection_checkpoint_revision,projection_materialized_revision,projection_patch_count,projection_patch_bytes FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        &params![conv_id, user_id, turn_id],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    if !matches!(row.text("status").as_str(), "completed" | "interrupted" | "truncated" | "failed" | "superseded") {
        return Ok(Value::Null);
    }
    let current = row.opt_int("projection_revision").unwrap_or(0);
    if current != expected {
        return Ok(json!({"stale": true, "projectionRevision": current}));
    }
    let projection = projection_from_turn_row(session, &row)?;
    let Some(Value::Array(images)) = projection.get("images") else { return Ok(Value::Null) };
    let Some(image) = images.get(index as usize) else { return Ok(Value::Null) };
    let Some((media_type, base64)) = legacy_turn_image_payload(image) else { return Ok(Value::Null) };
    Ok(json!({"stale": false, "projectionRevision": current, "mediaType": media_type, "base64": base64}))
}

/// `_turn_projection_page`.
pub fn turn_projection_page_op(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let page = integer(payload, "page", None, Some(0), Some(I64_MAX))?;
    let mut expected = None;
    if !get(payload, "projection_revision").is_null() {
        expected = Some(integer(payload, "projection_revision", None, Some(1), Some(I64_MAX))?);
    }
    let mut identity = Payload::new();
    for key in ["conversation_id", "user_id", "turn_id"] {
        identity.insert(key.into(), get(payload, key).clone());
    }
    let turn = turn_get(session, &identity)?;
    let Value::Object(turn) = turn else { return Ok(Value::Null) };
    let revision = py_int(get(&turn, "projectionRevision"))?;
    if expected.is_some_and(|expected| expected != revision) {
        return Ok(stale_turn_projection_page(revision));
    }
    match turn_projection_page(&turn, page) {
        Some(result) => Ok(result),
        None => fail("database_protocol_error", "Turn projection page is outside its revision"),
    }
}

/// `_turn_list`.
pub fn turn_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let lane = get(payload, "lane_id");
    let mut sql = String::from("SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=?");
    let mut args = vec![SqlValue::Text(conv_id), SqlValue::Integer(user_id)];
    if truthy(lane) {
        sql.push_str(" AND lane_id=?");
        args.push(SqlValue::Text(python_str(lane)));
    }
    sql.push_str(" ORDER BY ordinal, turn_id");
    let rows = session.fetch_all(&sql, &args)?;
    Ok(Value::Array(rows.iter().map(|row| turn_public(session, row)).collect::<OpResult<_>>()?))
}

/// `_attempt_get`.
pub fn attempt_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let mut row = session.fetch_one(
        "SELECT a.* FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND t.user_id=?",
        &params![attempt_id, user_id],
    )?;
    if let Some(found) = &row {
        if truthy(get(payload, "resolve_executor")) {
            let config = load(found.get("config_json"))?;
            let root = match &config {
                Value::Object(config) => text(get(config, "flowRootAttemptId")),
                Value::Null => String::new(),
                _ if !truthy(&config) => String::new(),
                _ => return crate::error::internal("config has no get"),
            };
            if !root.is_empty() {
                row = session.fetch_one(
                    "SELECT a.* FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND t.user_id=? AND a.conversation_id=?",
                    &params![root, user_id, found.get("conversation_id")],
                )?;
            }
        }
    }
    let include_config = match payload.get("include_execution_config") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid attempt execution-config flag"),
    };
    let Some(row) = row else { return Ok(Value::Null) };
    let mut result = attempt_public(&row)?;
    if include_config {
        result["config"] = Value::Object(stored_object(row.get("config_json"), "attempt config")?);
    }
    Ok(result)
}

/// `_attempt_dispatchable_list`.
pub fn attempt_dispatchable_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let created_before = integer(payload, "created_before_ms", None, Some(0), None)?;
    let limit = integer(payload, "limit", Some(8), Some(1), Some(32))?;
    let rows = session.fetch_all(
        "SELECT a.*, t.user_id AS owner_user_id FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id WHERE a.status='pending' AND a.task_id='' AND a.queue_state='' AND a.dispatch_mode=? AND a.created_at<=? AND t.status='pending' AND t.current_attempt_id=a.attempt_id ORDER BY a.created_at, a.attempt_id LIMIT ?",
        &params![CONVERSATION_EXECUTOR_DISPATCH_MODE, created_before, limit],
    )?;
    let mut dispatchable = Vec::new();
    for row in &rows {
        let owner = row.int("owner_user_id")?;
        let turn = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=? AND current_attempt_id=? AND status='pending'",
            &params![row.get("conversation_id"), owner, row.get("turn_id"), row.get("attempt_id")],
        )?;
        let Some(turn) = turn else { continue };
        dispatchable.push(json!({
            "userId": owner,
            "turn": turn_public(session, &turn)?,
            "attempt": attempt_public(row)?,
            "config": Value::Object(stored_object(row.get("config_json"), "attempt config")?),
        }));
    }
    Ok(Value::Array(dispatchable))
}

/// `_turn_revision` (as an integer for internal callers).
pub fn conversation_revision(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<i64> {
    let row = session
        .fetch_one("SELECT rev FROM storage_conversations WHERE id=? AND user_id=?", &params![conv_id, user_id])?;
    match row {
        Some(row) => row.int("rev"),
        None => Ok(0),
    }
}

pub fn turn_revision(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    Ok(Value::from(conversation_revision(session, &conv_id, user_id)?))
}

/// `_prune_turn_tombstones`.
pub fn prune_turn_tombstones(session: &Session<'_>, now: i64) -> OpResult<()> {
    session
        .execute("DELETE FROM storage_turn_tombstones WHERE deleted_at < ?", &params![now - TOMBSTONE_RETENTION_MS])?;
    Ok(())
}

/// `_turn_list_delta`.
pub fn turn_list_delta(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let since = integer(payload, "since_ms", Some(0), Some(0), None)?;
    let watermark = now_ms();
    let lower = (since - DELTA_OVERLAP_MS).max(0);
    let mut known: Vec<(String, i64)> = Vec::new();
    if let Some(Value::Object(raw)) = payload.get("known_revisions") {
        for (turn_id, revision) in raw.iter().take(2000) {
            let parsed = match revision {
                Value::Bool(flag) => Some(*flag as i64),
                Value::Number(number) => number.as_i64().or_else(|| number.as_f64().map(|float| float.trunc() as i64)),
                Value::String(text) => text.trim().parse::<i64>().ok(),
                _ => None,
            };
            if let Some(parsed) = parsed {
                known.retain(|(id, _)| id != turn_id);
                known.push((turn_id.clone(), parsed.max(0)));
            }
        }
    }
    let rows = if !known.is_empty() {
        let revision_rows = session.fetch_all(
            "SELECT turn_id,projection_revision FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND updated_at>=?",
            &params![conv_id.clone(), user_id, lower],
        )?;
        let changed: Vec<String> = revision_rows
            .iter()
            .filter(|row| {
                let id = row.text("turn_id");
                let known_revision = known.iter().find(|(known_id, _)| *known_id == id).map(|(_, r)| *r).unwrap_or(-1);
                row.opt_int("projection_revision").unwrap_or(0) > known_revision
            })
            .map(|row| row.text("turn_id"))
            .collect();
        let mut rows = Vec::new();
        for chunk in changed.chunks(400) {
            let marks = vec!["?"; chunk.len()].join(",");
            let sql = format!(
                "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id IN ({marks})"
            );
            let mut args = vec![SqlValue::Text(conv_id.clone()), SqlValue::Integer(user_id)];
            args.extend(chunk.iter().map(|id| SqlValue::Text(id.clone())));
            rows.extend(session.fetch_all(&sql, &args)?);
        }
        rows.sort_by_key(|row| row.opt_int("ordinal").unwrap_or(0));
        rows
    } else {
        session.fetch_all(
            "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND updated_at>=? ORDER BY ordinal",
            &params![conv_id.clone(), user_id, lower],
        )?
    };
    let tombstones = session.fetch_all(
        "SELECT turn_id FROM storage_turn_tombstones WHERE conversation_id=? AND user_id=? AND deleted_at>=?",
        &params![conv_id, user_id, lower],
    )?;
    Ok(json!({
        "turns": rows.iter().map(|row| turn_public(session, row)).collect::<OpResult<Vec<_>>>()?,
        "deletedTurnIds": tombstones.iter().map(|row| Value::String(row.text("turn_id"))).collect::<Vec<_>>(),
        "serverNowMs": watermark,
    }))
}

/// `_turn_events`.
pub fn turn_events(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let after = integer(payload, "after", Some(0), Some(0), None)?;
    let limit = integer(payload, "limit", Some(1000), Some(1), Some(5000))?;
    let owner = session.fetch_one(
        "SELECT 1 AS present FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND t.user_id=?",
        &params![attempt_id.clone(), user_id],
    )?;
    if owner.is_none() {
        return Ok(Value::Null);
    }
    let rows = session.fetch_all(
        "SELECT payload_json FROM storage_attempt_events WHERE attempt_id=? AND sequence>? ORDER BY sequence LIMIT ?",
        &params![attempt_id.clone(), after, limit],
    )?;
    let mut events = Vec::with_capacity(rows.len());
    for row in &rows {
        let event = load(row.get("payload_json"))?;
        events.push(if truthy(&event) { event } else { Value::Object(Obj::new()) });
    }
    if text(get(payload, "projection_mode")) != "patch" {
        hydrate_slim_frame_tail(session, &attempt_id, &mut events)?;
    }
    Ok(Value::Array(events))
}

fn hydrate_slim_frame_tail(session: &Session<'_>, attempt_id: &str, events: &mut [Value]) -> OpResult<()> {
    let index = events.iter().rposition(|envelope| {
        matches!(envelope.get("payload"), Some(Value::Object(body)) if !body.contains_key("projection"))
            && matches!(
                envelope.get("type").and_then(Value::as_str),
                Some("projection_updated" | "interaction_request" | "terminal_settlement")
            )
    });
    let Some(index) = index else { return Ok(()) };
    let turn_id = text(events[index].get("turnId").unwrap_or(&Value::Null));
    if turn_id.is_empty() {
        return Ok(());
    }
    let turn = session.fetch_one(
        "SELECT turn_id,conversation_id,user_id,current_attempt_id,projection_json,projection_revision,projection_checkpoint_revision,projection_materialized_revision,projection_patch_count,projection_patch_bytes FROM storage_conversation_turns WHERE turn_id=?",
        &params![turn_id],
    )?;
    let Some(turn) = turn else { return Ok(()) };
    if turn.get("current_attempt_id").as_str() != Some(attempt_id) {
        return Ok(());
    }
    let projection = projection_from_turn_row(session, &turn)?;
    events[index]["payload"]["projection"] = projection;
    Ok(())
}

/// `_turn_events_prune`.
pub fn turn_events_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let settled_before = integer(payload, "settled_before_ms", None, Some(1), None)?;
    let max_attempts = integer(payload, "max_attempts", Some(16), Some(1), Some(256))?;
    let max_rows = integer(payload, "max_rows", Some(4096), Some(1), Some(200_000))?;
    let mut candidates = session.fetch_all(
        "SELECT a.attempt_id FROM storage_generation_attempts AS a WHERE a.status NOT IN ('pending','running') AND a.settled_at IS NOT NULL AND a.settled_at < ? AND NOT EXISTS (SELECT 1 FROM storage_conversation_turns AS t WHERE t.current_attempt_id=a.attempt_id AND (t.projection_checkpoint_revision IS NOT NULL OR t.projection_materialized_revision IS NOT NULL)) AND NOT EXISTS (SELECT 1 FROM storage_conversation_changes AS c WHERE c.attempt_id=a.attempt_id AND c.attempt_sequence IS NOT NULL) AND EXISTS (SELECT 1 FROM storage_attempt_events AS e             WHERE e.attempt_id=a.attempt_id) ORDER BY a.settled_at, a.attempt_id LIMIT ?",
        &params![settled_before, max_attempts + 1],
    )?;
    let overflow = candidates.len() as i64 > max_attempts;
    candidates.truncate(max_attempts as usize);
    let mut deleted_rows = 0i64;
    let mut deleted_attempts = 0i64;
    for candidate in &candidates {
        if deleted_rows >= max_rows {
            break;
        }
        let attempt_id = candidate.get("attempt_id").clone();
        let max_seq = session.fetch_one(
            "SELECT COALESCE(MAX(sequence),0) AS n FROM storage_attempt_events WHERE attempt_id=?",
            &params![attempt_id.clone()],
        )?;
        let remaining_seq = match max_seq {
            Some(row) => row.int("n")?,
            None => 0,
        };
        let mut cursor = 0i64;
        while cursor < remaining_seq && deleted_rows < max_rows {
            let window = (max_rows - deleted_rows).min(1024);
            let upper = cursor + window;
            deleted_rows += session.execute(
                "DELETE FROM storage_attempt_events WHERE attempt_id=? AND sequence>? AND sequence<=?",
                &params![attempt_id.clone(), cursor, upper],
            )? as i64;
            cursor = upper;
        }
        if cursor < remaining_seq {
            break;
        }
        deleted_attempts += 1;
    }
    let remaining = overflow as i64 + (candidates.len() as i64 - deleted_attempts);
    Ok(json!({"deleted_rows": deleted_rows, "deleted_attempts": deleted_attempts, "remaining": remaining}))
}

fn snapshot_active_attempt_phase(
    session: &Session<'_>,
    turns: &[Row],
    attempts: &[Row],
    conversation_id: &str,
    user_id: i64,
) -> OpResult<Option<Value>> {
    let running: Vec<String> =
        attempts.iter().filter(|row| row.text("status") == "running").map(|row| row.text("attempt_id")).collect();
    let turn = turns.iter().rev().find(|row| {
        row.get("lane_id").as_str() == Some("main")
            && row.get("status").as_str() == Some("running")
            && running.contains(&row.text("current_attempt_id"))
    });
    let Some(turn) = turn else { return Ok(None) };
    let event_row = session.fetch_one(
        "SELECT CASE WHEN e.payload_bytes<=32768 THEN e.payload_json ELSE NULL END AS payload_json FROM storage_attempt_events e JOIN storage_conversation_turns t ON t.turn_id=e.turn_id AND t.conversation_id=e.conversation_id AND t.current_attempt_id=e.attempt_id WHERE e.attempt_id=? AND e.conversation_id=? AND t.user_id=? ORDER BY e.sequence DESC LIMIT 1",
        &params![turn.get("current_attempt_id"), conversation_id, user_id],
    )?;
    let event = match event_row {
        Some(row) => load(row.get("payload_json"))?,
        None => Value::Null,
    };
    let Value::Object(event) = event else { return Ok(None) };
    let phase = match event.get("payload") {
        Some(Value::Object(body)) => body.get("phase").cloned(),
        _ => None,
    };
    let revision = event.get("projectionRevision");
    let Some(phase @ Value::Object(_)) = phase else { return Ok(None) };
    if !truthy(phase.get("phase").unwrap_or(&Value::Null)) {
        return Ok(None);
    }
    let revision = match revision {
        Some(Value::Number(number)) => number.as_i64(),
        _ => None,
    };
    let Some(revision) =
        revision.filter(|revision| (0..=turn.int("projection_revision").unwrap_or(0)).contains(revision))
    else {
        return Ok(None);
    };
    Ok(Some(json!({
        "turnId": turn.text("turn_id"),
        "attemptId": turn.text("current_attempt_id"),
        "projectionRevision": revision,
        "phase": phase,
    })))
}

fn projection_byte_budget(payload: &Payload) -> OpResult<i64> {
    let budget = integer(payload, "max_projection_bytes", Some(0), Some(0), Some(16_777_216))?;
    if budget != 0 && budget < 65536 {
        return fail("database_protocol_error", "Invalid projection byte budget");
    }
    Ok(budget)
}

fn sync_tail_rows(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    lane_id: &str,
    before_ordinal: i64,
    limit: i64,
    max_projection_bytes: i64,
) -> OpResult<(Vec<Row>, bool)> {
    let where_clause =
        "WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal<? ORDER BY ordinal DESC LIMIT ?";
    let args = params![conv_id, user_id, lane_id, before_ordinal, limit + 1];
    if max_projection_bytes == 0 {
        let mut rows = session.fetch_all(&format!("SELECT * FROM storage_conversation_turns {where_clause}"), &args)?;
        let has_more = rows.len() as i64 > limit;
        rows.truncate(limit as usize);
        rows.reverse();
        return Ok((rows, has_more));
    }
    let sizes = session.fetch_all(
        &format!("SELECT turn_id,ordinal,current_attempt_id,projection_checkpoint_revision,LENGTH(CAST(projection_json AS BLOB)) AS inline_bytes,projection_patch_bytes FROM storage_conversation_turns {where_clause}"),
        &args,
    )?;
    let window: Vec<&Row> = sizes.iter().take(limit as usize).collect();
    let checkpointed: Vec<&&Row> =
        window.iter().filter(|row| !row.get("projection_checkpoint_revision").is_null()).collect();
    let mut checkpoint_bytes: Vec<((SqlValue, SqlValue, SqlValue), i64)> = Vec::new();
    if !checkpointed.is_empty() {
        let marks = vec!["(?,?,?)"; checkpointed.len()].join(",");
        let mut keys = vec![SqlValue::Text(conv_id.into()), SqlValue::Integer(user_id)];
        for row in &checkpointed {
            keys.push(row.get("turn_id").clone());
            keys.push(row.get("current_attempt_id").clone());
            keys.push(row.get("projection_checkpoint_revision").clone());
        }
        let sql = format!(
            "SELECT turn_id,attempt_id,projection_revision,projection_bytes FROM storage_turn_projection_checkpoints WHERE conversation_id=? AND user_id=? AND (turn_id,attempt_id,projection_revision) IN ({marks})"
        );
        for row in session.fetch_all(&sql, &keys)? {
            checkpoint_bytes.push((
                (row.get("turn_id").clone(), row.get("attempt_id").clone(), row.get("projection_revision").clone()),
                row.int("projection_bytes")?,
            ));
        }
    }
    let mut selected: Vec<SqlValue> = Vec::new();
    let mut used = 0i64;
    for row in &window {
        let mut size = row.opt_int("inline_bytes").unwrap_or(0) + row.opt_int("projection_patch_bytes").unwrap_or(0);
        if !row.get("projection_checkpoint_revision").is_null() {
            let key = (
                row.get("turn_id").clone(),
                row.get("current_attempt_id").clone(),
                row.get("projection_checkpoint_revision").clone(),
            );
            match checkpoint_bytes.iter().find(|(candidate, _)| *candidate == key) {
                Some((_, bytes)) => size += bytes,
                None => return fail("database_integrity", "Turn projection checkpoint is missing"),
            }
        }
        if !selected.is_empty() && used + size > max_projection_bytes {
            break;
        }
        selected.push(row.get("turn_id").clone());
        used += size;
    }
    if selected.is_empty() {
        return Ok((vec![], false));
    }
    let marks = vec!["?"; selected.len()].join(",");
    let sql = format!(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? AND turn_id IN ({marks}) ORDER BY ordinal"
    );
    let mut args = vec![SqlValue::Text(conv_id.into()), SqlValue::Integer(user_id), SqlValue::Text(lane_id.into())];
    let count = selected.len();
    args.extend(selected);
    let rows = session.fetch_all(&sql, &args)?;
    Ok((rows, sizes.len() > count))
}

fn current_attempts(session: &Session<'_>, conv_id: &str, user_id: i64, turns: &[Row]) -> OpResult<Vec<Row>> {
    if turns.is_empty() {
        return Ok(vec![]);
    }
    let marks = vec!["?"; turns.len()].join(",");
    let sql = format!(
        "SELECT a.* FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.current_attempt_id=a.attempt_id WHERE t.conversation_id=? AND t.user_id=? AND t.turn_id IN ({marks}) ORDER BY a.created_at,a.attempt_id"
    );
    let mut args = vec![SqlValue::Text(conv_id.into()), SqlValue::Integer(user_id)];
    args.extend(turns.iter().map(|row| SqlValue::Text(row.text("turn_id"))));
    session.fetch_all(&sql, &args)
}

fn queue_snapshot_item(row: &Row) -> OpResult<Value> {
    let loaded = load(row.get("payload_json"))?;
    let queue_payload = if truthy(&loaded) { loaded } else { Value::Object(Obj::new()) };
    let Value::Object(queue_payload) = queue_payload else {
        return crate::error::internal("queue payload has no get");
    };
    let user_message_value = get(&queue_payload, "_user_msg");
    let user_message = if truthy(user_message_value) { user_message_value.clone() } else { Value::Object(Obj::new()) };
    let Value::Object(user_message) = user_message else { return crate::error::internal("_user_msg has no get") };
    let text_value = [get(&user_message, "content"), get(&queue_payload, "_peerText"), get(&queue_payload, "text")]
        .into_iter()
        .find(|value| truthy(value))
        .cloned()
        .unwrap_or(Value::String(String::new()));
    let timestamp_value = [get(&user_message, "timestamp"), get(&queue_payload, "timestamp")]
        .into_iter()
        .find(|value| truthy(value))
        .cloned();
    let timestamp = match timestamp_value {
        Some(value) => py_int(&value)?,
        None => row.int("created_at_ms")?,
    };
    let mut item = Obj::new();
    item.insert("queueId".into(), Value::String(row.text("id")));
    item.insert("position".into(), Value::from(row.int("position")?));
    let kind = row.text("kind");
    item.insert("kind".into(), Value::String(if kind.is_empty() { "real".into() } else { kind }));
    item.insert("priority".into(), Value::from(row.int("priority")?));
    item.insert("timestamp".into(), Value::from(timestamp));
    item.insert("text".into(), Value::String(python_str(&text_value).chars().take(2000).collect()));
    for (key, field) in [
        ("hasImages", "images"),
        ("hasPdfs", "pdfTexts"),
        ("hasAttachments", "attachments"),
        ("hasRefs", "convRefs"),
        ("hasQuotes", "replyQuotes"),
    ] {
        item.insert(key.into(), Value::Bool(truthy(get(&queue_payload, field))));
    }
    let source = [get(&user_message, "_msgId"), get(&queue_payload, "_msgId")].into_iter().find(|value| truthy(value));
    if let Some(source) = source {
        item.insert("sourceMessageId".into(), Value::String(python_str(source)));
    }
    for (public, stored) in
        [("inputTurnId", "input_turn_id"), ("outputTurnId", "output_turn_id"), ("attemptId", "attempt_id")]
    {
        if row.get(stored).truthy() {
            item.insert(public.into(), Value::String(row.get(stored).text_or_empty()));
        }
    }
    if truthy(get(&queue_payload, "_peerMessage")) {
        item.insert("isPeerMessage".into(), Value::Bool(true));
        item.insert("fromConv".into(), Value::String(text(get(&queue_payload, "_fromConv"))));
        item.insert("isPeerHuman".into(), Value::Bool(truthy(get(&queue_payload, "_peerHuman"))));
    }
    if let Some(completion) = background_command_completion(&queue_payload) {
        item.insert("backgroundCommand".into(), completion);
    }
    if truthy(get(&queue_payload, "_steerFallback")) {
        item.insert("steerFallback".into(), Value::Bool(true));
    }
    Ok(Value::Object(item))
}

/// `_turn_sync_snapshot`.
pub fn turn_sync_snapshot(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let max_projection_bytes = projection_byte_budget(payload)?;
    let (conv_id, user_id) = turn_identity(payload)?;
    let turn_limit = integer(payload, "turn_limit", Some(0), Some(0), Some(256))?;
    let include_artifact_hint = match payload.get("include_artifact_hint") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid artifact hint selector"),
    };
    let Some(conversation) = session.fetch_one(
        "SELECT rev,settings_json FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let sync_sequence = conversation_sync_head(session, &conv_id, user_id)?;
    let mut turn_window: Option<Value> = None;
    let all_turns = |session: &Session<'_>| {
        session.fetch_all(
            "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? ORDER BY ordinal",
            &params![conv_id.clone(), user_id],
        )
    };
    let turns = if turn_limit != 0 {
        let lane_counts = session.fetch_all(
            "SELECT lane_id,COUNT(*) AS total FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? GROUP BY lane_id",
            &params![conv_id.clone(), user_id],
        )?;
        let linear = lane_counts.is_empty() || (lane_counts.len() == 1 && lane_counts[0].text("lane_id") == "main");
        if linear {
            let (turns, has_more) =
                sync_tail_rows(session, &conv_id, user_id, "main", I64_MAX, turn_limit, max_projection_bytes)?;
            let total = lane_counts.first().and_then(|row| row.opt_int("total")).unwrap_or(0);
            turn_window = Some(json!({
                "laneId": "main",
                "nextBeforeOrdinal": if has_more && !turns.is_empty() { Value::from(turns[0].int("ordinal")?) } else { Value::Null },
                "hasMore": has_more,
                "totalTurns": total,
            }));
            turns
        } else {
            all_turns(session)?
        }
    } else {
        all_turns(session)?
    };
    let attempts = if turn_window.is_some() {
        current_attempts(session, &conv_id, user_id, &turns)?
    } else {
        session.fetch_all(
            "SELECT a.* FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id WHERE a.conversation_id=? AND t.user_id=? ORDER BY a.created_at,a.attempt_id",
            &params![conv_id.clone(), user_id],
        )?
    };
    let queue_rows = session.fetch_all(
        "SELECT id,payload_json,position,kind,priority,created_at_ms,input_turn_id,output_turn_id,attempt_id FROM storage_queue_items WHERE conv_id=? AND user_id=? ORDER BY priority,position,id",
        &params![conv_id.clone(), user_id],
    )?;
    let queue_items = queue_rows.iter().map(queue_snapshot_item).collect::<OpResult<Vec<_>>>()?;
    let settings = load(conversation.get("settings_json"))?;
    let settings = if truthy(&settings) { settings } else { Value::Object(Obj::new()) };
    let Value::Object(settings) = settings else {
        return fail("database_integrity", "Conversation settings are malformed");
    };
    let public_settings: Obj =
        settings.into_iter().filter(|(key, _)| !SYNC_PRIVATE_SETTING_KEYS.contains(&key.as_str())).collect();
    let mut result = Obj::new();
    result.insert("conversationId".into(), Value::String(conv_id.clone()));
    result.insert("conversationRevision".into(), Value::from(conversation.opt_int("rev").unwrap_or(0)));
    result.insert("syncSequence".into(), Value::from(sync_sequence));
    result.insert("settings".into(), Value::Object(public_settings));
    result.insert(
        "turns".into(),
        Value::Array(turns.iter().map(|row| turn_public(session, row)).collect::<OpResult<_>>()?),
    );
    result.insert("attempts".into(), Value::Array(attempts.iter().map(attempt_public).collect::<OpResult<_>>()?));
    result.insert("queueItems".into(), Value::Array(queue_items));
    let mut result = match with_sync_lineage(result, session, &conv_id, user_id, sync_sequence, None)? {
        Value::Object(map) => map,
        _ => unreachable!(),
    };
    if let Some(phase) = snapshot_active_attempt_phase(session, &turns, &attempts, &conv_id, user_id)? {
        result.insert("activeAttemptPhase".into(), phase);
    }
    if let Some(window) = turn_window {
        result.insert("turnWindow".into(), window);
    }
    if include_artifact_hint {
        let present = session
            .fetch_one(
                "SELECT 1 AS present FROM chat_artifacts WHERE conv_id=? AND deleted_at=0 LIMIT 1",
                &params![conv_id],
            )?
            .is_some();
        result.insert("hasArtifacts".into(), Value::Bool(present));
    }
    Ok(Value::Object(result))
}

/// `_turn_sync_round_directory`.
pub fn turn_sync_round_directory(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let lane_id = required_text(payload, "lane_id", 128)?;
    let expected = integer(payload, "sync_sequence", None, Some(0), None)?;
    let offset = integer(payload, "offset", Some(0), Some(0), None)?;
    let limit = integer(payload, "limit", Some(50), Some(1), Some(100))?;
    let anchor = text(get(payload, "anchor_turn_id"));
    if session
        .fetch_one(
            "SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?",
            &params![conv_id.clone(), user_id],
        )?
        .is_none()
    {
        return Ok(Value::Null);
    }
    let head = conversation_sync_head(session, &conv_id, user_id)?;
    let stale = |sequence: i64| {
        json!({"ownerId": user_id, "conversationId": conv_id, "laneId": lane_id, "syncSeq": sequence,
               "stale": true, "totalRounds": 0, "offset": offset, "entries": []})
    };
    if head != expected {
        return Ok(stale(head));
    }
    let where_clause = " FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? AND actor IN ('human','critic','virtual_user')";
    let total = session
        .fetch_one(&format!("SELECT COUNT(*) AS n{where_clause}"), &params![conv_id.clone(), user_id, lane_id.clone()])?
        .map(|row| row.int("n"))
        .transpose()?
        .unwrap_or(0);
    let rows = session.fetch_all(
        &format!("SELECT turn_id,ordinal,actor{where_clause} ORDER BY ordinal LIMIT ? OFFSET ?"),
        &params![conv_id.clone(), user_id, lane_id.clone(), limit, offset],
    )?;
    let mut entries = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        entries.push(json!({
            "turnId": col(row, "turn_id")?,
            "ordinal": row.int("ordinal")?,
            "actor": col(row, "actor")?,
            "roundNumber": offset + index as i64 + 1,
        }));
    }
    let mut result = Obj::new();
    result.insert("ownerId".into(), Value::from(user_id));
    result.insert("conversationId".into(), Value::String(conv_id.clone()));
    result.insert("laneId".into(), Value::String(lane_id.clone()));
    result.insert("syncSeq".into(), Value::from(head));
    result.insert("stale".into(), Value::Bool(false));
    result.insert("totalRounds".into(), Value::from(total));
    result.insert("offset".into(), Value::from(offset));
    result.insert("entries".into(), Value::Array(entries));
    if offset + (rows.len() as i64) < total {
        result.insert("nextOffset".into(), Value::from(offset + rows.len() as i64));
    }
    if !anchor.is_empty() {
        let row = session.fetch_one(
            "SELECT ordinal FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? AND turn_id=?",
            &params![conv_id.clone(), user_id, lane_id.clone(), anchor],
        )?;
        if let Some(row) = row {
            let rank = session
                .fetch_one(
                    &format!("SELECT COUNT(*) AS n{where_clause} AND ordinal<=?"),
                    &params![conv_id.clone(), user_id, lane_id.clone(), row.int("ordinal")?],
                )?
                .map(|row| row.int("n"))
                .transpose()?
                .unwrap_or(0);
            if rank > 0 {
                result.insert("activeRound".into(), Value::from(rank));
            }
        }
    }
    let final_head = conversation_sync_head(session, &conv_id, user_id)?;
    if final_head != head {
        return Ok(stale(final_head));
    }
    Ok(Value::Object(result))
}

/// `_turn_sync_page`.
pub fn turn_sync_page(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let lane_id = required_text(payload, "lane_id", 128)?;
    let before_ordinal = integer(payload, "before_ordinal", Some(I64_MAX), Some(0), Some(I64_MAX))?;
    let limit = integer(payload, "limit", Some(64), Some(1), Some(256))?;
    let max_projection_bytes = projection_byte_budget(payload)?;
    let expected = integer(payload, "sync_sequence", None, Some(0), None)?;
    let anchor_sequence = integer(payload, "anchor_sequence", Some(0), Some(0), None)?;
    let Some(conversation) = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let sync_sequence = conversation_sync_head(session, &conv_id, user_id)?;
    if sync_sequence != expected {
        return Ok(json!({"stale": true, "syncSequence": sync_sequence}));
    }
    let total = session
        .fetch_one(
            "SELECT COUNT(*) AS total FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=?",
            &params![conv_id.clone(), user_id, lane_id.clone()],
        )?
        .map(|row| row.opt_int("total").unwrap_or(0))
        .unwrap_or(0);
    let (page_rows, has_more) =
        sync_tail_rows(session, &conv_id, user_id, &lane_id, before_ordinal, limit, max_projection_bytes)?;
    let attempts = current_attempts(session, &conv_id, user_id, &page_rows)?;
    let next_before = match page_rows.first() {
        Some(row) => Value::from(row.int("ordinal")?),
        None => Value::Null,
    };
    let mut page = Obj::new();
    page.insert("conversationId".into(), Value::String(conv_id.clone()));
    page.insert("conversationRevision".into(), Value::from(conversation.opt_int("rev").unwrap_or(0)));
    page.insert("syncSequence".into(), Value::from(sync_sequence));
    page.insert("laneId".into(), Value::String(lane_id));
    page.insert("beforeOrdinal".into(), Value::from(before_ordinal));
    page.insert("nextBeforeOrdinal".into(), next_before);
    page.insert("hasMore".into(), Value::Bool(has_more));
    page.insert("totalTurns".into(), Value::from(total));
    page.insert(
        "turns".into(),
        Value::Array(page_rows.iter().map(|row| turn_public(session, row)).collect::<OpResult<_>>()?),
    );
    page.insert("attempts".into(), Value::Array(attempts.iter().map(attempt_public).collect::<OpResult<_>>()?));
    let Value::Object(mut page) = with_sync_lineage(page, session, &conv_id, user_id, sync_sequence, None)? else {
        unreachable!()
    };
    if anchor_sequence != 0 {
        let digest = if anchor_sequence == sync_sequence {
            page["headDigest"].clone()
        } else {
            change_digest_at(session, &conv_id, user_id, anchor_sequence)?
        };
        page.insert("anchorDigest".into(), digest);
    }
    Ok(Value::Object(page))
}

fn byte_bounded_change_page(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    after: i64,
    limit: i64,
    max_bytes: i64,
) -> OpResult<(Vec<Row>, Vec<Value>)> {
    let sizes = session.fetch_all(
        &format!("SELECT c.sync_sequence,LENGTH(CAST(event_json AS BLOB))+COALESCE(LENGTH(CAST(payload_json AS BLOB)),0) AS stored_bytes {CHANGE_ROWS_FROM}AND c.sync_sequence>? ORDER BY c.sync_sequence LIMIT ?"),
        &params![conv_id, user_id, after, limit],
    )?;
    let mut rows = Vec::new();
    let mut events = Vec::new();
    let mut used = 0i64;
    let mut start = 0usize;
    while start < sizes.len() {
        let mut end = start;
        let mut candidate = 0i64;
        while end < sizes.len()
            && (end == start || used + candidate + sizes[end].opt_int("stored_bytes").unwrap_or(0) <= max_bytes)
        {
            candidate += sizes[end].opt_int("stored_bytes").unwrap_or(0);
            end += 1;
        }
        let batch = session.fetch_all(
            &format!("{CHANGE_ROW_COLUMNS}{CHANGE_ROWS_FROM}AND c.sync_sequence>=? AND c.sync_sequence<=? ORDER BY c.sync_sequence"),
            &params![conv_id, user_id, sizes[start].int("sync_sequence")?, sizes[end - 1].int("sync_sequence")?],
        )?;
        for row in batch {
            let event = conversation_change_from_row(&row)?;
            let size = crate::json::dumps(&event).len() as i64;
            if !events.is_empty() && used + size > max_bytes {
                return Ok((rows, events));
            }
            rows.push(row);
            events.push(event);
            used += size;
        }
        start = end;
    }
    Ok((rows, events))
}

/// `_turn_sync_changes`.
pub fn turn_sync_changes(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let after = integer(payload, "after", Some(0), Some(0), None)?;
    let limit = integer(payload, "limit", Some(500), Some(1), Some(2000))?;
    let max_bytes = if payload.contains_key("max_bytes") {
        Some(integer(payload, "max_bytes", None, Some(CHANGE_PAGE_MIN_BYTES), Some(CHANGE_PAGE_MAX_BYTES))?)
    } else {
        None
    };
    if session
        .fetch_one(
            "SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?",
            &params![conv_id.clone(), user_id],
        )?
        .is_none()
    {
        return Ok(Value::Null);
    }
    let head = conversation_sync_head(session, &conv_id, user_id)?;
    let reset = |reason: &str| -> OpResult<Value> {
        let mut result = Obj::new();
        result.insert("head".into(), Value::from(head));
        result.insert("events".into(), Value::Array(vec![]));
        result.insert("resetRequired".into(), Value::Bool(true));
        result.insert("resetReason".into(), Value::String(reason.into()));
        with_sync_lineage(result, session, &conv_id, user_id, head, None)
    };
    if after > head {
        return reset("cursor_invalid");
    }
    let (rows, events) = match max_bytes {
        None => {
            let rows = session.fetch_all(
                &format!(
                    "{CHANGE_ROW_COLUMNS}{CHANGE_ROWS_FROM}AND c.sync_sequence>? ORDER BY c.sync_sequence LIMIT ?"
                ),
                &params![conv_id.clone(), user_id, after, limit],
            )?;
            (rows, None)
        }
        Some(max_bytes) => {
            let (rows, events) = byte_bounded_change_page(session, &conv_id, user_id, after, limit, max_bytes)?;
            (rows, Some(events))
        }
    };
    if head > after
        && (rows.is_empty()
            || rows
                .iter()
                .enumerate()
                .any(|(index, row)| row.opt_int("sync_sequence").unwrap_or(0) != after + index as i64 + 1))
    {
        return reset("cursor_expired");
    }
    let events = match events {
        Some(events) => events,
        None => rows.iter().map(conversation_change_from_row).collect::<OpResult<Vec<_>>>()?,
    };
    let digests: Vec<String> = rows.iter().map(change_row_digest).collect();
    let anchor = change_digest_at(session, &conv_id, user_id, after)?;
    let last_sequence = rows.last().map(|row| row.opt_int("sync_sequence").unwrap_or(0));
    let head_digest = if after == head {
        Some(anchor.clone())
    } else if last_sequence == Some(head) {
        Some(Value::String(digests.last().cloned().unwrap_or_default()))
    } else {
        None
    };
    let mut page = Obj::new();
    page.insert("head".into(), Value::from(head));
    page.insert("events".into(), Value::Array(events));
    page.insert("resetRequired".into(), Value::Bool(false));
    page.insert("hasMore".into(), Value::Bool(last_sequence.is_some_and(|sequence| sequence < head)));
    page.insert("changeDigests".into(), Value::Array(digests.into_iter().map(Value::String).collect()));
    let Value::Object(mut page) = with_sync_lineage(page, session, &conv_id, user_id, head, head_digest)? else {
        unreachable!()
    };
    if after != 0 {
        page.insert("anchorDigest".into(), anchor);
    }
    if max_bytes.is_some() {
        page.insert("maxBytesApplied".into(), Value::Bool(true));
    }
    Ok(Value::Object(page))
}

/// `_turn_sync_prune`.
pub fn turn_sync_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let created_before = integer(payload, "created_before_ms", None, Some(1), None)?;
    let max_rows = integer(payload, "max_rows", Some(512), Some(1), Some(20_000))?;
    let mut rows = session.fetch_all(
        "SELECT conversation_id,user_id,sync_sequence FROM storage_conversation_changes WHERE created_at<? ORDER BY created_at,conversation_id,user_id,sync_sequence LIMIT ?",
        &params![created_before, max_rows + 1],
    )?;
    let overflow = rows.len() as i64 > max_rows;
    rows.truncate(max_rows as usize);
    let mut deleted = 0usize;
    for chunk in rows.chunks(CONVERSATION_CHANGE_DELETE_KEY_CHUNK) {
        let marks = vec!["(?,?,?)"; chunk.len()].join(",");
        let mut keys = Vec::new();
        for row in chunk {
            keys.push(row.get("conversation_id").clone());
            keys.push(SqlValue::Integer(row.int("user_id")?));
            keys.push(SqlValue::Integer(row.int("sync_sequence")?));
        }
        deleted += session.execute(
            &format!(
                "DELETE FROM storage_conversation_changes WHERE (conversation_id,user_id,sync_sequence) IN ({marks})"
            ),
            &keys,
        )?;
    }
    Ok(json!({"deletedRows": deleted, "remaining": overflow}))
}

#[cfg(test)]
mod resume_plan_tests {
    use super::*;

    #[test]
    fn resume_plan_read_pins_raw_status_owner_revision_and_attempt_identity() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE storage_conversation_turns(conversation_id TEXT,user_id INTEGER,turn_id TEXT,current_attempt_id TEXT,projection_revision INTEGER,status TEXT); CREATE TABLE storage_generation_attempts(attempt_id TEXT,turn_id TEXT,conversation_id TEXT,status TEXT); INSERT INTO storage_conversation_turns VALUES('c',1,'t','a',3,'interrupted'); INSERT INTO storage_generation_attempts VALUES('a','t','c','interrupted');").unwrap();
        let session = Session::new(&connection, "test", None, false);
        let mut payload =
            json!({"conversation_id":"c","user_id":1,"turn_id":"t","attempt_id":"a","projection_revision":3})
                .as_object()
                .unwrap()
                .clone();
        assert_eq!(
            turn_resume_plan_read(&session, &payload).unwrap(),
            json!({"status":"source_pinned","sourceStatus":"interrupted","conversationId":"c","turnId":"t","attemptId":"a","projectionRevision":3})
        );
        payload.insert("projection_revision".into(), json!(2));
        assert_eq!(turn_resume_plan_read(&session, &payload).unwrap(), json!({"status":"stale_source"}));
        payload.insert("projection_revision".into(), json!(3));
        payload.insert("user_id".into(), json!(2));
        assert_eq!(turn_resume_plan_read(&session, &payload).unwrap(), json!({"status":"source_unavailable"}));
        payload.insert("user_id".into(), json!(1));
        for sql in [
            "UPDATE storage_generation_attempts SET status='running'",
            "UPDATE storage_generation_attempts SET status='failed'",
            "UPDATE storage_generation_attempts SET status='interrupted',turn_id='other'",
            "DELETE FROM storage_generation_attempts",
        ] {
            connection.execute_batch(sql).unwrap();
            assert_eq!(turn_resume_plan_read(&session, &payload).unwrap(), json!({"status":"source_unavailable"}));
        }
        payload.insert("attempt_id".into(), json!("different"));
        assert_eq!(turn_resume_plan_read(&session, &payload).unwrap(), json!({"status":"stale_source"}));
    }
}
