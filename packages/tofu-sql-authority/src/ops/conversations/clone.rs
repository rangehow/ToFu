//! `operations_pkg/_conversation_clone.py`: atomic, inert conversation
//! snapshots. A live (pending/running) source turn is frozen through
//! `projection_with_stable_segments` (`segments.rs`).

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use super::support::{
    load, mark_conversation_search_projection_dirty, now_ms, prefix_chars, projection_from_turn_row, py_int, py_str,
    py_strip, str_or,
};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get, required_text, Payload};
use crate::session::{Session, SqlValue};

#[path = "segments.rs"]
mod segments;

const CLONE_RUNTIME_KEYS: &[&str] = &[
    "_activeAttemptId",
    "_attemptId",
    "_authoritativeActiveTaskIds",
    "_commandPending",
    "_flowRunId",
    "_needsStart",
    "_runId",
    "_streamCursor",
    "_streaming",
    "activeTaskId",
    "approvalRequired",
    "attemptId",
    "isStreaming",
    "runId",
];
const CLONE_TASK_ID_KEYS: &[&str] = &["_proactiveTaskId", "_taskId", "_translateTaskId", "taskId"];
const TERMINAL_TOOL_STATUSES: &[&str] = &[
    "abort",
    "aborted",
    "completed",
    "done",
    "error",
    "failed",
    "not-run",
    "not_run",
    "rejected",
    "skipped",
    "succeeded",
    "success",
    "unknown",
];

/// `settings_without_live_runtime(raw)`.
pub fn settings_without_live_runtime(raw: &SqlValue) -> OpResult<Map<String, Value>> {
    let settings = load(raw)?;
    let settings = if truthy(&settings) { settings } else { Value::Object(Map::new()) };
    let Value::Object(mut result) = settings else {
        return fail("database_integrity", "Conversation settings are malformed");
    };
    result.shift_remove("activeTaskId");
    result.shift_remove("_activeAttemptId");
    Ok(result)
}

fn new_task_id() -> String {
    format!("clone-task-{}", uuid::Uuid::new_v4().simple())
}

/// `_clone_projection_value`.
fn clone_projection_value(
    value: &Value,
    turn_ids: &HashMap<String, String>,
    archive_ids: &HashMap<String, String>,
    task_ids: &mut HashMap<String, String>,
) -> Value {
    match value {
        Value::Object(map) => {
            let mut cloned = Map::new();
            for (key, item) in map {
                if CLONE_RUNTIME_KEYS.contains(&key.as_str()) {
                    continue;
                }
                if matches!(key.as_str(), "_turnId" | "turnId") {
                    if let Value::String(old) = item {
                        if let Some(replacement) = turn_ids.get(old).filter(|id| !id.is_empty()) {
                            cloned.insert(key.clone(), Value::String(replacement.clone()));
                        }
                        continue;
                    }
                }
                if matches!(key.as_str(), "archiveId" | "_compactionArchiveId") {
                    if let Value::String(old) = item {
                        cloned.insert(key.clone(), Value::String(archive_ids.get(old).unwrap_or(old).clone()));
                        continue;
                    }
                }
                if CLONE_TASK_ID_KEYS.contains(&key.as_str()) {
                    if let Value::String(old) = item {
                        if !old.is_empty() {
                            let mapped = task_ids.entry(old.clone()).or_insert_with(new_task_id).clone();
                            cloned.insert(key.clone(), Value::String(mapped));
                            continue;
                        }
                    }
                }
                cloned.insert(key.clone(), clone_projection_value(item, turn_ids, archive_ids, task_ids));
            }
            Value::Object(cloned)
        }
        Value::Array(items) => Value::Array(
            items.iter().map(|item| clone_projection_value(item, turn_ids, archive_ids, task_ids)).collect(),
        ),
        other => other.clone(),
    }
}

/// `_aborted_tool_status(value)`.
fn aborted_tool_status(value: &Value) -> Value {
    let raw = if truthy(value) { crate::json::python_str(value) } else { String::new() };
    let normalized = py_strip(&raw).to_lowercase();
    if TERMINAL_TOOL_STATUSES.contains(&normalized.as_str()) {
        Value::String(normalized)
    } else {
        Value::String("aborted".into())
    }
}

/// `_terminalize_live_projection(projection)`.
fn terminalize_live_projection(projection: &Value) -> OpResult<Value> {
    let mut frozen = projection.as_object().cloned().unwrap_or_default();
    if let Some(Value::Array(rounds)) = frozen.get("toolRounds") {
        let rounds: Vec<Value> = rounds
            .iter()
            .map(|source| match source {
                Value::Object(round) => {
                    let mut round = round.clone();
                    let status = aborted_tool_status(get(&round, "status"));
                    round.insert("status".into(), status);
                    Value::Object(round)
                }
                other => other.clone(),
            })
            .collect();
        frozen.insert("toolRounds".into(), Value::Array(rounds));
    }
    if let Some(Value::Array(raw_segments)) = frozen.get("segments") {
        let segments: Vec<Value> = raw_segments
            .iter()
            .map(|source| match source {
                Value::Object(segment) if get(segment, "type") == &json!("tool_use") => {
                    let mut segment = segment.clone();
                    if let Some(Value::Object(result)) = segment.get("result") {
                        let mut result = result.clone();
                        let status = aborted_tool_status(get(&result, "status"));
                        result.insert("status".into(), status);
                        segment.insert("result".into(), Value::Object(result));
                    }
                    Value::Object(segment)
                }
                other => other.clone(),
            })
            .collect();
        frozen.insert("segments".into(), Value::Array(segments));
    }
    if let Some(Value::Object(trace)) = frozen.get("timingTrace") {
        let mut trace = trace.clone();
        trace.insert("status".into(), json!("aborted"));
        trace.insert("running".into(), Value::Bool(false));
        frozen.insert("timingTrace".into(), Value::Object(trace));
    }
    Ok(Value::Object(segments::projection_with_stable_segments(&Value::Object(frozen), "assistant", "interrupted")?))
}

/// `f"{time.time_ns():020d}_{uuid.uuid4().hex}"`.
fn new_archive_id() -> String {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|elapsed| elapsed.as_nanos()).unwrap_or(0);
    format!("{nanos:020}_{}", uuid::Uuid::new_v4().simple())
}

/// `conversation_clone(session, payload, source_id=, user_id=)`.
pub fn conversation_clone(session: &Session<'_>, payload: &Payload, source_id: &str, user_id: i64) -> OpResult<Value> {
    let destination_id = required_text(payload, "destination_conv_id", 256)?;
    let mut lock_ids = vec![source_id.to_owned(), destination_id.clone()];
    lock_ids.sort();
    lock_ids.dedup();
    for lock_id in &lock_ids {
        session.lock_key("conversation", &format!("{user_id}:{lock_id}"));
    }
    for lock_id in &lock_ids {
        session.lock_key("turn_conversation", &format!("{user_id}:{lock_id}"));
    }
    let Some(source) = session
        .fetch_one("SELECT * FROM storage_conversations WHERE id=? AND user_id=?", &params![source_id, user_id])?
    else {
        return Ok(json!({"cloned": false, "missing": true, "busy": false}));
    };
    let destination_taken = session
        .fetch_one("SELECT 1 AS present FROM storage_conversations WHERE id=?", &params![destination_id.as_str()])?
        .is_some()
        || session
            .fetch_one(
                "SELECT 1 AS present FROM storage_conversation_trash \
                 WHERE conversation_id=?",
                &params![destination_id.as_str()],
            )?
            .is_some();
    if destination_taken {
        return fail("database_conflict", "Destination conversation exists");
    }
    let title = match get(payload, "title") {
        Value::Null => format!("{} (copy)", str_or(source.get("title"), "Untitled")),
        Value::String(text) if !super::support::py_strip(text).is_empty() => super::support::py_strip(text).to_owned(),
        _ => return fail("database_protocol_error", "Invalid clone title"),
    };
    let now = now_ms();
    let source_turns = session.fetch_all(
        "SELECT * FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? ORDER BY lane_id,ordinal",
        &params![source_id, user_id],
    )?;
    let mut turn_ids = HashMap::new();
    for turn in &source_turns {
        turn_ids.insert(py_str(turn.get("turn_id")), uuid::Uuid::new_v4().hyphenated().to_string());
    }
    let source_archives = session.fetch_all(
        "SELECT archive_id,task_id FROM storage_compaction_archives \
         WHERE conversation_id=? AND user_id=? ORDER BY created_at_ms,archive_id",
        &params![source_id, user_id],
    )?;
    let mut archive_ids = HashMap::new();
    for row in &source_archives {
        archive_ids.insert(py_str(row.get("archive_id")), new_archive_id());
    }
    let mut task_ids: HashMap<String, String> = HashMap::new();
    let mut settings = settings_without_live_runtime(source.get("settings_json"))?;
    settings.insert("clonedFrom".into(), Value::String(source_id.to_owned()));
    let main_count = source_turns.iter().filter(|turn| str_or(turn.get("lane_id"), "main") == "main").count() as i64;
    let messages = source.get("messages_json");
    let messages = if messages.truthy() { messages.clone() } else { codec::dump_param(&json!([]))? };
    let msg_count = if main_count != 0 {
        main_count
    } else if source.get("msg_count").truthy() {
        py_int(source.get("msg_count"))?
    } else {
        0
    };
    session.execute(
        "INSERT INTO storage_conversations(\
         id,user_id,title,messages_json,created_at_ms,updated_at_ms,\
         settings_json,msg_count,search_text,rev) \
         VALUES (?,?,?,?,?,?,?,?,?,0)",
        &params![
            destination_id.as_str(),
            user_id,
            prefix_chars(&title, 500),
            messages,
            now,
            now,
            codec::dump_param(&Value::Object(settings))?,
            msg_count,
            "",
        ],
    )?;

    for turn in &source_turns {
        let old_turn_id = py_str(turn.get("turn_id"));
        let raw_projection = projection_from_turn_row(session, turn)?;
        if !raw_projection.is_object() {
            return fail("database_integrity", "Turn projection is malformed");
        }
        let projection = clone_projection_value(&raw_projection, &turn_ids, &archive_ids, &mut task_ids);
        let source_status = str_or(turn.get("status"), "");
        let is_live = matches!(source_status.as_str(), "pending" | "running");
        let (projection, clone_status, settlement) = if is_live {
            (
                terminalize_live_projection(&projection)?,
                "interrupted".to_owned(),
                json!({"outcome": "interrupted", "cause": "conversation_cloned_snapshot", "resumeOptions": []}),
            )
        } else {
            let settlement = load(turn.get("settlement_json"))?;
            let settlement = if truthy(&settlement) { settlement } else { Value::Object(Map::new()) };
            if !settlement.is_object() {
                return fail("database_integrity", "Turn settlement is malformed");
            }
            (projection, source_status.clone(), settlement)
        };
        let parent_id = turn.get("parent_turn_id");
        let mapped_parent = if parent_id.truthy() { turn_ids.get(&py_str(parent_id)).cloned() } else { None };
        session.execute(
            "INSERT INTO storage_conversation_turns(\
             turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,\
             actor,kind,run_id,status,current_attempt_id,projection_json,\
             projection_revision,settlement_json,created_at,updated_at) \
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            &params![
                turn_ids[&old_turn_id].as_str(),
                destination_id.as_str(),
                user_id,
                str_or(turn.get("lane_id"), "main"),
                mapped_parent,
                py_int(turn.get("ordinal"))?,
                py_str(turn.get("actor")),
                str_or(turn.get("kind"), "reply"),
                "",
                clone_status,
                SqlValue::Null,
                codec::dump_param(&projection)?,
                1i64,
                codec::dump_param(&settlement)?,
                py_int(turn.get("created_at"))?,
                if is_live { now } else { py_int(turn.get("updated_at"))? },
            ],
        )?;
    }
    mark_conversation_search_projection_dirty(session, &destination_id, user_id)?;

    for archive in &source_archives {
        let old_archive_id = py_str(archive.get("archive_id"));
        let old_task_id = str_or(archive.get("task_id"), "");
        let new_task_id = if old_task_id.is_empty() {
            String::new()
        } else {
            task_ids.entry(old_task_id).or_insert_with(new_task_id).clone()
        };
        session.execute(
            "INSERT INTO storage_compaction_archives(\
             archive_id,conversation_id,user_id,messages_json,summary,receipt_json,trigger,\
             task_id,round_num,model,tokens_before,tokens_after,msgs_before,\
             msgs_after,reason,payload_size,created_at_ms) \
             SELECT ?,?,?,messages_json,summary,receipt_json,trigger,?,round_num,model,\
             tokens_before,tokens_after,msgs_before,msgs_after,reason,\
             payload_size,created_at_ms FROM storage_compaction_archives \
             WHERE archive_id=? AND conversation_id=? AND user_id=?",
            &params![
                archive_ids[&old_archive_id].as_str(),
                destination_id.as_str(),
                user_id,
                new_task_id,
                old_archive_id.as_str(),
                source_id,
                user_id,
            ],
        )?;
    }
    Ok(json!({
        "cloned": true,
        "missing": false,
        "busy": false,
        "conversationId": destination_id,
        "turnCount": source_turns.len(),
        "archiveCount": source_archives.len(),
        "rev": 0,
    }))
}
